"""ReservationBook: atomic reservation within caller's transaction.

Spec section 3.6.  Guardrail check, capacity check, and Reservation insert
are merged into one module to prevent check-then-reserve TOCTOU.

Internal fixed order (per spec):
  1. Lock SpendGuardrail row (SELECT ... FOR UPDATE on PostgreSQL; plain SELECT on SQLite).
  2. Calculate all four guardrail formulas against current outstanding + new amount.
  3. Check per_operation_limit, rolling_24h_limit, period_limit, outstanding_exposure_limit.
  4. If any exceeded, raise ``GuardrailExceededError`` with which limit.
  5. Create SpendReservationRecord for each chargeable (non-mock) quote.
  6. Write Quote and Reservation ProviderCostLedgerEntry rows.
  7. Create CapacityReservationRecord for ``reserve_on_admission`` capacity requests.
  8. Return ReservationBundle.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text, func
from sqlalchemy.orm import Session

from app.entitlements.cost_models import (
    CapacityReservationRecord,
    ProviderCostLedgerEntryRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.types import (
    ExecutionMode,
    ReservationBundle,
    ReservationPlan,
)
from app.platform.tenancy import TenantContext


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _ledger_key(entry_type: str, call_semantic_key_hash: str) -> str:
    """The ledger row's ``semantic_key_hash`` for one entry of one call.

    ``provider_cost_ledger.semantic_key_hash`` is ``varchar(64)`` and unique
    per Workspace. Call keys are themselves 64-hex sha256 digests, so a
    ``quote_``/``resv_`` prefix overflowed the column on PostgreSQL (SQLite
    does not enforce lengths): hash the namespaced key instead, which keeps
    one quote and one reservation row per call and fits the column.
    """
    return hashlib.sha256(
        f"ledger:{entry_type}:{call_semantic_key_hash}".encode("utf-8")
    ).hexdigest()


def ledger_entry_key(entry_type: str, call_semantic_key_hash: str) -> str:
    """Public name of :func:`_ledger_key` for settlement-side writers.

    The dispatch coordinator writes the ``reported_cost`` and ``release``
    rows of a call it settles; they share this namespacing so every ledger
    row of one call has a distinct 64-hex key.
    """
    return _ledger_key(entry_type, call_semantic_key_hash)


def capacity_reservation_key(entitlement_key: str, operation_id: str) -> str:
    """The ``semantic_key_hash`` of an Operation's admission capacity reservation.

    Admission recovery looks the reservation up by this key, so the writer and
    the reader must share it. ``entitlement_key`` can be 128 characters and the
    column is ``varchar(64)``, so the key is hashed like the ledger keys.
    """
    return _ledger_key(f"capacity:{entitlement_key}", operation_id)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GuardrailExceededError(Exception):
    """Raised when a guardrail limit would be exceeded by the proposed reservation."""

    def __init__(self, limit_name: str, limit_value: Decimal, proposed_value: Decimal) -> None:
        self.limit_name = limit_name
        self.limit_value = limit_value
        self.proposed_value = proposed_value
        super().__init__(
            f"{limit_name}: proposed {proposed_value} exceeds limit {limit_value}"
        )


class ReservationBook:
    """Atomic reservation engine that enforces spend guardrails."""

    def reserve(
        self,
        session: Session,
        context: TenantContext,
        plan: ReservationPlan,
    ) -> ReservationBundle:
        """Reserve spend and capacity atomically within the caller's transaction."""
        workspace_id = context.workspace_id
        now = _utcnow()

        # Step 1: Lock SpendGuardrail row
        guardrail = self._lock_guardrail(session, workspace_id)

        # Step 2-3: Calculate and check guardrail formulas
        proposed = plan.quote_bundle.total_budget_amount
        self._check_guardrails(session, guardrail, proposed, now)

        # Step 5-6: Create SpendReservations and Ledger entries for non-mock quotes
        spend_map: dict[str, str] = {}
        is_mock = all(
            q.execution_mode == ExecutionMode.MOCK
            for q in plan.quote_bundle.call_quotes
        )

        if not is_mock:
            for quote in plan.quote_bundle.call_quotes:
                job_id = plan.job_ids_by_call_key[quote.call_semantic_key_hash]
                reservation_id = _new_id("sr")
                generation = (plan.generation_contexts or {}).get(quote.call_semantic_key_hash)

                session.add(SpendReservationRecord(
                    id=reservation_id,
                    workspace_id=workspace_id,
                    operation_id=plan.operation_id,
                    job_id=job_id,
                    run_item_id=generation.run_item_id if generation else None,
                    preview_confirmation_id=generation.preview_confirmation_id if generation else None,
                    recipe_snapshot_id=generation.recipe_snapshot_id if generation else None,
                    semantic_key_hash=quote.call_semantic_key_hash,
                    state="reserved",
                    provider=quote.provider,
                    execution_mode=str(quote.execution_mode),
                    provider_currency=quote.provider_currency,
                    maximum_provider_amount=quote.provider_amount,
                    budget_currency=quote.budget_currency,
                    maximum_budget_amount=quote.budget_amount,
                    outstanding_budget_amount=quote.budget_amount,
                    period_start=plan.entitlement_ref.period_start,
                    period_end=plan.entitlement_ref.period_end,
                    entitlement_version=plan.entitlement_ref.entitlement_version,
                    lease_sequence=plan.entitlement_ref.lease_sequence,
                    guardrail_revision=guardrail.revision,
                    platform_policy_version=guardrail.platform_policy_version,
                    created_at=now,
                    updated_at=now,
                ))
                spend_map[quote.call_semantic_key_hash] = reservation_id

            session.flush()

            for quote in plan.quote_bundle.call_quotes:
                reservation_id = spend_map[quote.call_semantic_key_hash]
                job_id = plan.job_ids_by_call_key[quote.call_semantic_key_hash]

                session.add(ProviderCostLedgerEntryRecord(
                    id=_new_id("le"),
                    workspace_id=workspace_id,
                    operation_id=plan.operation_id,
                    job_id=job_id,
                    spend_reservation_id=reservation_id,
                    entry_type="quote",
                    direction="increase",
                    execution_mode=str(quote.execution_mode),
                    semantic_key_hash=_ledger_key("quote", quote.call_semantic_key_hash),
                    provider=quote.provider,
                    model=quote.model,
                    provider_amount=quote.provider_amount,
                    provider_currency=quote.provider_currency,
                    budget_amount=quote.budget_amount,
                    budget_currency=quote.budget_currency,
                    exchange_rate=quote.exchange_rate,
                    exchange_rate_source=quote.exchange_rate_source,
                    simulated_amount=quote.simulated_amount,
                    simulated_currency=quote.simulated_currency,
                    created_at=now,
                ))

                session.add(ProviderCostLedgerEntryRecord(
                    id=_new_id("le"),
                    workspace_id=workspace_id,
                    operation_id=plan.operation_id,
                    job_id=job_id,
                    spend_reservation_id=reservation_id,
                    entry_type="reservation",
                    direction="increase",
                    execution_mode=str(quote.execution_mode),
                    semantic_key_hash=_ledger_key("reservation", quote.call_semantic_key_hash),
                    provider=quote.provider,
                    model=quote.model,
                    provider_amount=quote.provider_amount,
                    provider_currency=quote.provider_currency,
                    budget_amount=quote.budget_amount,
                    budget_currency=quote.budget_currency,
                    exchange_rate=quote.exchange_rate,
                    exchange_rate_source=quote.exchange_rate_source,
                    simulated_amount=quote.simulated_amount,
                    simulated_currency=quote.simulated_currency,
                    created_at=now,
                ))

        # Step 7: Create CapacityReservations for admission-phase requests only
        capacity_ids: list[str] = []
        for cap_req in plan.capacity_requests:
            if cap_req.acquisition_phase != "reserve_on_admission":
                continue

            cap_id = _new_id("cr")
            first_call_key = next(iter(plan.job_ids_by_call_key))
            job_id = plan.job_ids_by_call_key[first_call_key]

            session.add(CapacityReservationRecord(
                id=cap_id,
                workspace_id=workspace_id,
                operation_id=plan.operation_id,
                job_id=job_id,
                entitlement_key=cap_req.entitlement_key,
                quantity=int(cap_req.quantity),
                unit=cap_req.unit,
                acquisition_phase="admission",
                semantic_key_hash=capacity_reservation_key(
                    cap_req.entitlement_key, plan.operation_id
                ),
                state="reserved",
                entitlement_version=plan.entitlement_ref.entitlement_version,
                lease_sequence=plan.entitlement_ref.lease_sequence,
                created_at=now,
                updated_at=now,
            ))
            capacity_ids.append(cap_id)

        session.flush()

        job_ids = tuple(plan.job_ids_by_call_key.values())

        return ReservationBundle(
            operation_id=plan.operation_id,
            spend_reservation_by_call_key=spend_map,
            capacity_reservation_ids=tuple(capacity_ids),
            job_ids=job_ids,
            guardrail_revision=guardrail.revision,
        )

    def _lock_guardrail(
        self, session: Session, workspace_id: str,
    ) -> SpendGuardrailRecord:
        # SQLite leaves the lock out of the SQL; the guard (#59) still sees it.
        guardrail = session.query(SpendGuardrailRecord).filter_by(
            workspace_id=workspace_id,
        ).with_for_update().one()
        return guardrail

    def _check_guardrails(
        self,
        session: Session,
        guardrail: SpendGuardrailRecord,
        proposed_budget_amount: Decimal,
        now: datetime,
    ) -> None:
        workspace_id = guardrail.workspace_id

        if proposed_budget_amount > guardrail.per_operation_limit:
            raise GuardrailExceededError(
                "per_operation_limit",
                guardrail.per_operation_limit,
                proposed_budget_amount,
            )

        outstanding = self._current_outstanding(session, workspace_id)

        settled_24h = self._settled_amount_since(session, workspace_id, now - timedelta(hours=24))
        rolling_24h = settled_24h + outstanding + proposed_budget_amount
        if rolling_24h > guardrail.rolling_24h_limit:
            raise GuardrailExceededError(
                "rolling_24h_limit",
                guardrail.rolling_24h_limit,
                rolling_24h,
            )

        period_settled = self._period_settled(session, workspace_id)
        period_total = period_settled + outstanding + proposed_budget_amount
        if period_total > guardrail.period_limit:
            raise GuardrailExceededError(
                "period_limit",
                guardrail.period_limit,
                period_total,
            )

        total_outstanding = outstanding + proposed_budget_amount
        if total_outstanding > guardrail.outstanding_exposure_limit:
            raise GuardrailExceededError(
                "outstanding_exposure_limit",
                guardrail.outstanding_exposure_limit,
                total_outstanding,
            )

    def _current_outstanding(self, session: Session, workspace_id: str) -> Decimal:
        result = session.query(
            func.coalesce(func.sum(SpendReservationRecord.outstanding_budget_amount), 0),
        ).filter(
            SpendReservationRecord.workspace_id == workspace_id,
            SpendReservationRecord.state.in_(("reserved", "partially_settled", "uncertain")),
        ).scalar()
        return Decimal(str(result))

    @staticmethod
    def _direction_aware_sum():
        """SUM that accounts for direction: 'decrease' entries are negative."""
        from sqlalchemy import case, literal_column
        return func.coalesce(
            func.sum(
                case(
                    (ProviderCostLedgerEntryRecord.direction == "decrease",
                     -ProviderCostLedgerEntryRecord.budget_amount),
                    else_=ProviderCostLedgerEntryRecord.budget_amount,
                )
            ),
            literal_column("0"),
        )

    def _settled_amount_since(
        self, session: Session, workspace_id: str, since: datetime | None,
    ) -> Decimal:
        if session.get_bind().dialect.name == 'postgresql':
            # Runtime API cannot read raw ledger rows. The database verifies
            # the signed TenantContext before returning this bounded total.
            total = session.scalar(text('SELECT public.marketai_settled_cost_total(:workspace, :since)'),
                {'workspace': workspace_id, 'since': since})
            return Decimal(str(total))
        result = session.query(
            self._direction_aware_sum(),
        ).filter(
            ProviderCostLedgerEntryRecord.workspace_id == workspace_id,
            ProviderCostLedgerEntryRecord.entry_type.in_(
                ("reported_cost", "estimated_settlement", "adjustment"),
            ),
            ProviderCostLedgerEntryRecord.created_at >= since,
        ).scalar()
        return Decimal(str(result))

    def _period_settled(self, session: Session, workspace_id: str) -> Decimal:
        if session.get_bind().dialect.name == 'postgresql':
            return self._settled_amount_since(session, workspace_id, None)
        result = session.query(
            self._direction_aware_sum(),
        ).filter(
            ProviderCostLedgerEntryRecord.workspace_id == workspace_id,
            ProviderCostLedgerEntryRecord.entry_type.in_(
                ("reported_cost", "estimated_settlement", "adjustment"),
            ),
        ).scalar()
        return Decimal(str(result))
