"""Paid market-data calls: reserve at admission, dispatch through the fence, settle by credits.

Every call a resolve or a scan makes is one price-table operation (spec §6)
and one ``ProviderCallAttempt`` (``step_kind='market_data'``) behind the
dispatch fence of :class:`~app.providers.dispatch.ExternalCallCoordinator`,
exactly like ``llm_chat``:

* the admitting command reserves each call's maximum
  (:func:`reserve_market_data_calls`) under one Operation, so the Spend
  Guardrail sees the whole Job at once and any refusal rejects all of it;
* the Worker dispatches each call with :func:`execute_market_data_call`; a
  call is identified by its Job and its name (``page_search:1``,
  ``probe:2``), so a retried Job replays recorded outcomes instead of
  paying again;
* :class:`AdLibraryCallAdapter` settles what the provider reported
  (``credits_charged`` x the deployment's ``credit_usd``) and the
  coordinator releases the rest of that call's reservation;
* calls a Job will never make are released (:func:`release_market_data_call`).

Under Controlled Mock nothing is reserved: attempts are ``free_mock`` and
their usage carries a simulated amount only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, ContextManager, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.competitors.sources.base import AdLibrarySource, AdLibrarySourceError, SourceRequest, SourceResponse
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.policy import (
    EntitlementPolicy,
    FeatureNotEntitledError,
    LeaseExpiredError,
    NoEntitlementProjectionError,
    NumericEntitlementExceededError,
    WorkspaceSuspendedError,
)
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import GuardrailExceededError, ReservationBook
from app.entitlements.types import BillableCallSpec, EntitlementRequirements, ReservationPlan
from app.generation.models import ProviderAttemptEventRecord
from app.generation.spend import ensure_spend_guardrail
from app.market_data.pricing import MarketDataPrice, price_table
from app.platform.tenancy import TenantContext
from app.providers.adapter import ProviderCapabilities, ProviderResult, ReportedCost
from app.providers.dispatch import CoordinatedCallResult, ExternalCallCoordinator

MARKET_DATA_STEP_KIND = "market_data"
CHARGEABILITY_FOR_MODE = {"byok": "charged", "mock": "free_mock"}


class MarketDataCallError(Exception):
    """A call could not be admitted; ``code`` is what the interface renders."""

    def __init__(self, code: str, message: str, params: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.params = params or {}


@dataclass(frozen=True)
class PlannedCall:
    """One call a Job may make: its name within the Job and its price operation."""

    call_name: str
    operation: str


def market_data_call_key(job_id: str, call_name: str) -> str:
    """A call's identity: its Job and its name (64 hex, like ``llm_call_key``)."""
    if not job_id or not call_name:
        raise ValueError("a market-data call needs a job_id and a call_name")
    return hashlib.sha256(f"market_data:{job_id}:{call_name}".encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def reserve_market_data_calls(
    session: Session,
    ctx: TenantContext,
    *,
    operation_id: str,
    job_id: str,
    provider: str,
    calls: Sequence[PlannedCall],
) -> dict[str, str]:
    """Reserve every call's maximum in the caller's transaction (byok only).

    Returns ``{call_name: spend_reservation_id}``. Raises
    :class:`MarketDataCallError` (``entitlement_unavailable``,
    ``guardrail_exceeded``) or the pricing error of an unusable price
    table; the caller's transaction then rolls the whole command back.
    """
    prices = price_table(provider)

    try:
        entitlement_ref = EntitlementPolicy().authorize(
            session, ctx, EntitlementRequirements(feature_keys=(), numeric_requirements=()), _utcnow(),
        )
    except (
        NoEntitlementProjectionError,
        LeaseExpiredError,
        WorkspaceSuspendedError,
        FeatureNotEntitledError,
        NumericEntitlementExceededError,
    ) as exc:
        raise MarketDataCallError("entitlement_unavailable", str(exc)) from exc

    guardrail = ensure_spend_guardrail(session, ctx)
    budget_currency = guardrail.budget_currency or "USD"
    keys = {call.call_name: market_data_call_key(job_id, call.call_name) for call in calls}
    registry = {provider: {op: prices[op].unit_usd for op in {call.operation for call in calls}}}
    bundle = CostQuoteEngine(registry, config.MARKETAI_PRICE_REGISTRY_VERSION).bundle(
        [
            BillableCallSpec(call_semantic_key_hash=keys[call.call_name], provider=provider,
                             model=call.operation, quantity=1)
            for call in calls
        ],
        execution_mode="byok",
        budget_currency=budget_currency,
    )
    try:
        reserved = ReservationBook().reserve(session, ctx, ReservationPlan(
            quote_bundle=bundle,
            capacity_requests=(),
            entitlement_ref=entitlement_ref,
            operation_id=operation_id,
            job_ids_by_call_key={key: job_id for key in keys.values()},
        ))
    except GuardrailExceededError as exc:
        raise MarketDataCallError(
            "guardrail_exceeded",
            f"{exc.limit_name}: this would bring spend to {exc.proposed_value} {budget_currency}, "
            f"above the limit of {exc.limit_value} {budget_currency}",
        ) from exc
    return {name: reserved.spend_reservation_by_call_key[key] for name, key in keys.items()}


def open_reservation(session: Session, ctx: TenantContext, *, job_id: str, call_name: str) -> SpendReservationRecord | None:
    return session.execute(
        select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.semantic_key_hash == market_data_call_key(job_id, call_name),
            SpendReservationRecord.state == "reserved",
        )
    ).scalars().first()


