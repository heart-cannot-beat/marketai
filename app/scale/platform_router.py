"""Platform-scoped mirror of the Scale HTTP surface, at
``/api/platform/v1/scale/{meta_mode}/...`` (Issue #86, Task 6).

Spec: docs/superpowers/specs/2026-09-14-harden-scale-enrollment-lineage-
design.md §3.7, §3.10-§3.11.

This module exists so a platform operator, authenticated with a genuine
``PlatformOperatorJWTVerifier``-verified bearer token (Task 3,
``app.platform.platform_operator_identity``), can exercise the *complete*
mode-scoped Scale resource tree -- including lifecycle management --
against ``internal_validation`` (the attested-evidence pipeline mode, ADR
0009) as well as every other Meta mode. Without this, an
``internal_validation`` Enrollment could be activated by a platform
operator but never paused, resumed or closed (spec §3.7).

**Distinct from every other platform router in this codebase.** Every
other platform-admin route (``app.meta.platform_api``) uses the
transitional ``request_platform_context()`` dependency: JWT-primary with a
deprecated static-bearer-token fallback (``PLATFORM_ADMIN_TOKEN``), because
those routes predate the JWT verifier. This module uses
``require_platform_operator_jwt`` (Task 3) directly and exclusively -- no
static-token fallback exists or will ever be added here (spec §3.8's
"Static Admin Token Retirement" table: "New Scale platform routes" are
"JWT-only through ``require_platform_operator_jwt()``. Static bearer tokens
are always rejected."). If ``PLATFORM_OPERATOR_JWKS_URL`` is not
configured, every route in this module returns 503 rather than silently
falling back to a static token, since a static secret carries no
``workspace_id`` claim this module could safely bind onto a
``TenantContext`` (PO22).

**Route topology mirrors ``app.scale.v1_router``'s mode-scoped tree exactly**
(spec §3.11): a root router (``platform_scale_root``, prefix
``/api/platform/v1/scale``) mounts a mode router (``platform_scale_mode``)
at ``/{meta_mode}``. There is no platform-scoped ``available-meta-modes``
route -- ``list_available_meta_modes()`` (``app.platform.meta_mode_auth``)
already returns every mode for a ``PLATFORM_OPERATOR``/``SYSTEM`` identity,
so there is nothing for a platform-scoped discovery endpoint to narrow.

Every handler below delegates to the exact same ``app.scale.enrollment``/
``app.scale.advice`` domain functions ``app.scale.v1_router`` uses, and
reuses that module's private serialization/pagination helpers
(``_enrollment_dict``, ``_paginate``, etc.) rather than re-implementing
them, so the two routers can never drift on what an Enrollment/Episode/
Advice-thread response looks like. What differs between the two routers is
*only* the identity/authorization seam: ``_platform_context`` below in
place of ``app.scale.v1_router._mode_effective_context``.
"""
from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.meta.models import MetaObservationSyncScheduleRecord
from app.platform.etag import extract_if_match, set_etag
from app.platform.meta_mode_auth import MetaModeNotPermitted, require_meta_mode
from app.platform.platform_operator_identity import require_platform_operator_jwt
from app.platform.tenancy import IdentityKind, TenantContext
from app.platform.workspace_admission import WorkspaceAdmissionReader, WorkspaceNotFoundError

