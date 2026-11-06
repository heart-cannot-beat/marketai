"""CostQuoteEngine: stateless price lookup and quote generation.

Spec section 3.5. Each planned external call gets a separate FrozenQuote.
The price registry is seeded from ``config.COST_PER_IMAGE`` as the initial
version; callers may supply any registry conforming to the
``dict[str, dict[str, Decimal]]`` shape (provider -> model -> unit price).

Rules:
  - Mock real amounts are zero; simulated amounts go to ``simulated_*``.
  - Non-USD Workspace without FX Adapter raises ``KeyError`` for now
    (future: ``UnsupportedCurrencyPairError``).
  - Exchange rate for same-currency is 1.0 (identity).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from app.entitlements.types import (
    BillableCallSpec,
    ExecutionMode,
    FrozenQuote,
    QuoteBundle,
)


class CostQuoteEngine:
    """Stateless price lookup that produces immutable cost quotes."""

    def __init__(
        self,
        price_registry: dict[str, dict[str, Decimal]],
        price_rule_version: str,
    ) -> None:
        self._registry = price_registry
        self._price_rule_version = price_rule_version

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def quote(
        self,
        *,
        call_semantic_key_hash: str,
        provider: str,
        model: str,
        execution_mode: str,
        quantity: int,
        budget_currency: str,
    ) -> FrozenQuote:
        """Generate a single immutable quote for one external call."""
        unit_price = self._lookup(provider, model)
        mode = ExecutionMode(execution_mode)

        if mode == ExecutionMode.MOCK:
            provider_amount = Decimal("0")
            budget_amount = Decimal("0")
            simulated_amount = unit_price * quantity
            simulated_currency = budget_currency
        else:
            provider_amount = unit_price * quantity
            budget_amount = provider_amount  # same currency -> 1:1
            simulated_amount = None
            simulated_currency = None

        pricing_input = json.dumps(
            {"provider": provider, "model": model, "quantity": quantity,
             "unit_price": str(unit_price), "mode": execution_mode},
            sort_keys=True,
        )
        pricing_input_hash = hashlib.sha256(pricing_input.encode()).hexdigest()[:16]

        return FrozenQuote(
            call_semantic_key_hash=call_semantic_key_hash,
            provider=provider,
            model=model,
            execution_mode=mode,
            quantity=quantity,
            provider_amount=provider_amount,
            provider_currency=budget_currency,
            budget_amount=budget_amount,
            budget_currency=budget_currency,
            exchange_rate=Decimal("1"),
            exchange_rate_source="identity",
            exchange_rate_observed_at=None,
            fx_buffer_bps=0,
            price_rule_version=self._price_rule_version,
            pricing_input_hash=pricing_input_hash,
            simulated_amount=simulated_amount,
            simulated_currency=simulated_currency,
        )

    def bundle(
        self,
        call_specs: Sequence[BillableCallSpec],
        *,
        execution_mode: str,
        budget_currency: str,
    ) -> QuoteBundle:
        """Quote each spec and bundle the results."""
        quotes: list[FrozenQuote] = []
        for spec in call_specs:
            q = self.quote(
                call_semantic_key_hash=spec.call_semantic_key_hash,
                provider=spec.provider,
                model=spec.model,
                execution_mode=execution_mode,
                quantity=spec.quantity,
                budget_currency=budget_currency,
            )
            quotes.append(q)

        total = sum((q.budget_amount for q in quotes), Decimal("0"))
        return QuoteBundle(
            call_quotes=tuple(quotes),
            total_budget_amount=total,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _lookup(self, provider: str, model: str) -> Decimal:
        """Look up the unit price for a provider/model pair.

        Raises ``KeyError`` when the pair is not in the registry.
        """
        provider_prices = self._registry[provider]
        return provider_prices[model]
