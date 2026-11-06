"""Activation preview protocol (spec §4.5, issue #86 Task 10).

An activation preview freezes lineage + evidence at preview-time. Activation
(Task 11) consumes the preview and re-validates at commit-time, comparing
lineage hashes to detect drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.platform.tenancy import TenantContext
from .activation_evidence import resolve_activation_evidence
from .canonical_lineage import canonical_lineage_hash
from .command_journal import claim_command
from .enrollment_lineage import lock_and_validate_enrollment_candidate
from .models import (
    ScaleActivationPreviewRecord,
    ScaleEnrollmentCandidateRecord,
    ScaleEnrollmentRecord,
)

_PREVIEW_TTL = timedelta(minutes=15)


class PreviewExpiredError(Exception):
    pass


class PreviewAlreadyConsumedError(Exception):
    pass


class PreviewEnrollmentMismatchError(Exception):
    pass


class PreviewRevisionMismatchError(Exception):
    pass


class EnrollmentNotFoundError(Exception):
    pass


class EnrollmentLifecycleInvalidError(Exception):
    pass


def create_activation_preview(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> ScaleActivationPreviewRecord:
    """Create an activation preview for a draft or paused enrollment."""
    preview_id = new_id("saprev")

    scenario_scope_key = None
    if context.meta_mode == "controlled_mock":
        scenario_scope_key = session.execute(
            select(ScaleEnrollmentRecord.scenario_scope_key).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=enrollment_id,
            )
        ).scalar_one_or_none()
        if scenario_scope_key is None:
            raise EnrollmentNotFoundError(enrollment_id)

    claim = claim_command(
        session, context,
        command_type="create_preview",
        deduplication_key=f"create_preview:{idempotency_key}",
        result_id=preview_id,
        request_fingerprint=f"{enrollment_id}:{expected_revision}",
        scenario_scope_key=scenario_scope_key,
    )
    if claim.replay:
        existing = session.get(ScaleActivationPreviewRecord, claim.record.result_resource_id)
        if existing is not None:
            return existing

    # Discovery read (unlocked) to get candidate_id for lock ordering.
    # Spec §7.4: Journal → Guard → Candidate → Enrollment.
    discovery = session.execute(
        select(ScaleEnrollmentRecord)
        .filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=enrollment_id,
        )
    ).scalar_one_or_none()

    if discovery is None:
        raise EnrollmentNotFoundError(enrollment_id)

    if discovery.enrollment_candidate_id is None:
        raise EnrollmentLifecycleInvalidError(
            "Enrollment has no candidate (legacy raw-ID path)"
        )

    # Lock Guard first (via candidate validation), per spec §7.4.
    lineage = lock_and_validate_enrollment_candidate(
        session, context,
        candidate_id=discovery.enrollment_candidate_id,
    )

    # Now lock enrollment (after Guard), maintaining global order.
    enrollment = session.execute(
        select(ScaleEnrollmentRecord)
        .filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=enrollment_id,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if enrollment.lifecycle not in ("draft", "paused"):
        raise EnrollmentLifecycleInvalidError(
            f"Cannot create preview for {enrollment.lifecycle} enrollment"
        )
    if enrollment.revision != expected_revision:
        raise PreviewRevisionMismatchError(
            f"Expected revision {expected_revision}, got {enrollment.revision}"
        )

    now = datetime.now(timezone.utc)

    evidence = resolve_activation_evidence(
        session, context, lineage=lineage, reference_time=now,
    )

    transition_kind = "activate" if enrollment.lifecycle == "draft" else "resume"

    lineage_hash = canonical_lineage_hash(1, {
        "candidate_fingerprint": lineage.candidate_fingerprint,
        "guard_revision": str(lineage.guard_revision),
        "evidence_id": evidence.id,
        "attestation_id": evidence.attestation_id,
    })

    preview = ScaleActivationPreviewRecord(
        id=preview_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        scenario_scope_key=enrollment.scenario_scope_key,
        enrollment_id=enrollment_id,
        transition_kind=transition_kind,
        delivery_evidence_snapshot_id=evidence.id,
        lineage_guard_id=lineage.guard.id,
        candidate_fingerprint=lineage.candidate_fingerprint,
        guard_revision=lineage.guard_revision,
        lineage_hash=lineage_hash,
        enrollment_revision=enrollment.revision,
        expires_at=now + _PREVIEW_TTL,
    )
    session.add(preview)
    session.flush()
    return preview


def load_and_consume_preview(
    session: Session,
    context: TenantContext,
    *,
    preview_id: str,
    enrollment_id: str,
    expected_enrollment_revision: int,
) -> ScaleActivationPreviewRecord:
    """Load and atomically consume a preview. Raises on expiry, mismatch,
    or already-consumed."""
    now = datetime.now(timezone.utc)

    result = session.execute(
        update(ScaleActivationPreviewRecord)
        .where(
            ScaleActivationPreviewRecord.workspace_id == context.workspace_id,
            ScaleActivationPreviewRecord.meta_mode == context.meta_mode,
            ScaleActivationPreviewRecord.id == preview_id,
            ScaleActivationPreviewRecord.consumed_at.is_(None),
        )
        .values(consumed_at=now)
        .returning(ScaleActivationPreviewRecord)
    )
    preview = result.scalar_one_or_none()

    if preview is None:
        existing = session.execute(
            select(ScaleActivationPreviewRecord).filter_by(id=preview_id)
        ).scalar_one_or_none()
        if existing is None:
            raise PreviewEnrollmentMismatchError(f"Preview {preview_id} not found")
        if existing.consumed_at is not None:
            raise PreviewAlreadyConsumedError(preview_id)
        raise PreviewEnrollmentMismatchError(f"Preview {preview_id} inaccessible")

    expires_at = preview.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise PreviewExpiredError(preview_id)

    if preview.enrollment_id != enrollment_id:
        raise PreviewEnrollmentMismatchError(
            f"Preview is for enrollment {preview.enrollment_id}, not {enrollment_id}"
        )
    if preview.enrollment_revision != expected_enrollment_revision:
        raise PreviewRevisionMismatchError(
            f"Preview was for revision {preview.enrollment_revision}, "
            f"not {expected_enrollment_revision}"
        )

    return preview
