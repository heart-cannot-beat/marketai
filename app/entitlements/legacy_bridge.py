"""Compatibility view that projects legacy CostLedgerRecord rows into the new
ProviderCostLedgerEntryRecord schema.

Task 20: the legacy system tracks costs with CostLedgerRecord (entry_type
reserved/released/actual), while the new system uses ProviderCostLedgerEntryRecord.
This module queries old rows and maps them to a frozen dataclass compatible
with the new schema so reporting queries can operate on a single shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import CostLedgerRecord, GenerationRunRecord

# Entry-type mapping: legacy -> new schema
_ENTRY_TYPE_MAP: dict[str, str] = {
    "reserved": "reservation",
    "released": "release",
    "actual": "estimated_settlement",
}

# Direction mapping: legacy entry_type -> direction
_DIRECTION_MAP: dict[str, str] = {
    "reserved": "increase",
    "released": "decrease",
    "actual": "increase",
}


@dataclass(frozen=True)
class LegacyCostEvidence:
    """A CostLedgerRecord projected into the ProviderCostLedgerEntryRecord shape."""

    original_id: str  # "legacy_{CostLedgerRecord.id}"
    workspace_id: str
    entry_type: str  # mapped: reserved->reservation, released->release, actual->estimated_settlement
    direction: str  # reserved/actual->increase, released->decrease
    execution_mode: str  # mock->mock, non-mock->legacy_platform, missing->legacy_unknown
    provider: str
    provider_amount: Decimal
    provider_currency: str
    budget_amount: Decimal  # same as provider_amount (single currency)
    budget_currency: str  # same as provider_currency
    exchange_rate: Decimal  # always 1
    exchange_rate_source: str  # "legacy_single_currency"
    simulated_amount: Decimal | None  # non-zero for mock
    created_at: datetime


def _determine_execution_mode(provider: str | None) -> str:
    """Map a legacy provider string to an execution_mode value."""
    if provider is None:
        return "legacy_unknown"
    if provider == "mock":
        return "mock"
    return "legacy_platform"


def query_compatibility_view(
    session: Session,
    workspace_id: str,
) -> list[LegacyCostEvidence]:
    """Query all legacy CostLedgerRecord entries for *workspace_id* and return
    them mapped to :class:`LegacyCostEvidence`, which is structurally
    compatible with the new ProviderCostLedgerEntryRecord schema.

    The join through GenerationRunRecord (via run_id) resolves the provider
    that the flat CostLedgerRecord does not carry.
    """
    stmt = (
        select(CostLedgerRecord, GenerationRunRecord.provider)
        .outerjoin(
            GenerationRunRecord,
            (CostLedgerRecord.run_id == GenerationRunRecord.id)
            & (CostLedgerRecord.workspace_id == GenerationRunRecord.workspace_id),
        )
        .where(CostLedgerRecord.workspace_id == workspace_id)
        .order_by(CostLedgerRecord.created_at)
    )

    results: list[LegacyCostEvidence] = []
    for row in session.execute(stmt):
        record: CostLedgerRecord = row[0]
        provider: str | None = row[1]

        entry_type = _ENTRY_TYPE_MAP.get(record.entry_type, record.entry_type)
        direction = _DIRECTION_MAP.get(record.entry_type, "increase")
        execution_mode = _determine_execution_mode(provider)

        # For mock entries the actual cost is zero; the original amount
        # is carried in simulated_amount instead.
        if execution_mode == "mock":
            simulated_amount = record.amount
            provider_amount = Decimal("0")
            budget_amount = Decimal("0")
        else:
            simulated_amount = None
            provider_amount = record.amount
            budget_amount = record.amount

        results.append(
            LegacyCostEvidence(
                original_id=f"legacy_{record.id}",
                workspace_id=record.workspace_id,
                entry_type=entry_type,
                direction=direction,
                execution_mode=execution_mode,
                provider=provider or "unknown",
                provider_amount=provider_amount,
                provider_currency=record.currency,
                budget_amount=budget_amount,
                budget_currency=record.currency,
                exchange_rate=Decimal("1"),
                exchange_rate_source="legacy_single_currency",
                simulated_amount=simulated_amount,
                created_at=record.created_at,
            )
        )

    return results