def release_market_data_call(session: Session, ctx: TenantContext, *, job_id: str, call_name: str) -> bool:
    """Release a reserved call that will never be made; ``False`` when nothing was open.

    A reservation an attempt already holds is the coordinator's to settle,
    release or leave ``reserved`` for reconciliation -- never released here.
    """
    reservation = open_reservation(session, ctx, job_id=job_id, call_name=call_name)
    if reservation is None:
        return False
    in_use = session.execute(
        select(ProviderCallAttemptRecord.id).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.spend_reservation_id == reservation.id,
        )
    ).first()
    if in_use is not None:
        return False
    now = _utcnow()
    reservation.state = "released"
    reservation.outstanding_budget_amount = 0
    reservation.released_at = now
    reservation.updated_at = now
    session.flush()
    return True


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class AdLibraryCallAdapter:
    """``ExternalCallAdapter[SourceRequest, SourceResponse]`` over an ``AdLibrarySource``.

    Settles what the provider reported: ``credits_charged`` x ``credit_usd``
    of the call's operation. Under Mock the payable amount is zero and the
    same figure is the simulated amount (``None`` without a price table).
    A failure the provider charged for carries its cost too.
    """

    capabilities = ProviderCapabilities(
        accepts_idempotency_key=False, supports_result_lookup_by_request_id=False,
    )

    def __init__(
        self,
        source: AdLibrarySource,
        *,
        execution_mode: str,
        prices: Mapping[str, MarketDataPrice] | None,
        pricing_version: str,
    ) -> None:
        self._source = source
        self.provider = source.descriptor.provider
        self._mock = execution_mode == "mock"
        self._prices = prices
        self._pricing_version = pricing_version

    def _cost(self, operation: str, charged: int | None, remaining: int | None, cached: bool,
              evidence: Mapping[str, Any]) -> ReportedCost | None:
        if charged is None:
            return None
        tries = getattr(self._source, "last_tries", None)
        if tries:  # a retrying source (scans): every try, for reconciliation windows
            evidence = {**evidence, "tries": list(tries)}
        price = (self._prices or {}).get(operation)
        amount = price.credit_usd * charged if price is not None else None
        if not self._mock and amount is None:
            return None  # no price: the coordinator settles the reservation's maximum
        return ReportedCost(
            provider_amount=Decimal("0") if self._mock else amount,
            provider_currency="USD",
            price_rule_version=self._pricing_version,
            usage={"operation": operation, "credits_charged": charged,
                   "credits_remaining": remaining, "cached": cached},
            evidence=dict(evidence),
            simulated_amount=amount if self._mock else None,
        )

    async def execute(
        self, request: SourceRequest, *, credential: memoryview | None, idempotency_key: str,
    ) -> ProviderResult[SourceResponse]:
        del idempotency_key  # ScrapeCreators takes none; the fence prevents resends
        try:
            response = await self._source.call(request, credential=credential)
        except AdLibrarySourceError as exc:
            exc.reported_cost = self._cost(
                request.operation, exc.credits_charged, exc.credits_remaining, False, exc.evidence(),
            )
            raise
        return ProviderResult(
            output=response,
            provider_request_id=None,
            idempotency_key_accepted=False,
            reported_cost=self._cost(
                request.operation, response.credits_charged, response.credits_remaining,
                response.cached, response.evidence(),
            ),
        )


