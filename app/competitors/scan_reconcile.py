"""Reconciling a scan page whose outcome was lost after the dispatch fence (spec §5.1).

A page call that crossed the fence and then lost its answer (the Worker
died mid-request, a timeout) is ``uncertain``: nobody knows whether the
provider charged its credit, its reservation stays open, and it is never
resent. Reconciliation only settles the money; the page itself is fetched
again as a new attempt.

**Automatic** (:func:`balance_check`), when a resume starts and holds its
key's turn: the free credit balance is compared with the
``credits_remaining`` the last successful call *before* the page reported,
less every charge known since on the same CredentialVersion -- scan pages,
resolve calls (#269's gap: an ``uncertain`` resolve no longer holds the
turn, so its calls must be counted here) and pages reconciled earlier
(a charge a retried try *reported* is part of its call's own):

    difference = (baseline remaining - known charges since) - balance now

``0`` settles "not charged" and the page's own credits (1) "charged". Any
other value, a baseline that came from a ``cached`` answer, no baseline,
another call of unknown cost in the window -- an uncertain one, or one that
succeeded after a 5xx try that may have been charged unreported -- a
different key or no balance at all is left to the Owner. Usage of the same key outside MarketAI cannot
be excluded: an exact difference of 1 can misjudge one credit (about
$0.002), a stated risk.

**Manual**: the Owner marks the page; a Worker Job settles it the same way
(:func:`settle_uncertain_call`): ``certainty_resolved`` + ``usage_reported``
+ settle or release + ``reconciled`` on the attempt, which then reads as
``failed`` with a known charge for later windows. A charged page also counts
one ``provider_calls`` Usage fact; a page that was not charged counts none.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.entitlements.usage import record_provider_call
from app.market_data.calls import MARKET_DATA_STEP_KIND, call_usage
from app.market_data.pricing import MarketDataPrice
from app.platform.tenancy import TenantContext
from app.providers.adapter import ReportedCost
from app.providers.attempts import append_event
from app.providers.dispatch import ExternalCallCoordinator

#: States whose charge is unknown until reconciled.
UNKNOWN_STATES = frozenset({"uncertain", "dispatching"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _usage(session: Session, ctx: TenantContext, attempt_id: str) -> dict[str, Any]:
    return dict(call_usage(session, ctx, attempt_id).get("usage") or {})


def _hidden_charge(session: Session, ctx: TenantContext, attempt_id: str) -> bool:
    """A retried try (5xx) that may have been charged without the provider reporting it."""
    tries = (call_usage(session, ctx, attempt_id).get("evidence") or {}).get("tries") or []
    return any(item.get("dispatch_certainty") == "uncertain" for item in tries)


def _attempts(session: Session, ctx: TenantContext, credential_version_id: str) -> list[ProviderCallAttemptRecord]:
    return list(session.scalars(
        select(ProviderCallAttemptRecord)
        .where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.step_kind == MARKET_DATA_STEP_KIND,
            ProviderCallAttemptRecord.credential_version_id == credential_version_id,
        )
        .order_by(ProviderCallAttemptRecord.prepared_at, ProviderCallAttemptRecord.id)
    ))


def _manual(reason: str, **values: Any) -> dict[str, Any]:
    return {"outcome": "manual_required", "reason": reason, "difference": None,
            "baseline_credits_remaining": None, "known_charges": None, "balance": None,
            "checked_at": _now().isoformat(), **values}


def balance_check(
    session: Session,
    ctx: TenantContext,
    *,
    attempt_id: str,
    balance: int | None,
    run_credential_version_id: str | None,
    expected_charge: int = 1,
) -> dict[str, Any]:
    """The automatic verdict on one uncertain page (shape of ``CompetitorScanBalanceCheck``)."""
    attempt = session.get(ProviderCallAttemptRecord, attempt_id)
    if attempt is None or attempt.workspace_id != ctx.workspace_id or attempt.credential_version_id is None:
        return _manual("no_baseline")
    if run_credential_version_id != attempt.credential_version_id:
        return _manual("credential_changed")
    if balance is None:
        return _manual("balance_unavailable")
    attempts = _attempts(session, ctx, attempt.credential_version_id)
    position = next(index for index, row in enumerate(attempts) if row.id == attempt.id)
    baseline_index, baseline = None, None
    for index in range(position - 1, -1, -1):
        row = attempts[index]
        if row.dispatch_state != "succeeded":
            continue
        usage = _usage(session, ctx, row.id)
        if usage.get("credits_remaining") is None:
            continue
        baseline_index, baseline = index, usage
        break
    if baseline is None:
        return _manual("no_baseline", balance=balance)
    if baseline.get("cached"):
        return _manual("cached_baseline", balance=balance,
                       baseline_credits_remaining=int(baseline["credits_remaining"]))
    known = 0
    for row in attempts[baseline_index + 1:]:
        if row.id == attempt.id or row.dispatch_state == "prepared":
            continue
        if row.dispatch_state in UNKNOWN_STATES or _hidden_charge(session, ctx, row.id):
            return _manual("unknown_charge_in_window", balance=balance,
                           baseline_credits_remaining=int(baseline["credits_remaining"]))
        known += int(_usage(session, ctx, row.id).get("credits_charged") or 0)
    remaining = int(baseline["credits_remaining"])
    difference = remaining - known - balance
    values = {"difference": difference, "baseline_credits_remaining": remaining, "known_charges": known,
              "balance": balance, "checked_at": _now().isoformat()}
    if difference == 0:
        return {"outcome": "not_charged", "reason": None, **values}
    if difference == expected_charge:
        return {"outcome": "charged", "reason": None, **values}
    return {"outcome": "manual_required", "reason": "difference", **values}


def settle_uncertain_call(
    session: Session,
    ctx: TenantContext,
    *,
    attempt_id: str,
    charged: bool,
    prices: Mapping[str, MarketDataPrice] | None,
    pricing_version: str,
    source: str,
) -> bool:
    """Settle (charged) or release (not charged) an uncertain call's reservation, once.

    Returns ``False`` when the attempt is no longer uncertain (already
    settled by an earlier run of the same Job).
    """
    attempt = session.get(ProviderCallAttemptRecord, attempt_id)
    if attempt is None or attempt.workspace_id != ctx.workspace_id or attempt.dispatch_state not in UNKNOWN_STATES:
        return False
    now = _now()
    operation = attempt.model or ""
    price = (prices or {}).get(operation)
    credits = (price.credits if price else 1) if charged else 0
    amount = price.credit_usd * credits if price is not None else Decimal("0")
    mock = attempt.execution_mode == "mock"
    append_event(
        session, workspace_id=ctx.workspace_id, provider_call_attempt_id=attempt.id,
        event_type="certainty_resolved",
        payload={"resolution": "confirmed_dispatched" if charged else "confirmed_not_dispatched", "source": source},
        executor=ctx.executor,
    )
    cost = ReportedCost(
        provider_amount=Decimal("0") if mock else amount,
        provider_currency="USD",
        price_rule_version=pricing_version,
        usage={"operation": operation, "credits_charged": credits, "credits_remaining": None,
               "cached": False, "reconciled": source},
        evidence={"reconciliation": source, "charged": charged},
        simulated_amount=amount if mock else None,
    )
    ExternalCallCoordinator._append_usage_event(session, ctx, attempt=attempt, reported_cost=cost)
    if attempt.spend_reservation_id is not None:
        if charged:
            ExternalCallCoordinator._settle_reservation(session, ctx, attempt=attempt, now=now, reported_cost=cost)
        else:
            ExternalCallCoordinator._release_reservation(session, ctx, attempt=attempt, now=now)
    if charged:  # the provider charged the page, so it received the call
        record_provider_call(session, ctx, attempt, occurred_at=now)
    append_event(
        session, workspace_id=ctx.workspace_id, provider_call_attempt_id=attempt.id,
        event_type="reconciled", payload={"outcome": "failed", "charged": charged, "source": source},
        executor=ctx.executor,
    )
    attempt.dispatch_state = "failed"
    attempt.error_code = attempt.error_code or "outcome_unknown"
    attempt.completed_at = now
    attempt.updated_at = now
    session.flush()
    return True
