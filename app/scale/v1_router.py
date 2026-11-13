"""Scale v1 sub-router -- HTTP surface under ``/api/v1/scale`` (Issue #25,
Task 15; route topology restructured under Issue #86 Task 6).

Spec: docs/product/scale-advice-semantics-contract.md §4.1-§4.3, ADR 0008
Spec (route restructure): docs/superpowers/specs/2026-09-14-harden-scale-
enrollment-lineage-design.md §3.10-§3.11
Task: .superpowers/sdd/2026-09-14-harden-scale-enrollment-lineage/task-6-brief.md

Mounted on the shared ``v1_router`` in ``app.main``, following
``app.generation.v1_router``'s pattern: every route is a thin HTTP adapter
over ``app.scale.enrollment`` (Task 11) and ``app.scale.advice`` (Task 13).
No route touches ``app.scale.evidence``/``app.scale.service``/
``app.scale.evaluator``/``app.scale.worker`` directly -- sealing, assembling,
and running an Evaluation is scheduler/Worker-driven (spec §3.8, Task 14),
not an HTTP command; this router only reads what that pipeline has already
projected (``ScaleEvidenceReadinessProjectionRecord`` and
``ScaleEpisodeProjectionRecord``, via ``GET .../evidence``, #114).

``TenantContext`` and Workspace/actor identity come from
``request_tenant_context`` (verified BFF claims), matching every other
domain router. ``request_tenant_context()`` never populates ``meta_mode``
(spec §3: a Workspace may operate connections in more than one Meta mode).

**Route topology (spec §3.10-§3.11, Task 6):** ``meta_mode`` is exclusively a
URL path segment, never a query parameter or request-body field. This module
exposes two ``APIRouter`` instances:

* ``router`` (prefix ``/scale``) -- the root router. It carries only
  ``GET /available-meta-modes`` (no ``meta_mode`` in its path: the whole
  point of that route is to discover which modes are permitted *before*
  choosing one).
* ``mode_router`` -- every other Scale route, mounted onto ``router`` at
  ``/{meta_mode}`` via ``router.include_router(mode_router, prefix=
  "/{meta_mode}")`` at the bottom of this module. FastAPI validates the path
  segment against ``_META_MODE_PATTERN`` (the three known modes) before any
  handler runs, so an unknown mode is a 422 at the routing layer, not a 400
  from the domain gate.

Every ``mode_router`` route depends on :func:`_mode_effective_context`
instead of taking ``context``/``meta_mode`` as two separate parameters: it
resolves the verified delegated-user ``TenantContext`` from
``request_tenant_context`` and immediately authorizes+binds the path's
``meta_mode`` onto it via ``_effective_context`` (``require_meta_mode``,
Task 2) -- exactly the same gate ``app.scale.platform_router`` uses for the
platform-scoped mirror of this same route tree.

Revision / If-Match: Enrollment is the only mutable, revisioned aggregate a
command in this task's scope mutates directly. ``activate``/``pause``/
``resume``/``close`` all read ``expected_revision`` from ``If-Match`` via
``app.platform.etag.extract_if_match`` (428 if missing, 400 if
unparseable) and pass it straight to the Task 11 domain command, which
raises ``EnrollmentLifecycleError("revision_conflict", ...)`` -- mapped to
409 by :func:`register_scale_v1_problem_handlers` below -- on a stale
write. Episode, Advice thread, and Occurrence are not directly revisioned
HTTP-writable resources in this task's route list, so they carry no
If-Match.

``allowed_actions`` on every Enrollment/Advice-thread response is computed
server-side (``_enrollment_actions``/``_thread_actions`` below, via
``app.platform.action_eligibility.compute_actions``) from the aggregate's
own persisted lifecycle -- the browser never reconstructs eligibility
client-side.

Scoping decisions made for this task (documented so a later task extends
this deliberately, mirroring every earlier Scale task's precedent):

* **Targets, Exports, Enrollment-candidates, and Breakdowns have no
  service module yet.** ``app/scale/`` carries no target-lifecycle,
  export, Winner-eligibility, or breakdown-query module for this router to
  delegate to (Tasks 11-13's own "Consumes" lists never included one).
  Their five routes are mounted now, with stable paths/operation_ids for
  the OpenAPI contract and BFF allowlist, but every handler returns
  ``501 not_implemented`` rather than fabricating behavior.
* **Command-outcome recovery is also a 501 stub.** Every Task 11/13 domain
  command is already idempotent on its own ``idempotency_key`` (replaying
  the same key returns the same row rather than erroring), which is what
  actually protects a client's retry; a dedicated recovery-by-identity
  lookup endpoint has no backing index in this task's scope and is left
  for a later task alongside Targets/Exports.
* **Disposition/snooze require ``Idempotency-Key``; convert does not.**
  ``create_disposition``/``create_snooze`` (Task 15 fix round 1) now accept
  an optional ``idempotency_key``: replaying the same key against the same
  thread returns the existing row instead of inserting a duplicate, exactly
  like every Enrollment command above. These two routes therefore require
  the header the same way. ``convert_occurrence_to_draft``/
  ``convert_occurrence_to_test_plan`` remain naturally idempotent on the
  occurrence's own unique-conversion constraint, so no header is needed
  there.
"""
from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.experiments.winner_eligibility import WinnerIneligibleError
from app.library.api import _request_problem
from app.platform.action_eligibility import compute_actions
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.meta_mode_auth import (
    MetaModeNotPermitted,
    list_available_meta_modes,
    require_meta_mode,
)
from app.platform.pagination import InvalidCursorError, decode_offset_cursor, encode_cursor
from app.platform.tenancy import TenantContext, TenantScopeError

from .advice import (
    AdviceLifecycleError,
    convert_occurrence_to_draft,
    convert_occurrence_to_test_plan,
    create_disposition,
    create_snooze,
)
from .activation_preview import (
    EnrollmentLifecycleInvalidError as PreviewLifecycleInvalidError,
    EnrollmentNotFoundError as PreviewEnrollmentNotFoundError,
    PreviewAlreadyConsumedError,
    PreviewEnrollmentMismatchError,
    PreviewExpiredError,
    PreviewRevisionMismatchError,
    create_activation_preview,
)
from .candidates import (
    CandidateAlreadyConsumedError,
    CandidateExpiredError,
    CandidateNotFoundError,
    create_enrollment_candidate,
    list_eligible_candidates,
)
from .breakdowns import BreakdownNotAvailableError, query_snapshot_breakdowns
from .delivery_targets import DeliveryTargetError
from .observed_ads import ObservedAdListing, create_delivery_target_from_ad, list_observed_ads
from .command_journal import CommandOutcomeNotFoundError, IdempotencyConflictError, lookup_command_outcome
from .enrollment import (
    CandidateFingerprintDriftError,
    EnrollmentLifecycleError,
    activate_enrollment,
    close_enrollment,
    create_draft_enrollment,
    pause_enrollment,
    resume_enrollment,
)
from .enrollment_lineage import (
    DeliveryTargetExpiredError,
    DeliveryTargetNotFoundError,
    WinnerTargetMismatchError,
)
from .exports import (
    ExportError,
    ExportNotCompleteError,
    ExportNotFoundError,
    create_export,
    create_export_download_intent,
)
from .models import (
    ScaleAdviceConversionRecord,
    ScaleAdviceDispositionRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceSnoozeRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentCandidateRecord,
    ScaleEnrollmentRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationSignalRecord,
    ScaleEvidenceReadinessProjectionRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleExportRecord,
    ScalePolicyVersionRecord,
    ScaleSnapshotQualityIssueRecord,
    ScaleSourceQualityIssueRecord,
    ScaleTargetVersionRecord,
)
from .schemas import (
    ActivationPreviewCreateRequest,
    ActivationPreviewResponse,
    AdviceDispositionRequest,
    AdviceDispositionResponse,
    AdviceSnoozeRequest,
    AdviceSnoozeResponse,
    AdviceThreadListResponse,
    AdviceThreadResponse,
    BreakdownResponse,
    BreakdownSegmentResponse,
    CandidateCreateRequest,
    CandidateResponse,
    CommandOutcomeResponse,
    ConversionRequest,
    DeliveryTargetCreateRequest,
    DeliveryTargetResponse,
    ConversionResponse,
    EnrollmentActivateRequest,
    EnrollmentCloseRequest,
    EnrollmentCreateRequest,
    EnrollmentEvidenceResponse,
    EnrollmentListResponse,
    EnrollmentPauseRequest,
    EnrollmentResponse,
    EnrollmentResumeRequest,
    EpisodeResponse,
    ExportCreateRequest,
    ExportDownloadIntentRequest,
    ExportDownloadIntentResponse,
    ExportResponse,
    ObservedAdListResponse,
    TargetContextListResponse,
    TargetContextResponse,
    TargetCreateRequest,
    TargetListResponse,
    TargetPublicationPreviewRequest,
    TargetPublicationPreviewResponse,
    TargetPublishRequest,
    TargetResponse,
    TargetWithdrawalPreviewRequest,
    TargetWithdrawalPreviewResponse,
    TargetWithdrawRequest,
)
from .targets import (
    TargetLifecycleError,
    TargetNotFoundError,
    TargetPreviewConsumedError,
    TargetPreviewExpiredError,
    applicable_policy,
    create_publication_preview,
    create_withdrawal_preview,
    get_target,
    list_targets,
    platform_spend_floor,
    publish_target,
    resolve_target_contexts,
    target_actions,
    target_below_platform_floor,
    withdraw_target,
)
from .types import AdviceType

