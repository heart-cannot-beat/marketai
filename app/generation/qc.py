"""Quality control attempts for generated Asset Versions (#21 Task 4, #169).

A QC attempt is recorded against an exact ``AssetVersion`` as one
``AssetQCAttemptRecord`` plus append-only ``AssetQCCheckResultRecord``
evidence rows (insert-only -- never updated or deleted), and advances the
RunItem that produced the version through a CAS ``UPDATE ... WHERE revision
= expected`` -- the same pattern ``app.generation.worker``'s state
transitions use, so a concurrent writer on the same RunItem can never be
silently clobbered.

Since Issue #169 the Worker's ``generation.qc_asset_version`` Job runs
:func:`run_quality_check`: a governed ``llm_chat`` vision call
(``app.generation.vlm_qc``) that checks the image against the frozen
recipe's confirmed must-match items and the product's reference images.

* **Inputs.** The generated image (its committed object, hash-verified; a
  Mock version without one is rebuilt from its deterministic bytes) and up
  to three reference derivatives read through the Workspace-bound
  :class:`~app.generation.image_call.ReferenceReader` (committed key of this
  Workspace *and* the frozen sha256). Another Workspace's object is never
  read; an unreadable reference makes the affected verdicts ``uncertain``.
* **Governance.** The call is keyed ``(generation Job, "vlm_qc")``: the
  admitting command (submission / Recovery) reserved its maximum next to
  the image call's (``app.generation.spend``), the coordinator settles it at
  reported usage, a re-run Job replays the stored answer, an unknown outcome
  stays ``uncertain``. A check that is never made releases its reservation.
* **Outcome.** ``failed_blocking`` (a blocking must-match item whose
  ``qc_method`` lets the VLM decide, the product itself, or -- under policy
  ``vlm-qc/v2`` (Issue #194) -- the built-in single-continuous-image check
  against collages, split screens and seams, failed) fails
  the RunItem and blocks approval (``review``);
  every failure projects the version's human quality to *Needs Changes*
  with the Chinese reasons (a ``machine_qc`` review row a person corrects
  through the normal supersession chain), and the third consecutive failure
  of the same combination -- the same RunItem's retries, then the version a
  regeneration was drafted from -- projects *Rejected* instead. The image,
  the answer and every check row are kept: nothing pretends the task was
  lost (v9 §3.1). ``uncertain`` never passes silently: the RunItem succeeds
  (its image exists) but the attempt says why nobody could confirm it.

:func:`run_qc` / :func:`run_qc_with_blocking_failure` remain the
deterministic seams (recipes frozen before #169, and the review-gate tests).
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from typing import Callable, ContextManager

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    QualityReviewRecord,
    ResolutionPreviewRecord,
    new_id,
    utcnow,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.generation.creative_recipe import QC_FAILED_STATES, build_creative_recipe
from app.generation.models import (
    AssetQCAttemptRecord,
    AssetQCCheckResultRecord,
    GenerationDraftRecord,
    GenerationRecipeSnapshotRecord,
    ResolutionPreviewConfirmationRecord,
    RunItemRecord,
)
from app.generation.onboarding_facts import emit_onboarding_fact
from app.generation.vlm_qc import (
    METHOD_VLM,
    QC_CALL_NAME,
    QC_POLICY_VERSION,
    QCCheck,
    QCVerdict,
    build_qc_request,
    interpret_qc_answer,
    policy_of,
    rule_set_version,
    unchecked_verdict,
)
from app.platform import audit
from app.platform.blob_store import BlobStore, CommittedBlobRef
from app.platform.tenancy import TenantContext
from app.providers.llm_registry import MAX_IMAGE_BYTES
from app.providers.llm_types import LlmImage, canonical_digest

logger = logging.getLogger(__name__)

#: The third consecutive failure of one combination projects Rejected (v9 §3.1).
REJECT_AFTER_CONSECUTIVE_FAILURES = 3
#: How far back the failure streak is followed (retries, then regeneration sources).
_MAX_STREAK_WALK = 20

MACHINE_REVIEW_SOURCE = "machine_qc"
MACHINE_REVIEW_ACTOR = "system:vlm_qc"

#: Execution modes ``asset_qc_attempts.execution_mode`` accepts.
_ATTEMPT_MODES = ("byok", "sandbox", "mock")


class QCError(ValueError):
    """Raised when a QC attempt cannot be recorded against a RunItem."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _get_run_item(session: Session, ctx: TenantContext, run_item_id: str) -> RunItemRecord:
    """Workspace-scoped lookup.

    Bare ``scalar_one()`` (not ``scalar_one_or_none()``) so a foreign-workspace
    ``run_item_id`` raises ``sqlalchemy.exc.NoResultFound`` -- the same
    tenant-isolation contract ``app.generation.worker._get_run_item`` gives
    ``handle_run_item()``.
    """
    return session.execute(
        select(RunItemRecord).where(
            RunItemRecord.id == run_item_id,
            RunItemRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()


def _transition_run_item(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    *,
    state: str,
    error_summary: str | None = None,
) -> RunItemRecord:
    values: dict = {"state": state, "revision": item.revision + 1, "updated_at": utcnow()}
    if error_summary is not None:
        values["error_summary"] = error_summary

    result = session.execute(
        update(RunItemRecord)
        .where(
            RunItemRecord.id == item.id,
            RunItemRecord.workspace_id == ctx.workspace_id,
            RunItemRecord.revision == item.revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise QCError(
            "run_item_revision_conflict",
            f"RunItem {item.id} expected revision {item.revision}, was concurrently modified",
        )
    session.flush()
    session.refresh(item)
    return item


def _lock_version(session: Session, ctx: TenantContext, asset_version_id: str) -> AssetVersionRecord:
    # Freeze/Launch eligibility locks the same exact AssetVersion.  Taking
    # that row lock before appending QC evidence gives those gates a real
    # linearization boundary instead of merely claiming one in their audit
    # payload.
    version = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == asset_version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if version is None:
        raise QCError(
            "asset_version_not_found",
            f"AssetVersion {asset_version_id} not found",
        )
    return version


def _insert_attempt(
    session: Session,
    ctx: TenantContext,
    *,
    asset_version_id: str,
    run_item_id: str,
    execution_mode: str,
    checks: list[tuple[QCCheck | None, dict]],
    final_state: str,
    check_type: str,
    qc_policy_version: str = "v1",
    rule_set_version: str = "v1",
    input_hash: str | None = None,
    provider_attempt_id: str | None = None,
    feedback_round: int = 0,
    feedback_summary: dict | None = None,
) -> AssetQCAttemptRecord:
    now = utcnow()
    max_seq = session.scalar(
        select(func.coalesce(func.max(AssetQCAttemptRecord.attempt_sequence), 0)).where(
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
            AssetQCAttemptRecord.asset_version_id == asset_version_id,
        )
    )
    attempt = AssetQCAttemptRecord(
        id=new_id("qca"),
        workspace_id=ctx.workspace_id,
        asset_version_id=asset_version_id,
        run_item_id=run_item_id,
        feedback_round=feedback_round,
        qc_policy_version=qc_policy_version,
        rule_set_version=rule_set_version,
        input_hash=input_hash or f"qc:{asset_version_id}"[:64],
        provider_attempt_id=provider_attempt_id,
        execution_mode=execution_mode if execution_mode in _ATTEMPT_MODES else "mock",
        state="running",
        feedback_summary_json=feedback_summary or {},
        attempt_sequence=(max_seq or 0) + 1,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    session.add(attempt)
    session.flush()

    # Append-only: AssetQCCheckResultRecord rows are inserted, never updated.
    for check, legacy in checks:
        if check is not None:
            evidence = {**check.evidence(), "qc_attempt_sequence": attempt.attempt_sequence}
            row = AssetQCCheckResultRecord(
                id=new_id("qcr"),
                workspace_id=ctx.workspace_id,
                qc_attempt_id=attempt.id,
                rule_id=check.rule_id,
                rule_version=rule_set_version,
                check_type=check_type,
                severity=check.severity,
                outcome=check.outcome,
                evidence_json=evidence,
                evidence_hash=_evidence_hash(evidence),
                feedback_detail=check.reason,
                created_at=now,
            )
        else:
            evidence = dict(legacy.get("evidence") or {})
            row = AssetQCCheckResultRecord(
                id=new_id("qcr"),
                workspace_id=ctx.workspace_id,
                qc_attempt_id=attempt.id,
                rule_id=legacy["rule_id"],
                rule_version="v1",
                check_type="deterministic",
                severity=legacy["severity"],
                outcome=legacy["outcome"],
                evidence_json=evidence,
                evidence_hash=_evidence_hash(evidence),
                created_at=now,
            )
        session.add(row)

    attempt.state = final_state
    attempt.revision += 1
    attempt.terminal_at = utcnow()
    attempt.updated_at = utcnow()
    session.flush()
    return attempt


def _record_attempt(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
    run_item_id: str,
    item: RunItemRecord,
    *,
    rule_id: str,
    severity: str,
    outcome: str,
    final_state: str,
    evidence: dict | None = None,
) -> AssetQCAttemptRecord:
    """One deterministic check (the pre-#169 seam)."""
    _lock_version(session, ctx, asset_version_id)
    return _insert_attempt(
        session, ctx,
        asset_version_id=asset_version_id,
        run_item_id=run_item_id,
        execution_mode=item.execution_mode,
        checks=[(None, {
            "rule_id": rule_id, "severity": severity, "outcome": outcome, "evidence": evidence or {},
        })],
        final_state=final_state,
        check_type="deterministic",
    )


def run_qc(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
    run_item_id: str,
) -> AssetQCAttemptRecord:
    """Run a single passing deterministic QC check against ``asset_version_id``.

    The Worker uses this only for recipes frozen before Issue #169 (no
    quality-check policy, nothing reserved for a VLM call).
    """
    item = _get_run_item(session, ctx, run_item_id)
    attempt = _record_attempt(
        session, ctx, asset_version_id, run_item_id, item,
        rule_id="format_check",
        severity="must_match",
        outcome="pass",
        final_state="passed",
    )
    item = _transition_run_item(session, ctx, item, state="succeeded")
    from app.generation.worker import _sync_run_state_best_effort

    _sync_run_state_best_effort(session, ctx, item.generation_run_id)
    emit_onboarding_fact(
        session,
        ctx,
        fact_type="asset_version.qualified",
        source_resource_id=asset_version_id,
    )
    return attempt


def run_qc_with_blocking_failure(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
    run_item_id: str,
) -> AssetQCAttemptRecord:
    """Test-support seam: record a QC attempt with a blocking ``must_match`` failure.

    Used to exercise the review gate in :mod:`app.generation.review` -- a
    version carrying a ``failed_blocking`` QC attempt cannot be approved.
    """
    item = _get_run_item(session, ctx, run_item_id)
    attempt = _record_attempt(
        session, ctx, asset_version_id, run_item_id, item,
        rule_id="content_safety",
        severity="must_match",
        outcome="fail",
        final_state="failed_blocking",
        evidence={"reason": "blocking content violation"},
    )
    item = _transition_run_item(
        session,
        ctx,
        item,
        state="failed",
        error_summary="qc_blocking_failure",
    )
    from app.generation.worker import _sync_run_state_best_effort

    _sync_run_state_best_effort(session, ctx, item.generation_run_id)
    return attempt


# ---------------------------------------------------------------------------
# Recording a VLM outcome
# ---------------------------------------------------------------------------


def _latest_qc_state(session: Session, ctx: TenantContext, asset_version_id: str) -> str | None:
    return session.execute(
        select(AssetQCAttemptRecord.state)
        .where(
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
            AssetQCAttemptRecord.asset_version_id == asset_version_id,
        )
        .order_by(AssetQCAttemptRecord.attempt_sequence.desc())
        .limit(1)
    ).scalar_one_or_none()


def _producing_item(session: Session, ctx: TenantContext, version: AssetVersionRecord) -> RunItemRecord | None:
    if not version.producing_run_item_id:
        return None
    return session.execute(
        select(RunItemRecord).where(
            RunItemRecord.id == version.producing_run_item_id,
            RunItemRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()


def _regeneration_source(session: Session, ctx: TenantContext, item: RunItemRecord) -> AssetVersionRecord | None:
    """The version the item's Run was drafted from (regeneration), if any."""
    run = session.execute(
        select(GenerationRunRecord).where(
            GenerationRunRecord.id == item.generation_run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if run is None or not run.creation_preview_confirmation_id:
        return None
    source_id = session.execute(
        select(GenerationDraftRecord.source_asset_version_id)
        .select_from(ResolutionPreviewConfirmationRecord)
        .join(
            ResolutionPreviewRecord,
            (ResolutionPreviewRecord.id == ResolutionPreviewConfirmationRecord.preview_id)
            & (ResolutionPreviewRecord.workspace_id == ResolutionPreviewConfirmationRecord.workspace_id),
        )
        .join(
            GenerationDraftRecord,
            (GenerationDraftRecord.id == ResolutionPreviewRecord.generation_draft_id)
            & (GenerationDraftRecord.workspace_id == ResolutionPreviewRecord.workspace_id),
        )
        .where(
            ResolutionPreviewConfirmationRecord.id == run.creation_preview_confirmation_id,
            ResolutionPreviewConfirmationRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if not source_id:
        return None
    return session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == source_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()


def _predecessor(
    session: Session, ctx: TenantContext, version: AssetVersionRecord, item: RunItemRecord | None,
) -> AssetVersionRecord | None:
    """The previous attempt at the same combination: an earlier image of the
    same RunItem (a Recovery retry), else the version a regeneration of this
    combination was drafted from."""
    if item is None:
        return None
    earlier = session.execute(
        select(AssetVersionRecord)
        .where(
            AssetVersionRecord.workspace_id == ctx.workspace_id,
            AssetVersionRecord.producing_run_item_id == item.id,
            AssetVersionRecord.id != version.id,
            AssetVersionRecord.created_at <= version.created_at,
        )
        .order_by(AssetVersionRecord.created_at.desc(), AssetVersionRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if earlier is not None:
        return earlier
    return _regeneration_source(session, ctx, item)


def prior_failure_streak(
    session: Session, ctx: TenantContext, version: AssetVersionRecord, item: RunItemRecord | None,
) -> int:
    """Consecutive failed checks immediately before ``version`` (same combination).

    Only versions of the same execution mode count: Controlled Mock evidence
    never mixes with real evidence.
    """
    streak = 0
    seen = {version.id}
    current, current_item = version, item
    for _ in range(_MAX_STREAK_WALK):
        previous = _predecessor(session, ctx, current, current_item)
        if previous is None or previous.id in seen or previous.execution_mode != version.execution_mode:
            break
        seen.add(previous.id)
        if _latest_qc_state(session, ctx, previous.id) not in QC_FAILED_STATES:
            break
        streak += 1
        current, current_item = previous, _producing_item(session, ctx, previous)
    return streak


def _project_machine_review(
    session: Session,
    ctx: TenantContext,
    version: AssetVersionRecord,
    *,
    review_value: str,
    reason: str,
) -> QualityReviewRecord | None:
    """Project the human quality of a failed version (never over a person's review)."""
    from app.generation.review import find_review_head

    if find_review_head(session, ctx, version.id) is not None:
        return None
    asset = session.execute(
        select(AssetRecord).where(
            AssetRecord.id == version.asset_id,
            AssetRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if asset is None:
        return None
    review = QualityReviewRecord(
        id=new_id("qr"),
        workspace_id=ctx.workspace_id,
        product_id=asset.product_id,
        asset_id=asset.id,
        asset_version_id=version.id,
        review_value=review_value,
        reason=reason[:2000],
        source=MACHINE_REVIEW_SOURCE,
        actor_id=MACHINE_REVIEW_ACTOR,
        legacy_unscoped=False,
        created_at=utcnow(),
    )
    session.add(review)
    session.flush()
    return review


@dataclass(frozen=True)
class QualityCheckOutcome:
    attempt_id: str | None
    state: str
    consecutive_failures: int = 0
    projected_review: str | None = None
    llm_attempt_id: str | None = None
    replayed: bool = False


def record_quality_check(
    session: Session,
    ctx: TenantContext,
    *,
    asset_version_id: str,
    run_item_id: str,
    verdict: QCVerdict,
    execution_mode: str,
    policy: dict | None,
    llm_attempt_id: str | None = None,
    request_digest: str | None = None,
    call_evidence: dict | None = None,
) -> QualityCheckOutcome:
    """Record one finished quality check and project it (one transaction)."""
    item = _get_run_item(session, ctx, run_item_id)
    version = _lock_version(session, ctx, asset_version_id)
    if item.state != "qc_pending":
        # A redelivered Job after the outcome was committed: nothing to do.
        latest = session.execute(
            select(AssetQCAttemptRecord)
            .where(
                AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
                AssetQCAttemptRecord.asset_version_id == asset_version_id,
            )
            .order_by(AssetQCAttemptRecord.attempt_sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        return QualityCheckOutcome(
            attempt_id=latest.id if latest else None,
            state=latest.state if latest else item.state,
            replayed=True,
        )

    prior = prior_failure_streak(session, ctx, version, item)
    streak = prior + 1 if verdict.failed else 0
    projected = None
    if verdict.failed:
        projected = "rejected" if streak >= REJECT_AFTER_CONSECUTIVE_FAILURES else "needs_changes"

    reasons = verdict.reasons()
    summary = {
        "verdict": verdict.state,
        "summary": verdict.summary,
        "reasons": reasons,
        "fix_hints": verdict.fix_hints(),
        "consecutive_failures": streak,
        "projected_review": projected,
        "policy_version": (policy or {}).get("version") or QC_POLICY_VERSION,
        "method": (policy or {}).get("method"),
        "provider": (policy or {}).get("provider"),
        "model": (policy or {}).get("model"),
        "llm_attempt_id": llm_attempt_id,
        "controlled_mock": execution_mode == "mock",
        **({"call": call_evidence} if call_evidence else {}),
        **{key: value for key, value in verdict.evidence.items() if value not in (None, [], "")},
    }
    attempt = _insert_attempt(
        session, ctx,
        asset_version_id=asset_version_id,
        run_item_id=run_item_id,
        execution_mode=execution_mode,
        checks=[(check, {}) for check in verdict.checks],
        final_state=verdict.state,
        check_type="vlm" if llm_attempt_id else "vlm_unavailable",
        qc_policy_version=str((policy or {}).get("version") or QC_POLICY_VERSION),
        rule_set_version=rule_set_version(policy),
        input_hash=request_digest or _evidence_hash({"asset_version_id": asset_version_id, "verdict": verdict.state}),
        provider_attempt_id=llm_attempt_id,
        feedback_round=prior,
        feedback_summary=summary,
    )

    if verdict.state == "failed_blocking":
        item = _transition_run_item(
            session, ctx, item, state="failed",
            error_summary=("qc_blocking_failure: " + "；".join(reasons))[:1000],
        )
    else:
        item = _transition_run_item(session, ctx, item, state="succeeded")

    review = None
    if projected is not None:
        lead = (
            f"连续 {streak} 次自动质检未通过，已标记为 Rejected（产物与失败证据均已保留）："
            if projected == "rejected" else "自动质检未通过："
        )
        review = _project_machine_review(
            session, ctx, version, review_value=projected, reason=lead + "；".join(reasons or [verdict.summary]),
        )

    from app.generation.worker import _sync_run_state_best_effort

    _sync_run_state_best_effort(session, ctx, item.generation_run_id)
    if verdict.state == "passed":
        emit_onboarding_fact(
            session,
            ctx,
            fact_type="asset_version.qualified",
            source_resource_id=asset_version_id,
        )
    audit.append_event(
        session,
        event_type="generation.asset_qc_recorded",
        resource_type="asset_version",
        resource_id=asset_version_id,
        details={
            "qc_attempt_id": attempt.id,
            "run_item_id": run_item_id,
            "state": verdict.state,
            "execution_mode": execution_mode,
            "consecutive_failures": streak,
            "projected_review": projected if review is not None else None,
            "llm_attempt_id": llm_attempt_id,
            "failed_rules": [c.rule_id for c in verdict.checks if c.verdict == "fail"],
            "uncertain_rules": [c.rule_id for c in verdict.checks if c.verdict == "uncertain"],
        },
    )
    return QualityCheckOutcome(
        attempt_id=attempt.id,
        state=verdict.state,
        consecutive_failures=streak,
        projected_review=projected if review is not None else None,
        llm_attempt_id=llm_attempt_id,
    )


# ---------------------------------------------------------------------------
# The Worker's quality check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    item_id: str
    version_id: str
    execution_mode: str
    semantic_fingerprint: str
    policy: dict | None
    recipe: object
    generation_job_id: str
    operation_id: str
    committed_key: str | None
    storage_version_id: str
    content_sha256: str
    byte_size: int
    mime_type: str
    thumbnail: CommittedBlobRef | None
    thumbnail_sha256: str


def _generation_call(session: Session, ctx: TenantContext, version: AssetVersionRecord, item: RunItemRecord) -> tuple[str, str]:
    """``(job_id, operation_id)`` of the call that produced ``version``."""
    if version.provider_attempt_id:
        attempt = session.execute(
            select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.id == version.provider_attempt_id,
                ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one_or_none()
        if attempt is not None and attempt.job_id and attempt.operation_id:
            return attempt.job_id, attempt.operation_id
    from app.generation.worker import _get_job_id, _get_operation_id

    return _get_job_id(session, ctx, item), _get_operation_id(session, ctx, item)


def _prepare(session: Session, ctx: TenantContext, asset_version_id: str, run_item_id: str) -> _Prepared | None:
    item = _get_run_item(session, ctx, run_item_id)
    if item.state != "qc_pending":
        return None
    version = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == asset_version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()
    run = session.execute(
        select(GenerationRunRecord).where(
            GenerationRunRecord.id == item.generation_run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()
    snapshot = None
    if run.recipe_snapshot_id:
        snapshot = session.execute(
            select(GenerationRecipeSnapshotRecord).where(
                GenerationRecipeSnapshotRecord.id == run.recipe_snapshot_id,
                GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id,
            )
        ).scalar_one_or_none()
    policy = policy_of(snapshot.canonical_json if snapshot is not None else None)
    job_id, operation_id = _generation_call(session, ctx, version, item)
    thumbnail = None
    if version.thumbnail_object_key and version.thumbnail_content_sha256:
        thumbnail = CommittedBlobRef(
            committed_key=version.thumbnail_object_key,
            storage_version_id=version.thumbnail_storage_version_id or "",
            size_bytes=int(version.thumbnail_byte_size or 0),
            checksum_sha256_b64=_b64(version.thumbnail_content_sha256),
            content_type_verified=version.thumbnail_mime_type or "image/jpeg",
        )
    return _Prepared(
        item_id=item.id,
        version_id=version.id,
        execution_mode=item.execution_mode,
        semantic_fingerprint=item.semantic_fingerprint,
        policy=policy,
        recipe=build_creative_recipe(session, ctx, item, run),
        generation_job_id=job_id,
        operation_id=operation_id,
        committed_key=version.committed_object_key,
        storage_version_id=version.committed_storage_version_id or "",
        content_sha256=version.content_sha256 or "",
        byte_size=int(version.byte_size or 0),
        mime_type=version.mime_type or "image/png",
        thumbnail=thumbnail,
        thumbnail_sha256=version.thumbnail_content_sha256 or "",
    )


def _b64(hex_digest: str) -> str:
    try:
        return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
    except ValueError:
        return ""


class _Unreadable(Exception):
    pass


async def _read_committed(
    blob_store: BlobStore | None, ctx: TenantContext, ref: CommittedBlobRef, expected_sha256: str,
) -> bytes:
    if not ref.committed_key.startswith(f"committed/{ctx.workspace_id}/"):
        raise _Unreadable("not a committed object of this Workspace")
    if blob_store is None:
        raise _Unreadable("no BlobStore")
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in blob_store.open_committed(ref):
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise _Unreadable("larger than the vision input limit")
            chunks.append(chunk)
    except _Unreadable:
        raise
    except Exception as exc:  # noqa: BLE001 -- storage failure: uncertain, never a pass
        raise _Unreadable(type(exc).__name__) from exc
    content = b"".join(chunks)
    if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise _Unreadable("content does not match its recorded hash")
    return content


async def _generated_image(prepared: _Prepared, ctx: TenantContext, blob_store: BlobStore | None) -> bytes:
    """The generated image's bytes, verified against the AssetVersion's hash."""
    failure: _Unreadable = _Unreadable("the generated image has no committed object")
    if prepared.committed_key:
        ref = CommittedBlobRef(
            committed_key=prepared.committed_key,
            storage_version_id=prepared.storage_version_id,
            size_bytes=prepared.byte_size,
            checksum_sha256_b64=_b64(prepared.content_sha256),
            content_type_verified=prepared.mime_type,
        )
        try:
            return await _read_committed(blob_store, ctx, ref, prepared.content_sha256)
        except _Unreadable as exc:
            failure = exc
        if prepared.thumbnail is not None:
            # An original over the vision limit (or briefly unavailable) is
            # still judged from its committed 512px derivative.
            try:
                return await _read_committed(blob_store, ctx, prepared.thumbnail, prepared.thumbnail_sha256)
            except _Unreadable:
                pass
    if prepared.execution_mode == "mock":
        # A Mock version's bytes are a pure function of its item: rebuild and
        # verify them when no committed copy can be read (no BlobStore).
        from app.generation.worker import build_mock_output

        content = build_mock_output(prepared.semantic_fingerprint)["content_bytes"]
        if hashlib.sha256(content).hexdigest() == prepared.content_sha256:
            return content
    raise failure


async def run_quality_check(
    ctx: TenantContext,
    *,
    asset_version_id: str,
    run_item_id: str,
    llm_client,
    blob_store: BlobStore | None,
    session_factory: Callable[[TenantContext], ContextManager[Session]],
) -> QualityCheckOutcome:
    """The ``generation.qc_asset_version`` Job: check, then record (Issue #169)."""
    from app.generation.image_call import ReferenceReader
    from app.providers.adapter import ProviderDispatchError
    from app.providers.llm_chat import LlmChatError, release_llm_chat_reservation

    with session_factory(ctx) as session:
        prepared = _prepare(session, ctx, asset_version_id, run_item_id)
        if prepared is None:
            item = _get_run_item(session, ctx, run_item_id)
            return QualityCheckOutcome(attempt_id=None, state=item.state, replayed=True)
        if prepared.policy is None:
            # Frozen before #169: nothing was reserved for a VLM call.
            attempt = run_qc(session, ctx, asset_version_id, run_item_id)
            return QualityCheckOutcome(attempt_id=attempt.id, state=attempt.state)

    policy = prepared.policy
    llm_mode = str(policy.get("execution_mode") or "")
    attempt_mode = llm_mode or ("mock" if prepared.execution_mode != "byok" else "byok")

    def record(verdict: QCVerdict, **kwargs) -> QualityCheckOutcome:
        with session_factory(ctx) as session:
            return record_quality_check(
                session, ctx,
                asset_version_id=asset_version_id, run_item_id=run_item_id, verdict=verdict,
                execution_mode=attempt_mode, policy=policy, **kwargs,
            )

    def release() -> None:
        if llm_mode != "byok":
            return
        with session_factory(ctx) as session:
            release_llm_chat_reservation(
                session, ctx, job_id=prepared.generation_job_id, call_name=QC_CALL_NAME,
            )

    if policy.get("method") != METHOD_VLM:
        return record(unchecked_verdict(
            str(policy.get("detail") or "这次生成不做自动质检，请人工审核。"),
            code=str(policy.get("reason") or "quality_check_unavailable"),
        ))

    try:
        generated = await _generated_image(prepared, ctx, blob_store)
    except _Unreadable as exc:
        release()
        return record(unchecked_verdict(
            f"生成图无法读取（{exc}），没有做自动质检，请人工审核。", code="generated_image_unreadable",
        ))

    recipe = prepared.recipe
    reader = ReferenceReader(ctx.workspace_id, blob_store)
    references: list[LlmImage] = []
    unreadable = 0
    for reference in recipe.reference_images[: int(policy.get("reference_images") or 0)]:
        try:
            content = await reader.read(reference)
        except ProviderDispatchError:
            unreadable += 1
            continue
        if len(content) > MAX_IMAGE_BYTES:
            unreadable += 1
            continue
        references.append(LlmImage.from_bytes(content, reference.mime_type))

    request = build_qc_request(
        recipe, policy,
        generated=LlmImage.from_bytes(generated, prepared.mime_type),
        references=references,
        references_unreadable=unreadable,
    )
    input_digest = canonical_digest({
        "policy": policy.get("version"),
        "model": policy.get("model"),
        "images": [hashlib.sha256(image.content or b"").hexdigest() for image in request.images],
        "messages": [hashlib.sha256(m.text.encode("utf-8")).hexdigest() for m in request.messages],
    })
    try:
        result = await llm_client.chat(
            ctx,
            job_id=prepared.generation_job_id,
            operation_id=prepared.operation_id,
            call_name=QC_CALL_NAME,
            request=request,
            execution_mode=llm_mode,
        )
    except LlmChatError as error:
        if error.dispatch_certainty == "not_dispatched":
            release()
        logger.warning(
            "generation_vlm_qc_call_failed",
            extra={
                "workspace_id": ctx.workspace_id,
                "asset_version_id": asset_version_id,
                "error_code": error.code,
                "dispatch_certainty": error.dispatch_certainty,
            },
        )
        reason = (
            "质检调用结果未知（已记录待对账），这张图请人工审核。"
            if error.dispatch_certainty == "uncertain"
            else f"质检调用失败（{error.code}），这张图请人工审核。"
        )
        return record(
            unchecked_verdict(reason, code=error.code),
            llm_attempt_id=error.attempt_id,
            request_digest=input_digest,
            call_evidence={"error_code": error.code, "dispatch_certainty": error.dispatch_certainty},
        )

    verdict = interpret_qc_answer(
        result.data, recipe, references_unreadable=unreadable,
        policy_version=str(policy.get("version") or ""),
    )
    return record(
        verdict,
        llm_attempt_id=result.attempt_id,
        request_digest=input_digest,
        call_evidence={
            "provider": result.provider,
            "model": result.model,
            "model_version": result.model_version,
            "execution_mode": result.execution_mode,
            "usage": dict(result.usage),
            "cost": dict(result.cost),
            "replayed": result.replayed,
            "images": 1 + len(references),
        },
    )


def record_quality_check_fault(
    ctx: TenantContext,
    *,
    asset_version_id: str,
    run_item_id: str,
    error: BaseException,
    session_factory: Callable[[TenantContext], ContextManager[Session]],
) -> None:
    """Settle a RunItem whose quality check crashed as ``uncertain`` (best effort).

    Without this a handler fault leaves the item at ``qc_pending`` and its
    Run ``running`` forever. The image is kept and the attempt says the
    check could not be made -- never a pass. A reservation the check never
    used is released.
    """
    from app.providers.llm_chat import release_llm_chat_reservation

    try:
        with session_factory(ctx) as session:
            item = _get_run_item(session, ctx, run_item_id)
            if item.state != "qc_pending":
                return
            version = session.execute(
                select(AssetVersionRecord).where(
                    AssetVersionRecord.id == asset_version_id,
                    AssetVersionRecord.workspace_id == ctx.workspace_id,
                )
            ).scalar_one()
            if item.execution_mode == "byok":
                job_id, _operation_id = _generation_call(session, ctx, version, item)
                release_llm_chat_reservation(session, ctx, job_id=job_id, call_name=QC_CALL_NAME)
            record_quality_check(
                session, ctx,
                asset_version_id=asset_version_id,
                run_item_id=run_item_id,
                verdict=unchecked_verdict(
                    f"质检执行出错（{type(error).__name__}），这张图请人工审核。", code="qc_internal_error",
                ),
                execution_mode=item.execution_mode,
                policy=None,
            )
    except Exception:  # noqa: BLE001 -- the Job's own failure records the fault
        logger.exception(
            "generation_qc_fault_not_recorded",
            extra={"workspace_id": ctx.workspace_id, "asset_version_id": asset_version_id},
        )


def _evidence_hash(evidence: dict) -> str:
    from ..platform.canonical import canonical_hash
    return canonical_hash(evidence)