def raw_digest(raw: Any) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


async def execute_market_data_call(
    coordinator: ExternalCallCoordinator,
    ctx: TenantContext,
    *,
    job_id: str,
    operation_id: str,
    call_name: str,
    request: SourceRequest,
    adapter: AdLibraryCallAdapter,
    evidence_recorder: Any,
    execution_mode: str,
    credential: memoryview | None,
    credential_version_id: str | None,
    session_factory: Callable[[TenantContext], ContextManager[Session]],
) -> CoordinatedCallResult[SourceResponse]:
    """Run (or replay) one call behind the fence; byok needs its open reservation."""
    reservation_id = None
    if execution_mode == "byok":
        with session_factory(ctx) as session:
            reservation = open_reservation(session, ctx, job_id=job_id, call_name=call_name)
            reservation_id = reservation.id if reservation is not None else None
    return await coordinator.execute(
        ctx,
        job_id=job_id,
        operation_id=operation_id,
        call_semantic_key_hash=market_data_call_key(job_id, call_name),
        step_kind=MARKET_DATA_STEP_KIND,
        execution_mode=execution_mode,
        chargeability=CHARGEABILITY_FOR_MODE[execution_mode],
        provider=adapter.provider,
        request=request,
        adapter=adapter,
        evidence_recorder=evidence_recorder,
        credential=credential,
        spend_reservation_id=reservation_id,
        model=request.operation.value,
        credential_version_id=credential_version_id,
    )


def call_usage(session: Session, ctx: TenantContext, attempt_id: str) -> dict[str, Any]:
    """The ``usage_reported`` payload of one attempt (``{}`` when none)."""
    event = session.execute(
        select(ProviderAttemptEventRecord)
        .where(
            ProviderAttemptEventRecord.workspace_id == ctx.workspace_id,
            ProviderAttemptEventRecord.provider_call_attempt_id == attempt_id,
            ProviderAttemptEventRecord.event_type == "usage_reported",
        )
        .order_by(ProviderAttemptEventRecord.sequence.desc())
    ).scalars().first()
    return dict(event.payload_json or {}) if event is not None else {}


#: Provider failure -> the code the interface renders. Source codes such as
#: ``credential_rejected`` already carry AI-provider copy, so market-data
#: failures get their own names; the source code travels as ``provider_code``.
FAILURE_INSUFFICIENT_CREDITS = "market_data_insufficient_credits"
FAILURE_CREDENTIAL_REJECTED = "market_data_credential_rejected"
FAILURE_CREDENTIAL_UNAVAILABLE = "market_data_credential_unavailable"
FAILURE_RATE_LIMITED = "market_data_rate_limited"
FAILURE_UNSUPPORTED_COUNTRY = "market_data_unsupported_country"
FAILURE_UNREACHABLE = "market_data_unreachable"
FAILURE_PROVIDER_ERROR = "market_data_provider_error"
FAILURE_OUTCOME_UNKNOWN = "market_data_outcome_unknown"

_INTERFACE_CODES = {
    "insufficient_credits": FAILURE_INSUFFICIENT_CREDITS,
    "credential_rejected": FAILURE_CREDENTIAL_REJECTED,
    "credential_unavailable": FAILURE_CREDENTIAL_UNAVAILABLE,
    "rate_limited": FAILURE_RATE_LIMITED,
    "unsupported_country": FAILURE_UNSUPPORTED_COUNTRY,
    "provider_unreachable": FAILURE_UNREACHABLE,
}


def interface_failure(provider_code: str | None, dispatch_certainty: str) -> tuple[str, dict[str, Any]]:
    """``(code, params)`` for a failed call: what the page shows, with the source code as a param."""
    source = provider_code or "provider_error"
    if dispatch_certainty == "uncertain":
        return FAILURE_OUTCOME_UNKNOWN, {"provider_code": source}
    return _INTERFACE_CODES.get(source, FAILURE_PROVIDER_ERROR), {"provider_code": source}