from .advice import convert_occurrence_to_draft, convert_occurrence_to_test_plan, create_disposition, create_snooze
from .activation_preview import create_activation_preview
from .breakdowns import query_snapshot_breakdowns
from .candidates import create_enrollment_candidate, list_eligible_candidates
from .command_journal import lookup_command_outcome
from .enrollment import activate_enrollment, close_enrollment, create_draft_enrollment, pause_enrollment, resume_enrollment
from .exports import create_export, create_export_download_intent
from .delivery_targets import ensure_metric_contract_set
from .observed_ads import create_delivery_target_from_ad, list_observed_ads
from .models import (
    ScaleAdviceConversionRecord,
    ScaleAdviceDispositionRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceSnoozeRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationSignalRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleExportRecord,
    ScaleSnapshotQualityIssueRecord,
)
from .schemas import (
    ActivationPreviewCreateRequest,
    ActivationPreviewResponse,
    AdviceDispositionRequest,
    AdviceDispositionResponse,
    AdviceSnoozeRequest,
    AdviceSnoozeResponse,
    AdviceThreadListResponse,
    BreakdownResponse,
    CandidateCreateRequest,
    CandidateResponse,
    CommandOutcomeResponse,
    ConversionRequest,
    ConversionResponse,
    DeliveryTargetCreateRequest,
    DeliveryTargetResponse,
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
    ExportDownloadIntentResponse,
    ExportResponse,
    MetricContractSetCreateRequest,
    MetricContractSetGenerationResponse,
    MetricContractSetResponse,
    ObservedAdListResponse,
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
    create_publication_preview,
    create_withdrawal_preview,
    get_target,
    list_targets,
    publish_target,
    resolve_target_contexts,
    withdraw_target,
)
from .types import AdviceType
from .v1_router import (
    _META_MODE_PATTERN,
    _candidate_dict,
    _delivery_target_dict,
    _enrollment_dict,
    _episode_dict,
    _evidence_detail_dict,
    _limit_query,
    _observed_ads_dict,
    _paginate,
    _preview_dict,
    _target_dict,
    _thread_dict,
)

PLATFORM_EXECUTOR = "system:platform_operator"

platform_scale_root = APIRouter(prefix="/api/platform/v1/scale", tags=["scale-platform"])
platform_scale_mode = APIRouter(tags=["scale-platform"])


# ---------------------------------------------------------------------------
# Platform-operator identity + Meta-mode gate
# ---------------------------------------------------------------------------


