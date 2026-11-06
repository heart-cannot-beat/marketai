# app/meta/evidence.py
"""Evidence publication and attestation ranking (spec §6.9-§6.11).

Four operations:

* :func:`publish_evidence` -- creates an ``EvidencePublicationRecord`` for a
  sync run, idempotent on ``(sync_run_id, operation_id)``: replaying the
  same Operation (a crash-retry) returns the publication it already
  produced rather than minting a duplicate. Publication generations are
  per sync run (``uq_epub_run_generation`` on ``(sync_run_id,
  publication_generation)``): a *genuine* recovery attempt after a terminal
  failure supplies a new ``operation_id`` (the caller's own
  ``create_or_replay_operation`` call mints it) and gets a fresh generation
  for the same run, never a mutation of the failed one (§6.10).
  ``operation_id`` is accepted directly (mirroring
  :func:`app.meta.sync_pipeline.create_sync_run`'s ``operation_id``
  parameter) so the Operation's own idempotency lifecycle stays visible at
  the call site rather than being hidden inside this function.
* :func:`create_attestation` -- records that a publication is eligible
  evidence for decisions, carrying the ``evidence_watermark`` (the sync
  run's covered date range as canonical daterange text, e.g.
  ``"[2026-09-01,2026-09-07]"``).
* :func:`invalidate_attestation` -- append-only ``valid`` -> ``invalidated``
  transition (§6.9). Never reversible: invalidating an already-invalidated
  attestation raises. Removes the attestation's coverage-ledger entries
  (§6.6.6) so invalidated evidence stops counting toward the 14-day
  observation clock.
* :func:`current_attestation` -- the attestation that wins for a contract
  generation: highest ``evidence_watermark`` upper bound, ``published_at``
  as tiebreaker (§6.11). ``evidence_watermark`` is stored as plain text (see
  ``EvidenceAttestationRecord`` in ``app.meta.models``), not a native
  PostgreSQL ``daterange`` column, so ranking is done in Python over the
  (typically small) set of ``valid`` attestations for one generation rather
  than via SQL ``upper()`` -- this keeps the comparison identical on SQLite
  and PostgreSQL instead of forking behavior per dialect.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import new_id, utcnow
from .models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaObservationSyncRunRecord,
    ObservationCoverageLedgerRecord,
)

__all__ = [
    "EvidenceError",
    "publish_evidence",
    "create_attestation",
    "invalidate_attestation",
    "current_attestation",
]


class EvidenceError(ValueError):
    """Raised for evidence publication/attestation invariant violations."""


def publish_evidence(
    session: Session,
    *,
    sync_run_id: str,
    operation_id: str,
) -> EvidencePublicationRecord:
    """Publish a sync run's evidence, creating the next publication generation.

    Idempotent on ``(sync_run_id, operation_id)`` (spec §6.10): a
    crash-retry that replays the same Operation for the same sync run
    returns the publication that Operation already produced, instead of
    minting a new generation. A genuine recovery attempt after a terminal
    failure uses a *new* ``operation_id`` (the caller's own
    ``create_or_replay_operation`` call mints it) and gets a fresh
    generation -- ``publication_generation`` is one past the highest
    generation ever issued for this ``sync_run_id`` (published or failed).
    """
    existing = session.execute(
        select(EvidencePublicationRecord).where(
            EvidencePublicationRecord.sync_run_id == sync_run_id,
            EvidencePublicationRecord.operation_id == operation_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    run = session.get(MetaObservationSyncRunRecord, sync_run_id)
    if run is None:
        raise EvidenceError(f"no sync run {sync_run_id!r}")

    max_generation = session.scalar(
        select(EvidencePublicationRecord.publication_generation)
        .where(EvidencePublicationRecord.sync_run_id == sync_run_id)
        .order_by(EvidencePublicationRecord.publication_generation.desc())
        .limit(1)
    )
    publication_generation = (max_generation or 0) + 1

    publication = EvidencePublicationRecord(
        id=new_id("epub"),
        workspace_id=run.workspace_id,
        meta_mode=run.meta_mode,
        sync_run_id=run.id,
        publication_generation=publication_generation,
        state="published",
        operation_id=operation_id,
        validation_hash=None,
        published_at=utcnow(),
    )
    session.add(publication)
    session.flush()
    return publication


def create_attestation(
    session: Session,
    *,
    contract_generation_id: str,
    publication_id: str,
    evidence_watermark: str,
) -> EvidenceAttestationRecord:
    """Record that ``publication_id`` is eligible evidence for decisions.

    ``workspace_id`` and ``meta_mode`` are read from the publication, not
    accepted as parameters -- an attestation cannot disagree with the
    publication it attests to.
    """
    publication = session.get(EvidencePublicationRecord, publication_id)
    if publication is None:
        raise EvidenceError(f"no evidence publication {publication_id!r}")

    attestation = EvidenceAttestationRecord(
        id=new_id("eatt"),
        workspace_id=publication.workspace_id,
        meta_mode=publication.meta_mode,
        contract_generation_id=contract_generation_id,
        publication_id=publication_id,
        state="valid",
        evidence_watermark=evidence_watermark,
        published_at=publication.published_at or utcnow(),
    )
    session.add(attestation)
    session.flush()
    return attestation


def invalidate_attestation(
    session: Session,
    *,
    attestation_id: str,
    reason: str,
) -> EvidenceAttestationRecord:
    """Transition an attestation ``valid`` -> ``invalidated`` (spec §6.9).

    Append-only: raises if the attestation is already invalidated rather
    than allowing a second invalidation (which would silently overwrite the
    original reason/timestamp) or a revert back to ``valid``. Also removes
    this attestation's coverage-ledger entries (§6.6.6, §6.9.5) so
    invalidated evidence stops contributing to the 14-day observation clock
    for its contract generation.
    """
    attestation = session.get(EvidenceAttestationRecord, attestation_id)
    if attestation is None:
        raise EvidenceError(f"no evidence attestation {attestation_id!r}")
    if attestation.state == "invalidated":
        raise EvidenceError(
            f"attestation {attestation_id!r} is already invalidated"
        )

    attestation.state = "invalidated"
    attestation.invalidated_at = utcnow()
    attestation.invalidation_reason = reason

    session.execute(
        delete(ObservationCoverageLedgerRecord).where(
            ObservationCoverageLedgerRecord.attestation_id == attestation_id,
        )
    )

    session.flush()
    return attestation


def _watermark_upper_bound(evidence_watermark: str) -> date:
    """Parse the upper-bound date out of canonical daterange text.

    Accepts ``"[start,end]"`` / ``"[start,end)"`` (and the ``(`` variants) --
    only the closing character and the comma-split are inspected, so any
    boundary inclusivity convention parses the same way.
    """
    text = evidence_watermark.strip()
    if text[:1] in "[(" and text[-1:] in "])":
        text = text[1:-1]
    parts = text.split(",")
    if len(parts) != 2:
        raise EvidenceError(
            f"malformed evidence_watermark: {evidence_watermark!r}"
        )
    return date.fromisoformat(parts[1].strip())


def current_attestation(
    session: Session,
    *,
    contract_generation_id: str,
) -> EvidenceAttestationRecord | None:
    """The winning attestation for a contract generation, or ``None``.

    Spec §6.11: highest ``evidence_watermark`` upper bound wins, regardless
    of publication order; ``published_at`` breaks ties. A restored
    historical run with an older watermark can never outrank a still-valid
    attestation with a newer one, and invalidating the newest valid
    attestation promotes the next-highest remaining one automatically --
    this is recomputed on every call rather than cached.
    """
    candidates = session.scalars(
        select(EvidenceAttestationRecord).where(
            EvidenceAttestationRecord.contract_generation_id == contract_generation_id,
            EvidenceAttestationRecord.state == "valid",
        )
    ).all()
    if not candidates:
        return None

    def _rank(attestation: EvidenceAttestationRecord) -> tuple[date, datetime, str]:
        return (
            _watermark_upper_bound(attestation.evidence_watermark),
            attestation.published_at,
            attestation.id,
        )

    return max(candidates, key=_rank)