#: The three Meta evidence modes ``require_meta_mode``/``list_available_
#: meta_modes`` (``app.platform.meta_mode_auth``) know about. FastAPI's own
#: ``Path(pattern=...)`` validation rejects anything else with a 422 before
#: a handler -- or ``require_meta_mode``'s own "unknown meta_mode" 400 --
#: ever runs (spec §3.10).
_META_MODE_PATTERN = r"^(customer_sandbox|controlled_mock|internal_validation)$"

router = APIRouter(prefix="/scale", tags=["scale"])

#: Every route except ``available-meta-modes`` -- mounted onto ``router`` at
#: ``/{meta_mode}`` at the bottom of this module (spec §3.11).
mode_router = APIRouter(tags=["scale"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _effective_context(context: TenantContext, meta_mode: str) -> TenantContext:
    """Authorize and bind the request's target Meta mode onto the verified
    TenantContext (see module docstring).

    ``require_meta_mode`` checks the context's ``identity_kind`` --
    delegated users may not select ``internal_validation`` regardless of
    what ``executor`` string happens to be set (ADR 0009).
    """
    try:
        require_meta_mode(context, meta_mode)
        return replace(context, meta_mode=meta_mode)
    except MetaModeNotPermitted as exc:
        status = 400 if exc.code == "invalid_meta_mode" else 403
        raise HTTPException(status, {"code": exc.code, "message": exc.detail}) from exc
    except TenantScopeError as exc:
        raise HTTPException(400, {"code": "invalid_meta_mode", "message": str(exc)}) from exc


def _mode_effective_context(
    meta_mode: str = Path(
        ..., pattern=_META_MODE_PATTERN, description="Meta evidence mode (URL path segment).",
    ),
    context: TenantContext = Depends(request_tenant_context),
) -> TenantContext:
    """Every ``mode_router`` route depends on this instead of separately
    declaring ``meta_mode``/``context`` -- it resolves+authorizes the path's
    ``{meta_mode}`` segment onto the verified delegated-user context in one
    step (see module docstring)."""
    return _effective_context(context, meta_mode)


def _limit_query(default: int = 50):
    return Query(default=default, ge=1, le=100)


def _paginate(items: list, *, context: TenantContext, filters_key: str,
              cursor: str | None, limit: int) -> dict:
    start = 0
    if cursor:
        try:
            start = decode_offset_cursor(cursor, context.workspace_id, filters_key)
        except InvalidCursorError as exc:
            raise HTTPException(400, {"code": "invalid_cursor", "message": str(exc)}) from exc
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        next_cursor = encode_cursor(context.workspace_id, filters_key, (start + limit,))
    return {"items": page, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Enrollment projections + allowed_actions
# ---------------------------------------------------------------------------

_ENROLLMENT_ACTION_CODES = ["activate", "pause", "resume", "close"]


def _enrollment_actions(enrollment: ScaleEnrollmentRecord) -> list:
    lifecycle = enrollment.lifecycle
    checks = {
        "activate": (lifecycle == "draft", "invalid_lifecycle"),
        "pause": (lifecycle == "active", "invalid_lifecycle"),
        "resume": (lifecycle == "paused", "invalid_lifecycle"),
        "close": (lifecycle in ("active", "paused"), "invalid_lifecycle"),
    }
    return compute_actions(_ENROLLMENT_ACTION_CODES, checks)


def _enrollment_dict(enrollment: ScaleEnrollmentRecord) -> dict:
    return {
        "id": enrollment.id,
        "meta_mode": enrollment.meta_mode,
        "meta_connection_id": enrollment.meta_connection_id,
        "scenario_scope_key": enrollment.scenario_scope_key,
        "ad_account_id": enrollment.ad_account_id,
        "winner_decision_id": enrollment.winner_decision_id,
        "winner_confirmation_id": enrollment.winner_confirmation_id,
        "asset_version_id": enrollment.asset_version_id,
        "product_version_id": enrollment.product_version_id,
        "offer_version_id": enrollment.offer_version_id,
        "market_code": enrollment.market_code,
        "landing_page_version_id": enrollment.landing_page_version_id,
        "market_asset_binding_id": enrollment.market_asset_binding_id,
        "campaign_remote_id": enrollment.campaign_remote_id,
        "ad_set_remote_id": enrollment.ad_set_remote_id,
        "ad_remote_id": enrollment.ad_remote_id,
        "remote_binding_key": enrollment.remote_binding_key,
        "lifecycle": enrollment.lifecycle,
        "transition_reason": enrollment.transition_reason,
        "current_episode_id": enrollment.current_episode_id,
        "creator_id": enrollment.creator_id,
        "original_initiator_id": enrollment.original_initiator_id,
        "revision": enrollment.revision,
        "evaluation_cancellation_generation": enrollment.evaluation_cancellation_generation,
        "created_at": enrollment.created_at,
        "updated_at": enrollment.updated_at,
        "closed_at": enrollment.closed_at,
        "allowed_actions": _enrollment_actions(enrollment),
    }


def _episode_dict(episode: ScaleEpisodeRecord) -> dict:
    return {
        "id": episode.id,
        "enrollment_id": episode.enrollment_id,
        "meta_mode": episode.meta_mode,
        "phase": episode.phase,
        "started_at": episode.started_at,
        "started_local_day": episode.started_local_day,
        "ended_at": episode.ended_at,
        "closure_reason": episode.closure_reason,
        "delivery_evidence_hash": episode.delivery_evidence_hash,
        "delivery_comparability_hash": episode.delivery_comparability_hash,
        "metric_semantic_contract_set_id": episode.metric_semantic_contract_set_id,
        "timezone_name": episode.timezone_name,
        "currency": episode.currency,
        "attribution_semantics_hash": episode.attribution_semantics_hash,
        "revision": episode.revision,
        "projection_revision": episode.projection_revision,
        "accepted_evaluation_id": episode.accepted_evaluation_id,
        # No command in this task's route list mutates an Episode directly
        # (it moves only as a side effect of an Enrollment command) -- no
        # action codes are eligible on it.
        "allowed_actions": compute_actions([], {}),
    }


def _evidence_detail_dict(
    evaluation: ScaleEvaluationRecord,
    snapshot: ScaleEvidenceSnapshotRecord,
    quality_issues: list[ScaleSnapshotQualityIssueRecord],
    signals: list[ScaleEvaluationSignalRecord],
) -> dict:
    """Serialize immutable evidence owned by the accepted Evaluation.

    Decimal signal values cross the HTTP boundary as strings so the browser
    never loses interval or threshold precision to JavaScript floats.
    """

    def decimal_string(value):
        return str(value) if value is not None else None

    return {
        "snapshot_id": snapshot.id,
        "policy_version_id": evaluation.policy_version_id,
        "target_version_id": evaluation.target_version_id,
        "algorithm_version": evaluation.algorithm_version,
        "metric_semantic_contract_set_hash": evaluation.metric_semantic_contract_set_hash,
        "data_quality_state": evaluation.data_quality_state,
        "safe_watermark": snapshot.safe_watermark,
        "last_complete_local_day": snapshot.last_complete_local_day,
        "last_sync_at": snapshot.last_sync_at,
        "freshness_reference_at": snapshot.freshness_reference_at,
        "complete_local_days_in_episode": snapshot.complete_local_days_in_episode,
        "active_delivery_days_in_episode": snapshot.active_delivery_days_in_episode,
        "current_window": {
            "role": "current_7d",
            "start_local_day": snapshot.current_window_start_local_day,
            "end_local_day": snapshot.current_window_end_local_day,
        },
        "baseline_window": {
            "role": "baseline_7d",
            "start_local_day": snapshot.baseline_window_start_local_day,
            "end_local_day": snapshot.baseline_window_end_local_day,
        },
        "rolling_window": {
            "role": "rolling_14d",
            "start_local_day": snapshot.rolling_window_start_local_day,
            "end_local_day": snapshot.rolling_window_end_local_day,
        },
        "quality_issues": [_quality_issue_dict(issue) for issue in quality_issues],
        "signals": [
            {
                "signal_id": signal.signal_id,
                "state": signal.state,
                "expected": decimal_string(signal.expected),
                "observed": decimal_string(signal.observed),
                "interval_lower": decimal_string(signal.interval_lower),
                "interval_upper": decimal_string(signal.interval_upper),
                "threshold": decimal_string(signal.threshold),
                "p_value": decimal_string(signal.p_value),
                "adjusted_q_value": decimal_string(signal.adjusted_q_value),
                "reason_code": signal.reason_code,
            }
            for signal in signals
        ],
    }


def _quality_issue_dict(issue) -> dict:
    """One typed quality issue: a snapshot's (``ScaleSnapshotQualityIssueRecord``)
    or a source manifest's (``ScaleSourceQualityIssueRecord``) -- the two
    share one value contract (spec §1.7)."""
    return {
        "code": issue.code,
        "severity": issue.severity,
        "is_blocking": issue.is_blocking,
        "contract_type": issue.contract_type,
        "window_role": issue.window_role,
        "local_day": issue.local_day,
        "segment_key": issue.segment_key,
        "detail": issue.detail,
        "recovery_action": issue.recovery_action,
    }


#: The accepted-Evaluation fields of ``EnrollmentEvidenceResponse``, all
#: null until the Episode's first Evaluation projects (#114).
_EVALUATION_FIELDS = (
    "evaluation_id", "maturity", "health", "health_reason", "healthy_evaluations",
    "streak_counters", "cursor_as_of_local_day", "cursor_source_revision",
    "cursor_semantic_event_sequence", "cursor_lineage_generation", "revision", "updated_at",
    "snapshot_id", "policy_version_id", "target_version_id", "algorithm_version",
    "metric_semantic_contract_set_hash", "data_quality_state", "safe_watermark",
    "last_complete_local_day", "last_sync_at", "freshness_reference_at",
    "complete_local_days_in_episode", "active_delivery_days_in_episode", "current_window",
    "baseline_window", "rolling_window", "quality_issues", "signals",
)


def _readiness(
    session, context: TenantContext, episode_id: str,
) -> tuple[dict | None, int | None]:
    """The Episode's evidence-readiness projection (contract §8, §18) with
    its latest source manifest's typed issues, and that manifest's source
    revision; ``(None, None)`` before the collector wrote one. Every read
    is scoped by the context's Workspace and Meta mode."""
    projection = session.execute(
        select(ScaleEvidenceReadinessProjectionRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode, episode_id=episode_id,
        )
    ).scalar_one_or_none()
    if projection is None:
        return None, None
    manifest = None
    issues: list = []
    if projection.latest_manifest_id is not None:
        manifest = session.execute(
            select(ScaleEvidenceSourceManifestRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode,
                id=projection.latest_manifest_id, episode_id=episode_id,
            )
        ).scalar_one_or_none()
        issues = session.execute(
            select(ScaleSourceQualityIssueRecord)
            .filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode,
                manifest_id=projection.latest_manifest_id,
            )
            .order_by(
                ScaleSourceQualityIssueRecord.is_blocking.desc(),
                ScaleSourceQualityIssueRecord.code.asc(),
                ScaleSourceQualityIssueRecord.id.asc(),
            )
        ).scalars().all()
    revision = manifest.source_revision if manifest is not None else None
    return {
        "manifest_id": projection.latest_manifest_id,
        "manifest_state": manifest.state if manifest is not None else None,
        "source_revision": revision,
        "target_local_day": manifest.target_local_day if manifest is not None else None,
        "maturity": projection.maturity,
        "quality_state": projection.quality_state,
        "last_complete_local_day": projection.last_complete_local_day,
        "freshness_reference_at": projection.freshness_reference_at,
        "blocking_issue_count": projection.blocking_issue_count,
        "warning_issue_count": projection.warning_issue_count,
        "recovery_action": projection.recovery_action,
        "issues": [_quality_issue_dict(issue) for issue in issues],
        "revision": projection.revision,
        "updated_at": projection.updated_at,
    }, revision


def _enrollment_evidence(session, context: TenantContext, enrollment_id: str) -> dict:
    """``GET .../evidence`` for both the delegated and the platform router.

    404 ``enrollment_not_found`` for an Enrollment outside the context's
    Workspace and Meta mode, 404 ``no_active_episode`` without a current
    Episode. Otherwise 200 (#114): ``readiness`` from the Episode's
    evidence-readiness projection (null before the first manifest), and the
    accepted Evaluation's fields, all null before the first Evaluation
    projects -- an activated Enrollment waiting for its first complete day
    is a state, not a missing resource. ``evaluation_stale`` is true when
    the latest manifest is a newer source revision than the one the
    Evaluation was sealed from, so a blocked day over an older Evaluation
    reads as stale/blocked rather than as current (contract §8, §22).
    """
    enrollment = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode, id=enrollment_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(404, {"code": "enrollment_not_found", "message": enrollment_id})
    if enrollment.current_episode_id is None:
        raise HTTPException(404, {"code": "no_active_episode", "message": enrollment_id})
    episode_id = enrollment.current_episode_id
    readiness, latest_revision = _readiness(session, context, episode_id)
    body = {
        "enrollment_id": enrollment.id,
        "episode_id": episode_id,
        "readiness": readiness,
        "evaluation_stale": False,
        **dict.fromkeys(_EVALUATION_FIELDS),
    }
    projection = session.execute(
        select(ScaleEpisodeProjectionRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode, episode_id=episode_id,
        )
    ).scalar_one_or_none()
    if projection is None:
        return body
    evaluation = session.execute(
        select(ScaleEvaluationRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=projection.evaluation_id,
            episode_id=projection.episode_id,
        )
    ).scalar_one_or_none()
    if evaluation is None:
        raise HTTPException(
            500,
            {"code": "evidence_projection_incomplete", "message": "accepted Evaluation is unavailable"},
        )
    snapshot = session.execute(
        select(ScaleEvidenceSnapshotRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=evaluation.snapshot_id,
            episode_id=projection.episode_id,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(
            500,
            {"code": "evidence_projection_incomplete", "message": "accepted EvidenceSnapshot is unavailable"},
        )
    quality_issues = session.execute(
        select(ScaleSnapshotQualityIssueRecord)
        .filter_by(workspace_id=context.workspace_id, meta_mode=context.meta_mode, snapshot_id=snapshot.id)
        .order_by(
            ScaleSnapshotQualityIssueRecord.is_blocking.desc(),
            ScaleSnapshotQualityIssueRecord.code.asc(),
            ScaleSnapshotQualityIssueRecord.id.asc(),
        )
    ).scalars().all()
    signals = session.execute(
        select(ScaleEvaluationSignalRecord)
        .filter_by(workspace_id=context.workspace_id, meta_mode=context.meta_mode, evaluation_id=evaluation.id)
        .order_by(ScaleEvaluationSignalRecord.signal_id.asc())
    ).scalars().all()
    body.update({
        "evaluation_stale": latest_revision is not None and latest_revision > snapshot.source_revision,
        "evaluation_id": projection.evaluation_id,
        "maturity": projection.maturity,
        "health": projection.health,
        "health_reason": projection.health_reason,
        "healthy_evaluations": projection.healthy_evaluations,
        "streak_counters": projection.streak_counters,
        "cursor_as_of_local_day": projection.cursor_as_of_local_day,
        "cursor_source_revision": projection.cursor_source_revision,
        "cursor_semantic_event_sequence": projection.cursor_semantic_event_sequence,
        "cursor_lineage_generation": projection.cursor_lineage_generation,
        "revision": projection.revision,
        "updated_at": projection.updated_at,
        **_evidence_detail_dict(evaluation, snapshot, quality_issues, signals),
    })
    return body


_THREAD_ACTION_CODES = ["acknowledge", "dismiss", "snooze", "convert"]
_CONVERTIBLE_ADVICE_TYPES = (AdviceType.REFRESH_CREATIVE.value, AdviceType.CONSIDER_SCALE_TEST.value)


def _thread_actions(thread: ScaleAdviceThreadRecord) -> list:
    is_active = thread.lifecycle == "active"
    convertible = is_active and thread.advice_type in _CONVERTIBLE_ADVICE_TYPES
    checks = {
        "acknowledge": (is_active, "thread_not_active"),
        "dismiss": (is_active, "thread_not_active"),
        "snooze": (is_active, "thread_not_active"),
        "convert": (
            convertible,
            "thread_not_active" if not is_active else "advice_type_not_convertible",
        ),
    }
    return compute_actions(_THREAD_ACTION_CODES, checks)


def _thread_dict(thread: ScaleAdviceThreadRecord) -> dict:
    return {
        "id": thread.id,
        "episode_id": thread.episode_id,
        "enrollment_id": thread.enrollment_id,
        "advice_type": thread.advice_type,
        "reason_signature": thread.reason_signature,
        "severity": thread.severity,
        "lifecycle": thread.lifecycle,
        "predecessor_thread_id": thread.predecessor_thread_id,
        "first_occurrence_id": thread.first_occurrence_id,
        "latest_occurrence_id": thread.latest_occurrence_id,
        "disposition_kind": thread.disposition_kind,
        "disposition_at": thread.disposition_at,
        "snooze_until": thread.snooze_until,
        "opened_at": thread.opened_at,
        "resolved_at": thread.resolved_at,
        "lifecycle_reason": thread.lifecycle_reason,
        "revision": thread.revision,
        "allowed_actions": _thread_actions(thread),
    }


@router.get("/available-meta-modes", operation_id="list_scale_available_modes")
def list_available_meta_modes_route(context: TenantContext = Depends(request_tenant_context)):
    """The Meta modes this request's identity may select (see
    ``app.platform.meta_mode_auth``). Root-level -- no ``meta_mode`` path
    segment, since the point is to discover which modes are permitted
    before choosing one (spec §3.11).

    ``operation_id`` deliberately avoids the substring "meta" (beyond the
    path itself): ``test_scale_openapi_has_no_meta_mutation_surface`` (ADR
    0008, "Scale never performs Meta writes") asserts no Scale operationId
    contains it, to keep a real Meta-mutation route from ever leaking into
    Scale's contract undetected. This route only reads which Meta modes an
    identity may select -- it performs no Meta write -- but a literal
    "meta" substring would still trip that guard.
    """
    return {"modes": list_available_meta_modes(context)}


# ---------------------------------------------------------------------------
# Enrollment lifecycle -- spec §4.3
# ---------------------------------------------------------------------------


@mode_router.post(
    "/enrollments",
    operation_id="create_scale_enrollment",
    response_model=EnrollmentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_enrollment(
    body: EnrollmentCreateRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        enrollment = create_draft_enrollment(
            session, effective,
            enrollment_candidate_id=body.enrollment_candidate_id,
            idempotency_key=idempotency_key,
        )
        return _enrollment_dict(enrollment)


@mode_router.get("/enrollments", operation_id="list_scale_enrollments", response_model=EnrollmentListResponse)
def list_enrollments(
    lifecycle: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        stmt = select(ScaleEnrollmentRecord).filter_by(
            workspace_id=effective.workspace_id, meta_mode=effective.meta_mode,
        )
        if lifecycle:
            stmt = stmt.filter(ScaleEnrollmentRecord.lifecycle == lifecycle)
        rows = session.execute(stmt.order_by(ScaleEnrollmentRecord.created_at.desc())).scalars().all()
        items = [_enrollment_dict(row) for row in rows]
    filters_key = f"enrollments:{lifecycle or ''}"
    return _paginate(items, context=effective, filters_key=filters_key, cursor=cursor, limit=limit)


@mode_router.get("/enrollments/{enrollment_id}", operation_id="get_scale_enrollment", response_model=EnrollmentResponse)
def get_enrollment(
    enrollment_id: str,
    response: Response,
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        enrollment = session.execute(
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=effective.workspace_id, meta_mode=effective.meta_mode, id=enrollment_id,
            )
        ).scalar_one_or_none()
        if enrollment is None:
            raise HTTPException(404, {"code": "enrollment_not_found", "message": enrollment_id})
        payload = _enrollment_dict(enrollment)
    set_etag(response, payload["revision"])
    return payload


@mode_router.put(
    "/enrollments/{enrollment_id}/activate",
    operation_id="activate_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def activate_enrollment_route(
    enrollment_id: str,
    body: EnrollmentActivateRequest,
    request: Request,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(effective) as session:
        enrollment, _episode = activate_enrollment(
            session, effective,
            enrollment_id=enrollment_id,
            activation_preview_id=body.activation_preview_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return _enrollment_dict(enrollment)


@mode_router.put(
    "/enrollments/{enrollment_id}/pause",
    operation_id="pause_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def pause_enrollment_route(
    enrollment_id: str,
    body: EnrollmentPauseRequest,
    request: Request,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(effective) as session:
        enrollment = pause_enrollment(
            session, effective, enrollment_id,
            expected_revision=expected_revision, idempotency_key=idempotency_key, reason=body.reason,
        )
        return _enrollment_dict(enrollment)


@mode_router.put(
    "/enrollments/{enrollment_id}/resume",
    operation_id="resume_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def resume_enrollment_route(
    enrollment_id: str,
    body: EnrollmentResumeRequest,
    request: Request,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(effective) as session:
        enrollment, _episode = resume_enrollment(
            session, effective,
            enrollment_id=enrollment_id,
            activation_preview_id=body.activation_preview_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return _enrollment_dict(enrollment)


@mode_router.put(
    "/enrollments/{enrollment_id}/close",
    operation_id="close_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def close_enrollment_route(
    enrollment_id: str,
    body: EnrollmentCloseRequest,
    request: Request,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(effective) as session:
        enrollment = close_enrollment(
            session, effective, enrollment_id,
            expected_revision=expected_revision, idempotency_key=idempotency_key, reason=body.reason,
        )
        return _enrollment_dict(enrollment)


@mode_router.get(
    "/enrollments/{enrollment_id}/episodes/{episode_id}",
    operation_id="get_scale_episode",
    response_model=EpisodeResponse,
)
def get_episode(
    enrollment_id: str,
    episode_id: str,
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        episode = session.execute(
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=effective.workspace_id, meta_mode=effective.meta_mode,
                id=episode_id, enrollment_id=enrollment_id,
            )
        ).scalar_one_or_none()
        if episode is None:
            raise HTTPException(404, {"code": "episode_not_found", "message": episode_id})
        return _episode_dict(episode)


@mode_router.get(
    "/enrollments/{enrollment_id}/evidence",
    operation_id="get_scale_enrollment_evidence",
    response_model=EnrollmentEvidenceResponse,
)
def get_enrollment_evidence(
    enrollment_id: str,
    effective: TenantContext = Depends(_mode_effective_context),
):
    """Current evidence/readiness (spec §4.3, #114): the Episode's
    evidence-readiness projection and the accepted-Evaluation projection
    the pipeline already CAS-maintains -- not a fresh evaluation request:
    collecting, sealing and evaluating are scheduler/Worker-driven (module
    docstring)."""
    # Shared with the platform mirror: ``_enrollment_evidence``.
    with tenant_session(effective) as session:
        return _enrollment_evidence(session, effective, enrollment_id)


@mode_router.get(
    "/enrollments/{enrollment_id}/breakdowns",
    operation_id="get_scale_enrollment_breakdowns",
    response_model=BreakdownResponse,
)
def get_enrollment_breakdowns(
    enrollment_id: str,
    snapshot_id: str = Query(..., min_length=1, description="Immutable snapshot to pin breakdowns to"),
    breakdown_type: str = Query(..., min_length=1, description="placement, country, or age_gender"),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        try:
            page = query_snapshot_breakdowns(
                session, effective,
                enrollment_id=enrollment_id,
                snapshot_id=snapshot_id,
                breakdown_type=breakdown_type,
                cursor=cursor,
                limit=limit,
            )
        except InvalidCursorError as exc:
            raise HTTPException(400, {"code": "invalid_cursor", "message": str(exc)}) from exc
        return {
            "snapshot_id": page.snapshot_id,
            "episode_id": page.episode_id,
            "breakdown_type": page.breakdown_type,
            "window_start": page.window_start,
            "window_end": page.window_end,
            "safe_watermark": page.safe_watermark,
            "meta_mode": page.meta_mode,
            "policy_version_id": page.policy_version_id,
            "target_version_id": page.target_version_id,
            "segments": [
                {
                    "breakdown_type": s.breakdown_type,
                    "segment_key": s.segment_key,
                    "state": s.state,
                    "suppression_reason": s.suppression_reason,
                    "spend_share": s.spend_share,
                    "impressions": s.impressions,
                    "link_clicks": s.link_clicks,
                    "purchases": s.purchases,
                    "purchase_value": s.purchase_value,
                    "cpa": s.cpa,
                    "roas": s.roas,
                    "insufficient_reason": s.insufficient_reason,
                } for s in page.segments
            ],
            "next_cursor": page.next_cursor,
        }


# ---------------------------------------------------------------------------
# Advice -- spec §4.3
# ---------------------------------------------------------------------------


@mode_router.get("/advice-threads", operation_id="list_scale_advice_threads", response_model=AdviceThreadListResponse)
def list_advice_threads(
    enrollment_id: str | None = Query(default=None),
    lifecycle: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        stmt = select(ScaleAdviceThreadRecord).filter_by(
            workspace_id=effective.workspace_id, meta_mode=effective.meta_mode,
        )
        if enrollment_id:
            stmt = stmt.filter(ScaleAdviceThreadRecord.enrollment_id == enrollment_id)
        if lifecycle:
            stmt = stmt.filter(ScaleAdviceThreadRecord.lifecycle == lifecycle)
        rows = session.execute(stmt.order_by(ScaleAdviceThreadRecord.opened_at.desc())).scalars().all()
        items = [_thread_dict(row) for row in rows]
    filters_key = f"advice-threads:{enrollment_id or ''}:{lifecycle or ''}"
    return _paginate(items, context=effective, filters_key=filters_key, cursor=cursor, limit=limit)


@mode_router.post(
    "/advice-threads/{thread_id}/disposition",
    operation_id="create_scale_advice_disposition",
    response_model=AdviceDispositionResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_advice_disposition(
    thread_id: str,
    body: AdviceDispositionRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    actor_id = effective.initiator_id or effective.executor
    with tenant_session(effective) as session:
        disposition_id = create_disposition(
            session, effective, thread_id, body.kind,
            actor_id=actor_id, note=body.note, occurrence_id=body.occurrence_id,
            idempotency_key=idempotency_key,
        )
        row = session.get(ScaleAdviceDispositionRecord, disposition_id)
        return {
            "id": row.id,
            "thread_id": row.thread_id,
            "occurrence_id": row.occurrence_id,
            "kind": row.kind,
            "note": row.note,
            "actor_id": row.actor_id,
            "created_at": row.created_at,
        }


@mode_router.post(
    "/advice-threads/{thread_id}/snooze",
    operation_id="create_scale_advice_snooze",
    response_model=AdviceSnoozeResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_advice_snooze(
    thread_id: str,
    body: AdviceSnoozeRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    actor_id = effective.initiator_id or effective.executor
    with tenant_session(effective) as session:
        snooze_id = create_snooze(
            session, effective, thread_id, body.until, actor_id=actor_id, reason=body.reason,
            idempotency_key=idempotency_key,
        )
        row = session.get(ScaleAdviceSnoozeRecord, snooze_id)
        return {
            "id": row.id,
            "thread_id": row.thread_id,
            "actor_id": row.actor_id,
            "started_at": row.started_at,
            "snooze_until": row.snooze_until,
            "reason": row.reason,
        }


@mode_router.post(
    "/advice-occurrences/{occurrence_id}/convert",
    operation_id="convert_scale_advice_occurrence",
    response_model=ConversionResponse,
    status_code=201,
)
def convert_advice_occurrence(
    occurrence_id: str,
    body: ConversionRequest,
    effective: TenantContext = Depends(_mode_effective_context),
):
    initiator_id = effective.initiator_id or effective.executor
    with tenant_session(effective) as session:
        occurrence = session.execute(
            select(ScaleAdviceOccurrenceRecord).filter_by(
                workspace_id=effective.workspace_id, meta_mode=effective.meta_mode, id=occurrence_id,
            )
        ).scalar_one_or_none()
        if occurrence is None:
            raise HTTPException(404, {"code": "occurrence_not_found", "message": occurrence_id})

        if occurrence.advice_type == AdviceType.REFRESH_CREATIVE.value:
            conversion_id = convert_occurrence_to_draft(
                session, effective, occurrence_id,
                initiator_id=initiator_id, provider=body.provider, model=body.model,
                refresh_axis=body.refresh_axis,
            )
        elif occurrence.advice_type == AdviceType.CONSIDER_SCALE_TEST.value:
            conversion_id = convert_occurrence_to_test_plan(
                session, effective, occurrence_id, initiator_id=initiator_id,
            )
        else:
            raise HTTPException(
                422,
                {
                    "code": "not_convertible",
                    "message": f"advice_type {occurrence.advice_type!r} has no conversion path",
                },
            )

        row = session.get(ScaleAdviceConversionRecord, conversion_id)
        return {
            "id": row.id,
            "thread_id": row.thread_id,
            "occurrence_id": row.occurrence_id,
            "generation_draft_id": row.generation_draft_id,
            "scale_test_plan_draft_id": row.scale_test_plan_draft_id,
            "input_lineage_hash": row.input_lineage_hash,
            "initiator_id": row.initiator_id,
            "created_at": row.created_at,
        }


# ---------------------------------------------------------------------------
# Activation previews (spec §4.5, Issue #86 Task 10)
# ---------------------------------------------------------------------------


def _preview_dict(preview) -> dict:
    return {
        "id": preview.id,
        "enrollment_id": preview.enrollment_id,
        "meta_mode": preview.meta_mode,
        "scenario_scope_key": preview.scenario_scope_key,
        "transition_kind": preview.transition_kind,
        "delivery_evidence_snapshot_id": preview.delivery_evidence_snapshot_id,
        "lineage_guard_id": preview.lineage_guard_id,
        "candidate_fingerprint": preview.candidate_fingerprint,
        "guard_revision": preview.guard_revision,
        "lineage_hash": preview.lineage_hash,
        "enrollment_revision": preview.enrollment_revision,
        "expires_at": preview.expires_at,
        "consumed_at": preview.consumed_at,
        "created_at": preview.created_at,
    }


def _target_dict(
    target: ScaleTargetVersionRecord,
    context: TenantContext,
    policy: ScalePolicyVersionRecord | None,
) -> dict:
    """``policy`` is the one in force for ``context``'s mode
    (``app.scale.targets.applicable_policy``), read once per response: a
    Target reports its floor and whether it falls below it (#134)."""
    floor = platform_spend_floor(policy, target.currency)
    return {
        "id": target.id,
        "product_version_id": target.product_version_id,
        "offer_version_id": target.offer_version_id,
        "market_code": target.market_code,
        "currency": target.currency,
        "target_cpa": str(target.target_cpa) if target.target_cpa else None,
        "breakeven_roas": str(target.breakeven_roas) if target.breakeven_roas else None,
        "minimum_economic_spend": str(target.minimum_economic_spend) if target.minimum_economic_spend else None,
        "lifecycle_state": target.lifecycle_state,
        "creator_id": target.creator_id,
        "revision": target.revision,
        "published_at": target.published_at,
        "withdrawn_at": target.withdrawn_at,
        "created_at": target.created_at,
        "allowed_actions": target_actions(target, context),
        "platform_spend_floor": floor.amount_text,
        "below_platform_floor": target_below_platform_floor(target, floor),
    }


@mode_router.post(
    "/enrollments/{enrollment_id}/activation-previews",
    operation_id="create_scale_activation_preview",
    response_model=ActivationPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_activation_preview_route(
    enrollment_id: str,
    body: ActivationPreviewCreateRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        preview = create_activation_preview(
            session, effective,
            enrollment_id=enrollment_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
        return _preview_dict(preview)


# ---------------------------------------------------------------------------
# Targets -- spec §10, §21 (Issue #87)
# ---------------------------------------------------------------------------


@mode_router.get(
    "/target-contexts/current",
    operation_id="list_scale_target_contexts",
    response_model=TargetContextListResponse,
)
def list_target_contexts(effective: TenantContext = Depends(_mode_effective_context)):
    with tenant_session(effective) as session:
        contexts = resolve_target_contexts(session, effective)
    return {"items": contexts}


@mode_router.get("/targets", operation_id="list_scale_targets", response_model=TargetListResponse)
def list_targets_route(
    lifecycle_state: str | None = Query(default=None),
    product_version_id: str | None = Query(default=None),
    offer_version_id: str | None = Query(default=None),
    market_code: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        rows = list_targets(
            session, effective,
            lifecycle_state=lifecycle_state,
            product_version_id=product_version_id,
            offer_version_id=offer_version_id,
            market_code=market_code,
        )
        policy = applicable_policy(session, effective)
        items = [_target_dict(r, effective, policy) for r in rows]
    filters_key = f"targets:{lifecycle_state or ''}:{product_version_id or ''}"
    return _paginate(items, context=effective, filters_key=filters_key, cursor=cursor, limit=limit)


@mode_router.get("/targets/{target_id}", operation_id="get_scale_target", response_model=TargetResponse)
def get_target_route(
    target_id: str,
    response: Response,
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        target = get_target(session, effective, target_id)
        set_etag(response, target.revision)
        return _target_dict(target, effective, applicable_policy(session, effective))


@mode_router.post(
    "/target-publication-previews",
    operation_id="create_scale_target_publication_preview",
    response_model=TargetPublicationPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_target_pub_preview_route(
    body: TargetPublicationPreviewRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    from decimal import Decimal as D

    with tenant_session(effective) as session:
        preview = create_publication_preview(
            session, effective,
            product_version_id=body.product_version_id,
            offer_version_id=body.offer_version_id,
            market_code=body.market_code,
            currency=body.currency,
            target_cpa=D(body.target_cpa) if body.target_cpa else None,
            breakeven_roas=D(body.breakeven_roas) if body.breakeven_roas else None,
            minimum_economic_spend=D(body.minimum_economic_spend) if body.minimum_economic_spend else None,
            idempotency_key=idempotency_key,
        )
        return {
            "id": preview.id,
            "product_version_id": preview.product_version_id,
            "offer_version_id": preview.offer_version_id,
            "market_code": preview.market_code,
            "currency": preview.currency,
            "target_cpa": str(preview.target_cpa) if preview.target_cpa else None,
            "breakeven_roas": str(preview.breakeven_roas) if preview.breakeven_roas else None,
            "minimum_economic_spend": str(preview.minimum_economic_spend) if preview.minimum_economic_spend else None,
            "affected_enrollment_count": preview.affected_enrollment_count,
            "policy_floor_check_passed": preview.policy_floor_check_passed,
            "impact_hash": preview.impact_hash,
            "expires_at": preview.expires_at,
            "created_at": preview.created_at,
        }


@mode_router.post(
    "/targets",
    operation_id="create_scale_target",
    response_model=TargetResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_target_route(
    body: TargetPublishRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        target = publish_target(
            session, effective,
            preview_id=body.publication_preview_id,
            idempotency_key=idempotency_key,
        )
        return _target_dict(target, effective, applicable_policy(session, effective))


@mode_router.post(
    "/targets/{target_id}/withdrawal-previews",
    operation_id="create_scale_target_withdrawal_preview",
    response_model=TargetWithdrawalPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_target_wd_preview_route(
    target_id: str,
    body: TargetWithdrawalPreviewRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        preview = create_withdrawal_preview(
            session, effective,
            target_id=target_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        return {
            "id": preview.id,
            "target_version_id": preview.target_version_id,
            "reason": preview.reason,
            "affected_enrollment_count": preview.affected_enrollment_count,
            "dependent_advice_thread_count": preview.dependent_advice_thread_count,
            "impact_hash": preview.impact_hash,
            "expires_at": preview.expires_at,
            "created_at": preview.created_at,
        }


@mode_router.post(
    "/targets/{target_id}/withdraw",
    operation_id="withdraw_scale_target",
    response_model=TargetResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def withdraw_target_route(
    target_id: str,
    body: TargetWithdrawRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        target = withdraw_target(
            session, effective,
            preview_id=body.withdrawal_preview_id,
            idempotency_key=idempotency_key,
        )
        return _target_dict(target, effective, applicable_policy(session, effective))


# ---------------------------------------------------------------------------
# Command-outcome recovery -- spec §4.2 (Issue #87)
# ---------------------------------------------------------------------------


@mode_router.get(
    "/command-outcomes/{identity}",
    operation_id="get_scale_command_outcome",
    response_model=CommandOutcomeResponse,
)
def get_command_outcome(
    identity: str,
    command_kind: str | None = Query(default=None),
    effective: TenantContext = Depends(_mode_effective_context),
):
    with tenant_session(effective) as session:
        outcome = lookup_command_outcome(session, effective, identity, command_kind)
        return {
            "identity_hash": outcome.identity_hash,
            "command_type": outcome.command_type,
            "dedup_key": outcome.dedup_key,
            "result_resource_id": outcome.result_resource_id,
            "result_resource_type": outcome.result_resource_type,
            "request_fingerprint": outcome.request_fingerprint,
            "initiator_id": outcome.initiator_id,
            "created_at": outcome.created_at,
        }


# ---------------------------------------------------------------------------
# Exports -- spec §16 (Issue #87)
# ---------------------------------------------------------------------------


@mode_router.post(
    "/exports",
    operation_id="create_scale_export",
    response_model=ExportResponse,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_export_route(
    body: ExportCreateRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        result = create_export(
            session, effective,
            enrollment_id=body.enrollment_id,
            episode_id=body.episode_id,
            export_kind=body.export_kind,
            snapshot_id=body.snapshot_id,
            evaluation_id=body.evaluation_id,
            idempotency_key=idempotency_key,
        )
        export = session.get(ScaleExportRecord, result.export_id)
        return {
            "id": export.id,
            "operation_id": export.operation_id,
            "enrollment_id": export.enrollment_id,
            "episode_id": export.episode_id,
            "export_kind": export.export_kind,
            "requested_by": export.requested_by,
            "requested_at": export.requested_at,
            "completed_at": export.completed_at,
            "result_object_reference": export.result_object_reference,
        }


@mode_router.post(
    "/exports/{export_id}/download-intents",
    operation_id="create_scale_export_download_intent",
    response_model=ExportDownloadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_export_download_intent_route(
    export_id: str,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        intent = create_export_download_intent(
            session, effective,
            export_id=export_id,
            idempotency_key=idempotency_key,
        )
        return {
            "intent_id": intent.intent_id,
            "expires_at": intent.expires_at,
        }


def _candidate_dict(candidate: ScaleEnrollmentCandidateRecord) -> dict:
    return {
        "id": candidate.id,
        "meta_mode": candidate.meta_mode,
        "scenario_scope_key": candidate.scenario_scope_key,
        "winner_decision_id": candidate.winner_decision_id,
        "delivery_target_id": candidate.delivery_target_id,
        "candidate_fingerprint": candidate.candidate_fingerprint,
        "expires_at": candidate.expires_at,
        "consumed_at": candidate.consumed_at,
        "created_at": candidate.created_at,
    }


@mode_router.get(
    "/enrollment-candidates",
    operation_id="list_scale_enrollment_candidates",
)
def list_enrollment_candidates(
    effective: TenantContext = Depends(_mode_effective_context),
):
    """Discovery only (spec §4.4) -- never gates any command. Returns
    confirmed Winner Decisions currently eligible for enrollment that have
    no live Enrollment yet; a caller still names the exact pair to
    :func:`create_scale_enrollment_candidate` below, which independently
    re-validates everything."""
    with tenant_session(effective) as session:
        items = list_eligible_candidates(session, effective)
        return {
            "items": [
                {
                    "evidence_origin": item.evidence_origin,
                    "delivery_targets": list(item.delivery_targets),
                    "delivery_unavailable_reason": item.delivery_unavailable_reason,
                    "decision_id": item.decision_id,
                    "confirmation_id": item.confirmation_id,
                    "experiment_version_id": item.experiment_version_id,
                    "candidate_id": item.candidate_id,
                    "asset_version_id": item.asset_version_id,
                }
                for item in items
            ]
        }


@mode_router.post(
    "/enrollment-candidates",
    operation_id="create_scale_enrollment_candidate",
    response_model=CandidateResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_enrollment_candidate_route(
    body: CandidateCreateRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(effective) as session:
        candidate = create_enrollment_candidate(
            session, effective,
            decision_id=body.winner_decision_id,
            delivery_target_id=body.delivery_target_id,
            idempotency_key=idempotency_key,
        )
        return _candidate_dict(candidate)


# ---------------------------------------------------------------------------
# DeliveryTarget entry (#129): pick an already-observed ad
# ---------------------------------------------------------------------------


def _observed_ads_dict(listing: ObservedAdListing) -> dict:
    return {
        "items": [
            {
                "meta_connection_id": ad.meta_connection_id,
                "ad_account_id": ad.ad_account_id,
                "campaign_remote_id": ad.campaign_remote_id,
                "adset_remote_id": ad.adset_remote_id,
                "ad_remote_id": ad.ad_remote_id,
                "ad_creative_id": ad.ad_creative_id,
                "source_type": ad.source_type,
                "asset_version_ids": list(ad.asset_version_ids),
            }
            for ad in listing.ads
        ],
        "unavailable": [
            {"meta_connection_id": connection_id, "reason": reason}
            for connection_id, reason in listing.unavailable
        ],
    }


def _delivery_target_dict(target) -> dict:
    return {
        "id": target.id,
        "meta_mode": target.meta_mode,
        "meta_connection_id": target.meta_connection_id,
        "ad_account_id": target.ad_account_id,
        "campaign_remote_id": target.campaign_remote_id,
        "adset_remote_id": target.adset_remote_id,
        "ad_remote_id": target.ad_remote_id,
        "ad_creative_id": target.ad_creative_id,
        "creative_asset_version_id": target.creative_asset_version_id,
        "market_code": target.market_code,
        "placement": target.placement,
        "offer_version_id": target.offer_version_id,
        "landing_page_evidence_hash": target.landing_page_evidence_hash,
        "attribution_setting": target.attribution_setting,
        "contract_generation_id": target.contract_generation_id,
        "source_type": target.source_type,
        "observed_at": target.observed_at,
        "expires_at": target.expires_at,
        "created_at": target.created_at,
    }


@mode_router.get(
    "/observed-ads",
    operation_id="list_scale_observed_ads",
    response_model=ObservedAdListResponse,
)
def list_observed_ads_route(
    effective: TenantContext = Depends(_mode_effective_context),
    meta_connection_id: str | None = Query(default=None, min_length=1, max_length=64),
):
    """The ads the Workspace's active connections in this mode have
    observed, for picking a Winner's DeliveryTarget. Reads only."""
    with tenant_session(effective) as session:
        return _observed_ads_dict(
            list_observed_ads(session, effective, connection_id=meta_connection_id)
        )


@mode_router.post(
    "/delivery-targets",
    operation_id="create_scale_delivery_target",
    response_model=DeliveryTargetResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_delivery_target_route(
    body: DeliveryTargetCreateRequest,
    effective: TenantContext = Depends(_mode_effective_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    """Create a Winner's DeliveryTarget from one observed ad (#129). Owners
    and Members alike; the server resolves the slot from recorded evidence
    and returns a typed reason when the ad does not match the Winner."""
    with tenant_session(effective) as session:
        target = create_delivery_target_from_ad(
            session, effective,
            decision_id=body.winner_decision_id,
            connection_id=body.meta_connection_id,
            ad_remote_id=body.ad_remote_id,
            idempotency_key=idempotency_key,
        )
        return _delivery_target_dict(target)


# Mount every mode-scoped route onto the root router at ``/{meta_mode}``
# (spec §3.11). Must run after every ``@mode_router...`` registration above.
router.include_router(mode_router, prefix="/{meta_mode}")


# ---------------------------------------------------------------------------
# Problem handlers (spec §4.2): every Scale domain error maps to the same RFC
# 9457 ``application/problem+json`` response the app-wide handlers build
# (``app.library.api._request_problem``: ``trace_id``, ``retryable``,
# ``instance``, ``field_errors`` and the ``X-Trace-Id`` header), which is the
# shape the BFF's ``ProblemDetails`` validator requires (#368). The 428/400/409
# precondition-mismatch triad is instead produced by
# ``app.platform.etag.extract_if_match`` (428/400) and the domain layer's own
# ``expected_revision`` check re-raising as ``EnrollmentLifecycleError(
# "revision_conflict", ...)`` (mapped to 409 below) -- both surface through
# the app-wide ``HTTPException``/generic-exception handlers
# ``app.library.api.register_problem_handlers`` already registers in
# ``app.main``, so this module registers no separate 428/400 handling of its
# own.
# ---------------------------------------------------------------------------


def _problem(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return _request_problem(request, status, code, message)


_ENROLLMENT_ERROR_STATUS = {
    "enrollment_not_found": 404,
    "episode_not_found": 404,
    "invalid_transition": 409,
    "revision_conflict": 409,
    "duplicate_binding": 409,
    "draft_creation_conflict": 409,
    "pause_conflict": 409,
    "resume_conflict": 409,
    "close_conflict": 409,
    "withdrawal_conflict": 409,
    "no_delivery_closure_conflict": 409,
    "episode_mismatch": 409,
    "episode_already_closed": 409,
    "not_withdrawable": 409,
    "scenario_scope_required": 422,
    "scenario_scope_forbidden": 422,
    "remote_binding_shape_invalid": 422,
    "required_field_missing": 422,
    "meta_mode_required": 400,
    # -- Issue #86 Task 11: preview-gated activate/resume -------------------
    "candidate_lineage_required": 422,
    "preview_transition_kind_mismatch": 409,
    "preview_lineage_stale": 409,
    "metric_contract_set_missing": 422,
    "metric_contract_set_ambiguous": 422,
    "contract_generation_not_found": 404,
    "workspace_not_found": 404,
}

_ADVICE_ERROR_STATUS = {
    "meta_mode_required": 400,
    "episode_not_found": 404,
    "enrollment_not_found": 404,
    "thread_not_found": 404,
    "occurrence_not_found": 404,
    "evaluation_not_found": 404,
    "policy_not_found": 404,
    "candidate_not_persisted": 404,
    "evaluation_episode_mismatch": 409,
    "thread_not_active": 409,
    "episode_closed": 409,
    "enrollment_not_actionable": 409,
    "wrong_advice_type": 422,
    "unsupported_disposition_kind": 422,
    "snooze_until_not_future": 422,
}

_TARGET_ERROR_STATUS = {
    "owner_required": 403,
    "at_least_one_target": 422,
    "target_not_found": 404,
    "preview_expired": 409,
    "preview_already_consumed": 409,
    "preview_hash_mismatch": 409,
    "invalid_lifecycle": 409,
    "no_controlled_mock_enrollment": 409,
    "idempotency_result_missing": 400,
    # #134: the minimum economic spend against the platform floor.
    "minimum_economic_spend_required": 422,
    "minimum_economic_spend_below_platform_floor": 422,
    "spend_floor_currency_unsupported": 422,
    "no_eligible_policy": 409,
}

#: The DeliveryTarget entry's typed reasons (#129) and the contract-set and
#: provenance codes of ``app.scale.delivery_targets`` it can surface.
_DELIVERY_TARGET_ERROR_STATUS = {
    "meta_mode_required": 400,
    "idempotency_result_missing": 400,
    "forbidden": 403,
    "connection_not_found": 404,
    "ad_not_observed": 404,
    "delivery_target_not_found": 404,
    "delivery_configuration_not_observed": 409,
    "delivery_target_expired": 409,
    "scale_contract_generation_conflict": 409,
    "scale_contract_set_drift": 409,
    "target_creative_mismatch": 409,
    "target_market_mismatch": 409,
    "target_placement_mismatch": 409,
    "target_offer_mismatch": 409,
    "target_landing_page_mismatch": 409,
    "target_attribution_mismatch": 409,
    "creative_unmapped": 422,
    "creative_ambiguous": 422,
    "provenance_unresolved": 422,
    "provenance_ambiguous": 422,
    "attribution_contract_unresolved": 422,
}

_BREAKDOWN_ERROR_STATUS = {
    "enrollment_not_found": 404,
    "snapshot_not_found": 404,
    "invalid_breakdown_type": 422,
    "snapshot_not_sealed": 422,
}

_EXPORT_ERROR_STATUS = {
    "enrollment_not_found": 404,
    "episode_not_found": 404,
    "export_not_found": 404,
    "export_not_complete": 409,
    "idempotency_result_missing": 400,
}


def register_scale_v1_problem_handlers(app) -> None:
    """Register RFC 9457 handlers for the Scale domain error types this
    router's routes can raise."""

    @app.exception_handler(EnrollmentLifecycleError)
    async def _enrollment_error_handler(request: Request, exc: EnrollmentLifecycleError):
        return _problem(request, _ENROLLMENT_ERROR_STATUS.get(exc.code, 422), exc.code, str(exc))

    @app.exception_handler(AdviceLifecycleError)
    async def _advice_error_handler(request: Request, exc: AdviceLifecycleError):
        return _problem(request, _ADVICE_ERROR_STATUS.get(exc.code, 422), exc.code, str(exc))

    # -- Issue #86 Task 9: Enrollment Candidate / lineage-gate errors -------

    @app.exception_handler(WinnerIneligibleError)
    async def _winner_ineligible_handler(request: Request, exc: WinnerIneligibleError):
        codes = [f.code for f in exc.findings]
        return _problem(request, 422, "winner_ineligible", f"Winner ineligible: {codes}")

    @app.exception_handler(DeliveryTargetNotFoundError)
    async def _target_not_found_handler(request: Request, exc: DeliveryTargetNotFoundError):
        return _problem(request, 404, "delivery_target_not_found", str(exc))

    @app.exception_handler(DeliveryTargetExpiredError)
    async def _target_expired_handler(request: Request, exc: DeliveryTargetExpiredError):
        return _problem(request, 409, "delivery_target_expired", str(exc))

    @app.exception_handler(WinnerTargetMismatchError)
    async def _winner_target_mismatch_handler(request: Request, exc: WinnerTargetMismatchError):
        return _problem(request, 409, "winner_target_mismatch", str(exc))

    @app.exception_handler(CandidateNotFoundError)
    async def _candidate_not_found_handler(request: Request, exc: CandidateNotFoundError):
        return _problem(request, 404, "candidate_not_found", str(exc))

    @app.exception_handler(CandidateAlreadyConsumedError)
    async def _candidate_consumed_handler(request: Request, exc: CandidateAlreadyConsumedError):
        return _problem(request, 409, "candidate_already_consumed", str(exc))

    @app.exception_handler(CandidateExpiredError)
    async def _candidate_expired_handler(request: Request, exc: CandidateExpiredError):
        return _problem(request, 409, "candidate_expired", str(exc))

    @app.exception_handler(CandidateFingerprintDriftError)
    async def _candidate_drift_handler(request: Request, exc: CandidateFingerprintDriftError):
        return _problem(request, 409, "candidate_fingerprint_drift", str(exc))

    # -- Issue #86 Task 10/11: ActivationPreview + command-journal errors ---

    @app.exception_handler(PreviewEnrollmentNotFoundError)
    async def _preview_enrollment_not_found_handler(
        request: Request, exc: PreviewEnrollmentNotFoundError,
    ):
        return _problem(request, 404, "enrollment_not_found", str(exc))

    @app.exception_handler(PreviewLifecycleInvalidError)
    async def _preview_lifecycle_invalid_handler(request: Request, exc: PreviewLifecycleInvalidError):
        return _problem(request, 422, "invalid_transition", str(exc))

    @app.exception_handler(PreviewExpiredError)
    async def _preview_expired_handler(request: Request, exc: PreviewExpiredError):
        return _problem(request, 409, "preview_expired", str(exc))

    @app.exception_handler(PreviewAlreadyConsumedError)
    async def _preview_consumed_handler(request: Request, exc: PreviewAlreadyConsumedError):
        return _problem(request, 409, "preview_already_consumed", str(exc))

    @app.exception_handler(PreviewEnrollmentMismatchError)
    async def _preview_mismatch_handler(request: Request, exc: PreviewEnrollmentMismatchError):
        return _problem(request, 409, "preview_enrollment_mismatch", str(exc))

    @app.exception_handler(PreviewRevisionMismatchError)
    async def _preview_revision_mismatch_handler(request: Request, exc: PreviewRevisionMismatchError):
        return _problem(request, 409, "preview_revision_mismatch", str(exc))

    @app.exception_handler(IdempotencyConflictError)
    async def _idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError):
        return _problem(request, 409, "idempotency_conflict", str(exc))

    # -- Issue #87 Task 6: Target / Breakdown / CommandOutcome / Export errors -

    @app.exception_handler(TargetLifecycleError)
    async def _target_lifecycle(request: Request, exc: TargetLifecycleError):
        extra = {"params": exc.params} if exc.params else {}
        return _request_problem(
            request, _TARGET_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc), **extra,
        )

    @app.exception_handler(DeliveryTargetError)
    async def _delivery_target_error(request: Request, exc: DeliveryTargetError):
        return _problem(request, _DELIVERY_TARGET_ERROR_STATUS.get(exc.code, 422), exc.code, str(exc))

    @app.exception_handler(BreakdownNotAvailableError)
    async def _breakdown_error(request: Request, exc: BreakdownNotAvailableError):
        return _problem(request, _BREAKDOWN_ERROR_STATUS.get(exc.code, 422), exc.code, str(exc))

    @app.exception_handler(CommandOutcomeNotFoundError)
    async def _command_not_found(request: Request, exc: CommandOutcomeNotFoundError):
        return _problem(request, 404, "command_outcome_not_found", str(exc))

    @app.exception_handler(ExportError)
    async def _export_error(request: Request, exc: ExportError):
        return _problem(request, _EXPORT_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc))