async def _platform_context(
    request: Request,
    meta_mode: str = Path(
        ..., pattern=_META_MODE_PATTERN, description="Meta evidence mode (URL path segment).",
    ),
) -> TenantContext:
    """Verify the platform-operator JWT, admit the token's own
    ``workspace_id`` (never a body/header-supplied one -- PO22), and
    authorize+bind the path's ``meta_mode`` (spec §3.7).

    Distinct failure modes, in order:

    * No/invalid/wrong-typ/wrong-issuer/... bearer token --
      ``require_platform_operator_jwt`` itself raises 401 (token problem) or
      503 (JWKS not configured/stale) -- see that function's own docstring.
    * The token's ``workspace_id`` does not exist in the domain projection --
      404 (mirrors every other cross-Workspace not-found in this codebase;
      never leaks whether that ID exists for the request's own tenant).
    * The Workspace is suspended -- 403.
    * ``meta_mode`` is not one this identity may use for this Workspace
      (``require_meta_mode``, e.g. ``internal_validation`` on a non-Platform
      Workspace) -- 403; an unrecognized ``meta_mode`` never reaches here at
      all (FastAPI's own ``Path(pattern=...)`` rejects it with 422 first).
    """
    identity = await require_platform_operator_jwt(request)

    admission_reader: WorkspaceAdmissionReader | None = getattr(
        request.app.state, "admission_reader", None,
    )
    if admission_reader is None:
        raise HTTPException(
            503,
            {
                "code": "platform_scale_not_configured",
                "message": "Workspace admission is not configured.",
            },
        )

    try:
        admission = await admission_reader.read(identity.workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(
            404, {"code": "workspace_not_found", "message": identity.workspace_id},
        ) from exc

    if admission.suspended:
        raise HTTPException(403, {"code": "workspace_suspended", "message": identity.workspace_id})

    ctx = TenantContext(
        workspace_id=identity.workspace_id,
        role="system",
        initiator_id=identity.sub,
        executor=PLATFORM_EXECUTOR,
        authorization_epoch=admission.authorization_epoch,
        identity_kind=IdentityKind.PLATFORM_OPERATOR,
        is_platform_workspace=admission.is_platform_workspace,
    )
    try:
        require_meta_mode(ctx, meta_mode)
    except MetaModeNotPermitted as exc:
        status = 400 if exc.code == "invalid_meta_mode" else 403
        raise HTTPException(status, {"code": exc.code, "message": exc.detail}) from exc

    return replace(ctx, meta_mode=meta_mode)


# ---------------------------------------------------------------------------
# Enrollment lifecycle
# ---------------------------------------------------------------------------


@platform_scale_mode.post(
    "/enrollments",
    operation_id="platform_create_scale_enrollment",
    response_model=EnrollmentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_enrollment(
    body: EnrollmentCreateRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        enrollment = create_draft_enrollment(
            session, context,
            enrollment_candidate_id=body.enrollment_candidate_id,
            idempotency_key=idempotency_key,
        )
        return _enrollment_dict(enrollment)


@platform_scale_mode.get(
    "/enrollments", operation_id="platform_list_scale_enrollments", response_model=EnrollmentListResponse,
)
def platform_list_enrollments(
    lifecycle: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        stmt = select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        )
        if lifecycle:
            stmt = stmt.filter(ScaleEnrollmentRecord.lifecycle == lifecycle)
        rows = session.execute(stmt.order_by(ScaleEnrollmentRecord.created_at.desc())).scalars().all()
        items = [_enrollment_dict(row) for row in rows]
    filters_key = f"enrollments:{lifecycle or ''}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@platform_scale_mode.get(
    "/enrollments/{enrollment_id}",
    operation_id="platform_get_scale_enrollment",
    response_model=EnrollmentResponse,
)
def platform_get_enrollment(
    enrollment_id: str,
    response: Response,
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        enrollment = session.execute(
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode, id=enrollment_id,
            )
        ).scalar_one_or_none()
        if enrollment is None:
            raise HTTPException(404, {"code": "enrollment_not_found", "message": enrollment_id})
        payload = _enrollment_dict(enrollment)
    set_etag(response, payload["revision"])
    return payload


@platform_scale_mode.put(
    "/enrollments/{enrollment_id}/activate",
    operation_id="platform_activate_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_activate_enrollment(
    enrollment_id: str,
    body: EnrollmentActivateRequest,
    request: Request,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        enrollment, _episode = activate_enrollment(
            session, context,
            enrollment_id=enrollment_id,
            activation_preview_id=body.activation_preview_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return _enrollment_dict(enrollment)


@platform_scale_mode.put(
    "/enrollments/{enrollment_id}/pause",
    operation_id="platform_pause_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_pause_enrollment(
    enrollment_id: str,
    body: EnrollmentPauseRequest,
    request: Request,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        enrollment = pause_enrollment(
            session, context, enrollment_id,
            expected_revision=expected_revision, idempotency_key=idempotency_key, reason=body.reason,
        )
        return _enrollment_dict(enrollment)


@platform_scale_mode.put(
    "/enrollments/{enrollment_id}/resume",
    operation_id="platform_resume_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_resume_enrollment(
    enrollment_id: str,
    body: EnrollmentResumeRequest,
    request: Request,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        enrollment, _episode = resume_enrollment(
            session, context,
            enrollment_id=enrollment_id,
            activation_preview_id=body.activation_preview_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return _enrollment_dict(enrollment)


@platform_scale_mode.put(
    "/enrollments/{enrollment_id}/close",
    operation_id="platform_close_scale_enrollment",
    response_model=EnrollmentResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_close_enrollment(
    enrollment_id: str,
    body: EnrollmentCloseRequest,
    request: Request,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        enrollment = close_enrollment(
            session, context, enrollment_id,
            expected_revision=expected_revision, idempotency_key=idempotency_key, reason=body.reason,
        )
        return _enrollment_dict(enrollment)


@platform_scale_mode.post(
    "/enrollments/{enrollment_id}/activation-previews",
    operation_id="platform_create_scale_activation_preview",
    response_model=ActivationPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_activation_preview(
    enrollment_id: str,
    body: ActivationPreviewCreateRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        preview = create_activation_preview(
            session, context,
            enrollment_id=enrollment_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
        return _preview_dict(preview)


@platform_scale_mode.get(
    "/enrollments/{enrollment_id}/episodes/{episode_id}",
    operation_id="platform_get_scale_episode",
    response_model=EpisodeResponse,
)
def platform_get_episode(
    enrollment_id: str,
    episode_id: str,
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        episode = session.execute(
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode,
                id=episode_id, enrollment_id=enrollment_id,
            )
        ).scalar_one_or_none()
        if episode is None:
            raise HTTPException(404, {"code": "episode_not_found", "message": episode_id})
        return _episode_dict(episode)


@platform_scale_mode.get(
    "/enrollments/{enrollment_id}/evidence",
    operation_id="platform_get_scale_enrollment_evidence",
    response_model=EnrollmentEvidenceResponse,
)
def platform_get_enrollment_evidence(
    enrollment_id: str,
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        enrollment = session.execute(
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode, id=enrollment_id,
            )
        ).scalar_one_or_none()
        if enrollment is None:
            raise HTTPException(404, {"code": "enrollment_not_found", "message": enrollment_id})
        if enrollment.current_episode_id is None:
            raise HTTPException(404, {"code": "no_active_episode", "message": enrollment_id})
        projection = session.execute(
            select(ScaleEpisodeProjectionRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode,
                episode_id=enrollment.current_episode_id,
            )
        ).scalar_one_or_none()
        if projection is None:
            raise HTTPException(
                404, {"code": "evidence_not_available", "message": "no Evaluation has projected yet"},
            )
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

        return {
            "enrollment_id": enrollment.id,
            "episode_id": projection.episode_id,
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
        }


@platform_scale_mode.get(
    "/enrollments/{enrollment_id}/breakdowns",
    operation_id="platform_get_scale_enrollment_breakdowns",
    response_model=BreakdownResponse,
)
def platform_get_enrollment_breakdowns(
    enrollment_id: str,
    snapshot_id: str = Query(..., min_length=1, description="Immutable snapshot to pin breakdowns to"),
    breakdown_type: str = Query(..., min_length=1, description="placement, country, or age_gender"),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        page = query_snapshot_breakdowns(
            session, context,
            enrollment_id=enrollment_id,
            snapshot_id=snapshot_id,
            breakdown_type=breakdown_type,
            cursor=cursor,
            limit=limit,
        )
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
# Advice
# ---------------------------------------------------------------------------


@platform_scale_mode.get(
    "/advice-threads", operation_id="platform_list_scale_advice_threads", response_model=AdviceThreadListResponse,
)
def platform_list_advice_threads(
    enrollment_id: str | None = Query(default=None),
    lifecycle: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        stmt = select(ScaleAdviceThreadRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        )
        if enrollment_id:
            stmt = stmt.filter(ScaleAdviceThreadRecord.enrollment_id == enrollment_id)
        if lifecycle:
            stmt = stmt.filter(ScaleAdviceThreadRecord.lifecycle == lifecycle)
        rows = session.execute(stmt.order_by(ScaleAdviceThreadRecord.opened_at.desc())).scalars().all()
        items = [_thread_dict(row) for row in rows]
    filters_key = f"advice-threads:{enrollment_id or ''}:{lifecycle or ''}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@platform_scale_mode.post(
    "/advice-threads/{thread_id}/disposition",
    operation_id="platform_create_scale_advice_disposition",
    response_model=AdviceDispositionResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_advice_disposition(
    thread_id: str,
    body: AdviceDispositionRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    actor_id = context.initiator_id or context.executor
    with tenant_session(context) as session:
        disposition_id = create_disposition(
            session, context, thread_id, body.kind,
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


@platform_scale_mode.post(
    "/advice-threads/{thread_id}/snooze",
    operation_id="platform_create_scale_advice_snooze",
    response_model=AdviceSnoozeResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_advice_snooze(
    thread_id: str,
    body: AdviceSnoozeRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    actor_id = context.initiator_id or context.executor
    with tenant_session(context) as session:
        snooze_id = create_snooze(
            session, context, thread_id, body.until, actor_id=actor_id, reason=body.reason,
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


@platform_scale_mode.post(
    "/advice-occurrences/{occurrence_id}/convert",
    operation_id="platform_convert_scale_advice_occurrence",
    response_model=ConversionResponse,
    status_code=201,
)
def platform_convert_advice_occurrence(
    occurrence_id: str,
    body: ConversionRequest,
    context: TenantContext = Depends(_platform_context),
):
    initiator_id = context.initiator_id or context.executor
    with tenant_session(context) as session:
        occurrence = session.execute(
            select(ScaleAdviceOccurrenceRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=context.meta_mode, id=occurrence_id,
            )
        ).scalar_one_or_none()
        if occurrence is None:
            raise HTTPException(404, {"code": "occurrence_not_found", "message": occurrence_id})

        if occurrence.advice_type == AdviceType.REFRESH_CREATIVE.value:
            conversion_id = convert_occurrence_to_draft(
                session, context, occurrence_id,
                initiator_id=initiator_id, provider=body.provider, model=body.model,
                refresh_axis=body.refresh_axis,
            )
        elif occurrence.advice_type == AdviceType.CONSIDER_SCALE_TEST.value:
            conversion_id = convert_occurrence_to_test_plan(
                session, context, occurrence_id, initiator_id=initiator_id,
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
# Targets -- spec §10, §21 (Issue #87)
# ---------------------------------------------------------------------------


@platform_scale_mode.get("/target-contexts/current", operation_id="platform_list_scale_target_contexts")
def platform_list_target_contexts(context: TenantContext = Depends(_platform_context)):
    with tenant_session(context) as session:
        contexts = resolve_target_contexts(session, context)
    return {"items": contexts}


@platform_scale_mode.get("/targets", operation_id="platform_list_scale_targets", response_model=TargetListResponse)
def platform_list_targets_route(
    lifecycle_state: str | None = Query(default=None),
    product_version_id: str | None = Query(default=None),
    offer_version_id: str | None = Query(default=None),
    market_code: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        rows = list_targets(
            session, context,
            lifecycle_state=lifecycle_state,
            product_version_id=product_version_id,
            offer_version_id=offer_version_id,
            market_code=market_code,
        )
        items = [_target_dict(r, context) for r in rows]
    filters_key = f"targets:{lifecycle_state or ''}:{product_version_id or ''}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@platform_scale_mode.get("/targets/{target_id}", operation_id="platform_get_scale_target", response_model=TargetResponse)
def platform_get_target_route(
    target_id: str,
    response: Response,
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        target = get_target(session, context, target_id)
        set_etag(response, target.revision)
        return _target_dict(target, context)


@platform_scale_mode.post(
    "/target-publication-previews",
    operation_id="platform_create_scale_target_publication_preview",
    response_model=TargetPublicationPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_target_pub_preview_route(
    body: TargetPublicationPreviewRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    from decimal import Decimal as D

    with tenant_session(context) as session:
        preview = create_publication_preview(
            session, context,
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


@platform_scale_mode.post(
    "/targets",
    operation_id="platform_create_scale_target",
    response_model=TargetResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_target_route(
    body: TargetPublishRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        target = publish_target(
            session, context,
            preview_id=body.publication_preview_id,
            idempotency_key=idempotency_key,
        )
        return _target_dict(target, context)


@platform_scale_mode.post(
    "/targets/{target_id}/withdrawal-previews",
    operation_id="platform_create_scale_target_withdrawal_preview",
    response_model=TargetWithdrawalPreviewResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_target_wd_preview_route(
    target_id: str,
    body: TargetWithdrawalPreviewRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        preview = create_withdrawal_preview(
            session, context,
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


@platform_scale_mode.post(
    "/targets/{target_id}/withdraw",
    operation_id="platform_withdraw_scale_target",
    response_model=TargetResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_withdraw_target_route(
    target_id: str,
    body: TargetWithdrawRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        target = withdraw_target(
            session, context,
            preview_id=body.withdrawal_preview_id,
            idempotency_key=idempotency_key,
        )
        return _target_dict(target, context)


# ---------------------------------------------------------------------------
# Command-outcome recovery -- spec §4.2 (Issue #87)
# ---------------------------------------------------------------------------


@platform_scale_mode.get(
    "/command-outcomes/{identity}",
    operation_id="platform_get_scale_command_outcome",
    response_model=CommandOutcomeResponse,
)
def platform_get_command_outcome(
    identity: str,
    command_kind: str | None = Query(default=None),
    context: TenantContext = Depends(_platform_context),
):
    with tenant_session(context) as session:
        outcome = lookup_command_outcome(session, context, identity, command_kind)
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


@platform_scale_mode.post(
    "/exports",
    operation_id="platform_create_scale_export",
    response_model=ExportResponse,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_export_route(
    body: ExportCreateRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        result = create_export(
            session, context,
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


@platform_scale_mode.post(
    "/exports/{export_id}/download-intents",
    operation_id="platform_create_scale_export_download_intent",
    response_model=ExportDownloadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_export_download_intent_route(
    export_id: str,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        intent = create_export_download_intent(
            session, context,
            export_id=export_id,
            idempotency_key=idempotency_key,
        )
        return {
            "intent_id": intent.intent_id,
            "expires_at": intent.expires_at,
        }


@platform_scale_mode.get("/enrollment-candidates", operation_id="platform_list_scale_enrollment_candidates")
def platform_list_enrollment_candidates(context: TenantContext = Depends(_platform_context)):
    with tenant_session(context) as session:
        items = list_eligible_candidates(session, context)
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


@platform_scale_mode.post(
    "/enrollment-candidates",
    operation_id="platform_create_scale_enrollment_candidate",
    response_model=CandidateResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_enrollment_candidate(
    body: CandidateCreateRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        candidate = create_enrollment_candidate(
            session, context,
            decision_id=body.winner_decision_id,
            delivery_target_id=body.delivery_target_id,
            idempotency_key=idempotency_key,
        )
        return _candidate_dict(candidate)


@platform_scale_mode.get(
    "/observed-ads",
    operation_id="platform_list_scale_observed_ads",
    response_model=ObservedAdListResponse,
)
def platform_list_observed_ads(
    context: TenantContext = Depends(_platform_context),
    meta_connection_id: str | None = Query(default=None, min_length=1, max_length=64),
):
    with tenant_session(context) as session:
        return _observed_ads_dict(list_observed_ads(session, context, connection_id=meta_connection_id))


@platform_scale_mode.post(
    "/delivery-targets",
    operation_id="platform_create_scale_delivery_target",
    response_model=DeliveryTargetResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def platform_create_delivery_target(
    body: DeliveryTargetCreateRequest,
    context: TenantContext = Depends(_platform_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        target = create_delivery_target_from_ad(
            session, context,
            decision_id=body.winner_decision_id,
            connection_id=body.meta_connection_id,
            ad_remote_id=body.ad_remote_id,
            idempotency_key=idempotency_key,
        )
        return _delivery_target_dict(target)


@platform_scale_mode.post(
    "/metric-contract-sets",
    operation_id="platform_ensure_scale_metric_contract_set",
    response_model=MetricContractSetResponse,
    status_code=200,
)
def platform_ensure_metric_contract_set(
    body: MetricContractSetCreateRequest,
    context: TenantContext = Depends(_platform_context),
):
    """Set up a connection's Scale read contracts; idempotent (#112).

    The one way to give an ``internal_validation`` connection its Scale
    contracts: daily, exact window and delivery configuration, frozen to the
    account's currency and time zone. Each new generation gets its daily
    schedule (account-local 04:00), so the platform Workspace's reads start
    at the next slot; #129's observed ads list the ads of the first attested
    configuration read. A repeat with the same account returns the same set
    (200, ``created`` false); a different currency or time zone is a
    conflict with the active generations, never a silent replacement.
    """
    with tenant_session(context) as session:
        contract_set = ensure_metric_contract_set(
            session, context, connection_id=body.meta_connection_id,
            currency=body.currency, timezone_name=body.timezone_name,
        )
        schedules = {
            row.contract_generation_id: row
            for row in session.scalars(
                select(MetaObservationSyncScheduleRecord).where(
                    MetaObservationSyncScheduleRecord.workspace_id == context.workspace_id,
                    MetaObservationSyncScheduleRecord.meta_mode == context.meta_mode,
                    MetaObservationSyncScheduleRecord.contract_generation_id.in_(
                        list(contract_set.generation_ids.values())),
                    MetaObservationSyncScheduleRecord.state.in_(("active", "paused")),
                )
            )
        }
        return MetricContractSetResponse(
            id=contract_set.contract_set_id,
            meta_mode=context.meta_mode,
            meta_connection_id=body.meta_connection_id,
            created=contract_set.created,
            generations=[
                MetricContractSetGenerationResponse(
                    contract_type=contract_type,
                    generation_id=generation_id,
                    schedule_state=schedules[generation_id].state if generation_id in schedules else None,
                    next_fire_at=schedules[generation_id].next_fire_at if generation_id in schedules else None,
                )
                for contract_type, generation_id in sorted(contract_set.generation_ids.items())
            ],
        )


# Mount every mode-scoped route onto the root router at ``/{meta_mode}``.
platform_scale_root.include_router(platform_scale_mode, prefix="/{meta_mode}")
