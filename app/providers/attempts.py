"""Generic ProviderAttempt module: header + append-only events + CAS projection.

Generalizes the pattern in ``app.generation.provider_attempt`` (Generation's
private copy) into a domain-agnostic module per spec §2D/§2J: any caller
(Localization, Generation once migrated) creates a
``ProviderCallAttemptRecord`` header, appends append-only outcome/usage
events, and reads a rebuildable CAS projection through this interface --
never by touching the three physical tables directly.

``step_kind``, ``chargeability``, ``truth_package_id`` and ``run_item_id``
are opaque metadata columns to this module. It never branches on their
value; it only persists what the caller supplies and enforces the
append-only/CAS shape of the event and projection tables.

Generation keeps its own ``app.generation.provider_attempt`` module for now
("Keep compatibility imports while generation migrates" -- spec §2D). Both
modules read/write the same three physical tables
(``provider_call_attempts``, ``provider_attempt_events``,
``provider_attempt_projections``), so the CAS/append-only semantics observed
by one are always visible to the other; migrating Generation's call sites
onto this module is a separate, later task.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.generation.models import (
    ProviderAttemptEventRecord,
    ProviderAttemptProjectionRecord,
)


class StaleProjectionError(RuntimeError):
    """Raised when a CAS UPDATE on a ProviderAttemptProjection affects no row.

    The ``revision`` guard means the row was concurrently advanced by
    another writer between the caller's read and this write; the caller
    must reload the projection (or the whole attempt) and retry, not
    silently overwrite.
    """


def prepare_attempt(
    session: Session,
    *,
    workspace_id: str,
    operation_id: str,
    job_id: str,
    step_kind: str,
    call_semantic_key_hash: str,
    attempt_no: int,
    execution_mode: str,
    provider: str,
    chargeability: str = "charged",
    spend_reservation_id: str | None = None,
    run_item_id: str | None = None,
    truth_package_id: str | None = None,
    model: str | None = None,
    provider_idempotency_key: str | None = None,
    credential_version_id: str | None = None,
    request_fingerprint_hash: str | None = None,
    redacted_request_hash: str | None = None,
) -> ProviderCallAttemptRecord:
    """Create a ``ProviderCallAttemptRecord`` header plus its initial CAS
    projection, in ``dispatch_state='prepared'`` / ``outcome='pending'``.

    Caller-supplied ``chargeability``/``execution_mode``/
    ``spend_reservation_id`` must already satisfy
    ``ProviderCallAttemptRecord``'s relational CHECK (the caller -- e.g. a
    command's admission path -- decided mode and chargeability before this
    call; this module does not infer or default that policy beyond the
    column's own ``chargeability="charged"`` default, mirrored here for
    call-site compatibility).
    """
    now = utcnow()
    idem_hash = (
        hashlib.sha256(provider_idempotency_key.encode()).hexdigest()
        if provider_idempotency_key
        else None
    )
    attempt = ProviderCallAttemptRecord(
        id=new_id("pca"),
        workspace_id=workspace_id,
        operation_id=operation_id,
        job_id=job_id,
        spend_reservation_id=spend_reservation_id,
        call_semantic_key_hash=call_semantic_key_hash,
        attempt_no=attempt_no,
        execution_mode=execution_mode,
        provider=provider,
        model=model,
        provider_idempotency_key=provider_idempotency_key,
        idempotency_key_hash=idem_hash,
        credential_version_id=credential_version_id,
        request_fingerprint_hash=request_fingerprint_hash,
        redacted_request_hash=redacted_request_hash,
        run_item_id=run_item_id,
        step_kind=step_kind,
        attempt_number=attempt_no,
        chargeability=chargeability,
        truth_package_id=truth_package_id,
        dispatch_state="prepared",
        prepared_at=now,
        updated_at=now,
    )
    session.add(attempt)

    projection = ProviderAttemptProjectionRecord(
        id=new_id("pap"),
        workspace_id=workspace_id,
        provider_call_attempt_id=attempt.id,
        dispatch_certainty="not_dispatched",
        outcome="pending",
        last_event_sequence=0,
        revision=1,
        updated_at=now,
    )
    session.add(projection)
    session.flush()
    return attempt


def append_event(
    session: Session,
    *,
    workspace_id: str,
    provider_call_attempt_id: str,
    event_type: str,
    payload: dict,
    executor: str,
    evidence_refs: dict | None = None,
    trace_id: str = "",
    request_id: str = "",
) -> ProviderAttemptEventRecord:
    """Append one immutable outcome/usage event and advance the CAS
    projection derived from it in the same flush."""
    proj = _get_projection(session, workspace_id, provider_call_attempt_id)
    next_seq = proj.last_event_sequence + 1
    idem_key = f"{provider_call_attempt_id}:{event_type}:{next_seq}"

    event = ProviderAttemptEventRecord(
        id=new_id("pae"),
        workspace_id=workspace_id,
        provider_call_attempt_id=provider_call_attempt_id,
        sequence=next_seq,
        event_type=event_type,
        payload_json=payload,
        evidence_refs_json=evidence_refs or {},
        idempotency_key=idem_key,
        executor=executor,
        trace_id=trace_id,
        request_id=request_id,
        created_at=utcnow(),
    )
    session.add(event)
    _advance_projection(session, proj, event)
    return event


def rebuild_projection(
    session: Session,
    workspace_id: str,
    provider_call_attempt_id: str,
) -> ProviderAttemptProjectionRecord:
    """Rebuild the rebuildable projection by replaying the immutable event
    stream from scratch. Never touches the event rows themselves."""
    proj = _get_projection(session, workspace_id, provider_call_attempt_id)
    events = (
        session.execute(
            select(ProviderAttemptEventRecord)
            .where(
                ProviderAttemptEventRecord.workspace_id == workspace_id,
                ProviderAttemptEventRecord.provider_call_attempt_id == provider_call_attempt_id,
            )
            .order_by(ProviderAttemptEventRecord.sequence)
        )
        .scalars()
        .all()
    )
    # CAS-guarded reset, same pattern as _advance_projection below: never a
    # bare ORM attribute mutation, so every write to this row -- including
    # the rebuild's reset -- goes through `revision = expected`.
    expected_revision = proj.revision
    result = session.execute(
        update(ProviderAttemptProjectionRecord)
        .where(
            ProviderAttemptProjectionRecord.id == proj.id,
            ProviderAttemptProjectionRecord.revision == expected_revision,
        )
        .values(
            dispatch_certainty="not_dispatched",
            outcome="pending",
            usage_summary_json={},
            settlement_state="pending",
            reconciliation_state="none",
            last_event_sequence=0,
            revision=expected_revision + 1,
            updated_at=utcnow(),
        )
    )
    if result.rowcount != 1:
        raise StaleProjectionError(
            f"ProviderAttemptProjection {proj.id} was concurrently modified "
            f"(expected revision {expected_revision})"
        )
    session.flush()
    session.refresh(proj)

    for event in events:
        _advance_projection(session, proj, event)
    return proj


def _get_projection(
    session: Session,
    workspace_id: str,
    provider_call_attempt_id: str,
) -> ProviderAttemptProjectionRecord:
    proj = session.execute(
        select(ProviderAttemptProjectionRecord).where(
            ProviderAttemptProjectionRecord.workspace_id == workspace_id,
            ProviderAttemptProjectionRecord.provider_call_attempt_id == provider_call_attempt_id,
        )
    ).scalar_one()
    return proj


def _advance_projection(
    session: Session,
    proj: ProviderAttemptProjectionRecord,
    event: ProviderAttemptEventRecord,
) -> None:
    # Compute next state from the in-memory snapshot; the CAS UPDATE below
    # is what actually commits it, guarded by `revision = expected`.
    dispatch_certainty = proj.dispatch_certainty
    outcome = proj.outcome
    usage_summary_json = proj.usage_summary_json
    reconciliation_state = proj.reconciliation_state

    match event.event_type:
        case "dispatch_started":
            dispatch_certainty = "dispatched"
        case "response_received":
            outcome = "succeeded"
            dispatch_certainty = "dispatched"
        case "failure_classified":
            classification = event.payload_json.get("classification", "terminal")
            if classification == "terminal":
                outcome = "failed"
            elif classification == "uncertain":
                outcome = "uncertain"
                dispatch_certainty = "uncertain"
        case "usage_reported":
            usage_summary_json = event.payload_json.get("usage", {})
        case "certainty_resolved":
            resolution = event.payload_json.get("resolution")
            if resolution == "confirmed_dispatched":
                dispatch_certainty = "dispatched"
            elif resolution == "confirmed_not_dispatched":
                dispatch_certainty = "not_dispatched"
                if outcome == "uncertain":
                    outcome = "failed"
        case "reconciled":
            reconciliation_state = "reconciled"
            outcome = event.payload_json.get("outcome", outcome)
        case "legacy_state_imported":
            dispatch_certainty = event.payload_json.get(
                "dispatch_certainty", dispatch_certainty
            )
            outcome = event.payload_json.get("outcome", outcome)

    expected_revision = proj.revision
    result = session.execute(
        update(ProviderAttemptProjectionRecord)
        .where(
            ProviderAttemptProjectionRecord.id == proj.id,
            ProviderAttemptProjectionRecord.revision == expected_revision,
        )
        .values(
            last_event_sequence=event.sequence,
            revision=expected_revision + 1,
            updated_at=utcnow(),
            dispatch_certainty=dispatch_certainty,
            outcome=outcome,
            usage_summary_json=usage_summary_json,
            reconciliation_state=reconciliation_state,
        )
    )
    if result.rowcount != 1:
        raise StaleProjectionError(
            f"ProviderAttemptProjection {proj.id} was concurrently modified "
            f"(expected revision {expected_revision})"
        )
    session.flush()
    session.refresh(proj)
