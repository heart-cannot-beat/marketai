"""Pre-activation evidence resolution (spec §6.11, issue #86 Task 10).

Resolves the current delivery evidence snapshot for a validated enrollment
lineage, enforcing freshness bounds and exact delivery-target binding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.meta.evidence import current_attestation
from app.platform.tenancy import TenantContext
from .enrollment_lineage import ValidatedEnrollmentLineage
from .models import ScaleDeliveryEvidenceSnapshotRecord

MAX_EVIDENCE_AGE = timedelta(hours=36)
MAX_FUTURE_TOLERANCE = timedelta(seconds=30)


class EvidenceStaleError(Exception):
    pass


class EvidenceFromFutureError(Exception):
    pass


class NoValidAttestationError(Exception):
    pass


def resolve_activation_evidence(
    session: Session,
    context: TenantContext,
    *,
    lineage: ValidatedEnrollmentLineage,
    reference_time: datetime,
) -> ScaleDeliveryEvidenceSnapshotRecord:
    """Select or create an evidence snapshot for the exact delivery target.

    Enforces 36-hour freshness, 30-second future tolerance, and
    exact delivery_target_fingerprint binding.
    """
    target = lineage.target

    attestation = current_attestation(
        session, contract_generation_id=target.contract_generation_id,
    )
    if attestation is None:
        raise NoValidAttestationError(target.contract_generation_id)

    snapshot = session.execute(
        select(ScaleDeliveryEvidenceSnapshotRecord)
        .filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            delivery_target_id=target.id,
            delivery_target_fingerprint=target.target_fingerprint,
            attestation_id=attestation.id,
        )
        .order_by(ScaleDeliveryEvidenceSnapshotRecord.observed_at.desc())
    ).scalars().first()

    if snapshot is None:
        from app.adapters.persistence.models import new_id
        snapshot = ScaleDeliveryEvidenceSnapshotRecord(
            id=new_id("sdes"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            scenario_scope_key=target.scenario_scope_key,
            delivery_target_id=target.id,
            delivery_target_fingerprint=target.target_fingerprint,
            contract_generation_id=target.contract_generation_id,
            attestation_id=attestation.id,
            evidence_watermark=attestation.evidence_watermark,
            observed_at=reference_time,
        )
        session.add(snapshot)
        session.flush()

    observed_at = snapshot.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = reference_time - observed_at
    if age > MAX_EVIDENCE_AGE:
        raise EvidenceStaleError(f"Evidence is {age} old, max {MAX_EVIDENCE_AGE}")

    if observed_at > reference_time + MAX_FUTURE_TOLERANCE:
        raise EvidenceFromFutureError(str(observed_at))

    return snapshot
