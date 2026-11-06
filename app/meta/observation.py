# app/meta/observation.py
"""Observation contract generations and the 14-day coverage ledger (spec §6.1, §6.6-§6.7).

Three operations:

* :func:`create_contract_generation` -- creates an immutable
  ``ObservationContractGenerationRecord``. Per §6.7, creating the *first*
  generation for a ``(workspace_id, meta_mode, meta_connection_id,
  contract_type)`` scope cannot rely on ``SELECT ... FOR UPDATE`` (there is
  no row yet to lock), so a transaction-scoped Postgres advisory lock keyed
  on the scope is acquired first. This serializes concurrent "create the
  first generation" attempts without a row to contend over. On SQLite
  (tests, local dev) advisory locks do not exist; the partial unique index
  ``uq_ocg_active_scope`` on ``ObservationContractGenerationRecord``
  (``state = 'active'``) is the backstop that still prevents two active
  generations for the same scope there.
* :func:`record_coverage` -- appends an ``ObservationCoverageLedgerRecord``
  for one ``(contract_generation_id, coverage_date)``, and starts the
  generation's observation clock (``clock_started_at``) on the first entry
  (§6.6.2).
* :func:`calculate_tenure` -- derives tenure from the ledger as the longest
  run of consecutive ``coverage_date`` values (§6.6.3, §6.6.5) rather than a
  stored, mutable counter.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..adapters.persistence.models import new_id, utcnow
from .models import ObservationContractGenerationRecord, ObservationCoverageLedgerRecord

__all__ = [
    "ContractError",
    "create_contract_generation",
    "record_coverage",
    "calculate_tenure",
]


class ContractError(ValueError):
    """Raised for observation contract generation invariant violations."""

    def __init__(self, message: str = "", *, code: str = "meta_contract_invalid") -> None:
        super().__init__(message)
        self.code = code


def _contract_hash(
    *,
    ad_account_id: str,
    fields: list,
    attribution_window: dict,
    breakdowns: list,
    time_window: dict,
    delivery_config: dict | None,
) -> str:
    """SHA-256 of the canonical contract definition (spec §6.1).

    Detects configuration drift that requires a new generation. It is
    deliberately not a uniqueness key -- reverted configurations may repeat
    a hash across generations; ``generation_number`` provides uniqueness.
    """
    canonical = json.dumps(
        {
            "ad_account_id": ad_account_id,
            "fields": fields,
            "attribution_window": attribution_window,
            "breakdowns": breakdowns,
            "time_window": time_window,
            "delivery_config": delivery_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _advisory_lock_key(workspace_id: str, meta_mode: str, connection_id: str, contract_type: str) -> int:
    """Deterministic signed-bigint key for ``pg_advisory_xact_lock``."""
    scope = f"{workspace_id}:{meta_mode}:{connection_id}:{contract_type}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return int(digest, 16) % (2**63 - 1)


def create_contract_generation(
    session: Session,
    *,
    workspace_id: str,
    meta_mode: str,
    connection_id: str,
    contract_type: str,
    ad_account_id: str,
    fields: list,
    attribution_window: dict,
    breakdowns: list,
    time_window: dict,
    local_timezone: str,
    delivery_config: dict | None = None,
) -> ObservationContractGenerationRecord:
    """Create a new active ``ObservationContractGenerationRecord``.

    Raises :class:`ContractError` if an active generation already exists
    for this ``(workspace_id, meta_mode, connection_id, contract_type)``
    scope -- generations are retired explicitly (elsewhere), never
    implicitly replaced. ``generation_number`` is one past the highest
    number ever issued in this scope (active or retired), so retirement and
    re-creation never reuses a number.
    """
    if session.get_bind().dialect.name == "postgresql":
        # §6.7: no row exists yet for the first generation in this scope,
        # so a transaction-scoped advisory lock -- not `FOR UPDATE` --
        # serializes concurrent "create the first generation" attempts.
        # Released automatically at transaction commit/rollback.
        lock_key = _advisory_lock_key(workspace_id, meta_mode, connection_id, contract_type)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    existing_active = session.scalar(
        select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == workspace_id,
            ObservationContractGenerationRecord.meta_mode == meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection_id,
            ObservationContractGenerationRecord.contract_type == contract_type,
            ObservationContractGenerationRecord.state == "active",
        )
    )
    if existing_active is not None:
        raise ContractError(
            code="meta_contract_already_exists",
            message="active generation already exists for "
            f"workspace_id={workspace_id!r} meta_mode={meta_mode!r} "
            f"connection_id={connection_id!r} contract_type={contract_type!r}"
        )

    max_generation_number = session.scalar(
        select(func.max(ObservationContractGenerationRecord.generation_number)).where(
            ObservationContractGenerationRecord.workspace_id == workspace_id,
            ObservationContractGenerationRecord.meta_mode == meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection_id,
            ObservationContractGenerationRecord.contract_type == contract_type,
        )
    )
    generation_number = (max_generation_number or 0) + 1

    generation = ObservationContractGenerationRecord(
        id=new_id("ocg"),
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=connection_id,
        contract_type=contract_type,
        generation_number=generation_number,
        ad_account_id=ad_account_id,
        fields=fields,
        attribution_window=attribution_window,
        breakdowns=breakdowns,
        time_window=time_window,
        delivery_config=delivery_config,
        contract_hash=_contract_hash(
            ad_account_id=ad_account_id,
            fields=fields,
            attribution_window=attribution_window,
            breakdowns=breakdowns,
            time_window=time_window,
            delivery_config=delivery_config,
        ),
        local_timezone=local_timezone,
        state="active",
        clock_started_at=None,
    )
    session.add(generation)
    session.flush()
    return generation


def record_coverage(
    session: Session,
    *,
    generation_id: str,
    coverage_date: date,
    sync_run_id: str,
    attestation_id: str,
) -> ObservationCoverageLedgerRecord:
    """Append one ``(contract_generation_id, coverage_date)`` ledger entry.

    ``workspace_id`` and ``meta_mode`` are read from the contract
    generation, not accepted as parameters -- a coverage entry cannot
    disagree with the generation it covers. Starts the generation's
    observation clock on the first entry ever recorded for it (§6.6.2):
    coverage begins when the first ledger entry is written, not when the
    generation row was created.
    """
    generation = session.get(ObservationContractGenerationRecord, generation_id)
    if generation is None:
        raise ContractError(f"no contract generation {generation_id!r}", code="meta_contract_not_found")

    entry = ObservationCoverageLedgerRecord(
        id=new_id("ocl"),
        workspace_id=generation.workspace_id,
        meta_mode=generation.meta_mode,
        contract_generation_id=generation.id,
        coverage_date=coverage_date,
        sync_run_id=sync_run_id,
        attestation_id=attestation_id,
    )
    session.add(entry)

    if generation.clock_started_at is None:
        generation.clock_started_at = utcnow()

    session.flush()
    return entry


def calculate_tenure(session: Session, generation_id: str) -> int:
    """Longest run of consecutive ``coverage_date`` values in the ledger (§6.6.3).

    Derived on demand from ledger entries rather than tracked as a mutable
    counter (§6.6.5), so it self-corrects when coverage entries are removed
    (e.g. after an attestation is invalidated, §6.9.5).
    """
    coverage_dates = session.scalars(
        select(ObservationCoverageLedgerRecord.coverage_date)
        .where(ObservationCoverageLedgerRecord.contract_generation_id == generation_id)
        .order_by(ObservationCoverageLedgerRecord.coverage_date)
    ).all()
    if not coverage_dates:
        return 0

    longest = current = 1
    for previous_date, current_date in zip(coverage_dates, coverage_dates[1:]):
        if current_date - previous_date == timedelta(days=1):
            current += 1
        else:
            current = 1
        longest = max(longest, current)
    return longest
