"""The market-data price table and the quote every real resolve or scan starts from.

``MARKETAI_MARKET_DATA_MAX_COSTS_JSON`` maps provider → operation →
``{"credits": <int>, "credit_usd": "<decimal>"}`` (spec §6). A provider's
table is usable only when it prices all five operations: a deployment that
priced ``page`` but not ``probe`` would let a scan through while refusing the
resolve that has to come first, so a partial table refuses every quote. A
change of prices ships with a new ``MARKETAI_PRICE_REGISTRY_VERSION``, which
each quote carries for the reservation and the evidence.

Controlled Mock never needs this table: its payable amount is zero, so a
missing table refuses real work only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app import config

#: The priced operations (spec §6): ``/search/companies``, a resolve's
#: ``/company/ads`` probe, a scan's ``/company/ads`` page, a ``/search/ads``
#: page, and the ``/ad`` media refresh.
MARKET_DATA_OPERATIONS: tuple[str, ...] = (
    "page_search",
    "probe",
    "page",
    "keyword_page",
    "ad_refresh",
)


class MarketDataPricingError(Exception):
    """The price table cannot quote real market-data work.

    ``code`` and ``params`` are what the interface renders
    (``error-<code>``); the message is an English diagnostic for logs.
    """

    def __init__(self, code: str, message: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.params = params or {}


@dataclass(frozen=True)
class MarketDataPrice:
    """The maximum one call of ``operation`` costs: credits × USD per credit."""

    operation: str
    credits: int
    credit_usd: Decimal

    @property
    def unit_usd(self) -> Decimal:
        return self.credit_usd * self.credits


@dataclass(frozen=True)
class MarketDataQuoteLine:
    operation: str
    calls: int
    credits: int
    usd: Decimal


@dataclass(frozen=True)
class MarketDataQuote:
    provider: str
    pricing_version: str
    lines: tuple[MarketDataQuoteLine, ...]
    credits: int
    usd: Decimal
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "pricing_version": self.pricing_version,
            "currency": self.currency,
            "credits": self.credits,
            "usd": str(self.usd),
            "lines": [
                {"operation": line.operation, "calls": line.calls,
                 "credits": line.credits, "usd": str(line.usd)}
                for line in self.lines
            ],
        }


def _invalid(provider: str, detail: str) -> MarketDataPricingError:
    return MarketDataPricingError(
        "market_data_prices_invalid",
        f"MARKETAI_MARKET_DATA_MAX_COSTS_JSON for {provider!r} is malformed: {detail}",
        {"provider": provider},
    )


def _price(provider: str, operation: str, raw: Any) -> MarketDataPrice:
    if not isinstance(raw, dict):
        raise _invalid(provider, f"{operation} is not an object")
    credits = raw.get("credits")
    # ``bool`` is an ``int``; a ``true`` credit count is a typo, not a price.
    if isinstance(credits, bool) or not isinstance(credits, int) or credits < 1:
        raise _invalid(provider, f"{operation}.credits must be a positive integer")
    credit_usd = raw.get("credit_usd")
    if not isinstance(credit_usd, str):
        raise _invalid(provider, f"{operation}.credit_usd must be a decimal string")
    try:
        unit = Decimal(credit_usd)
    except InvalidOperation:
        raise _invalid(provider, f"{operation}.credit_usd is not a decimal") from None
    if not unit.is_finite() or unit <= 0:
        raise _invalid(provider, f"{operation}.credit_usd must be positive")
    return MarketDataPrice(operation=operation, credits=credits, credit_usd=unit)


def price_table(provider: str) -> dict[str, MarketDataPrice]:
    """All five prices for ``provider``, or the coded reason there are none."""
    try:
        registry = json.loads(config.MARKET_DATA_MAX_COSTS_JSON or "{}")
    except json.JSONDecodeError:
        raise _invalid(provider, "not valid JSON") from None
    if not isinstance(registry, dict):
        raise _invalid(provider, "the registry is not an object")
    table = registry.get(provider)
    if table is None or table == {}:
        raise MarketDataPricingError(
            "market_data_prices_unconfigured",
            f"no market-data prices are configured for {provider!r}",
            {"provider": provider},
        )
    if not isinstance(table, dict):
        raise _invalid(provider, "the provider entry is not an object")
    missing = [op for op in MARKET_DATA_OPERATIONS if op not in table]
    if missing:
        raise MarketDataPricingError(
            "market_data_price_operation_missing",
            f"market-data prices for {provider!r} lack {', '.join(missing)}",
            {"provider": provider, "operations": ", ".join(missing)},
        )
    return {op: _price(provider, op, table[op]) for op in MARKET_DATA_OPERATIONS}


def quote_market_data(provider: str, calls: Mapping[str, int]) -> MarketDataQuote:
    """The maximum ``calls`` (operation → number of calls) can cost on ``provider``.

    Raises :class:`MarketDataPricingError` when the table is missing,
    incomplete or malformed -- whichever operations this quote happens to
    use. An unknown operation or a negative count is a caller bug
    (``ValueError``), not a deployment problem.
    """
    unknown = sorted(set(calls) - set(MARKET_DATA_OPERATIONS))
    if unknown:
        raise ValueError(f"unknown market-data operations: {', '.join(unknown)}")
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in calls.values()):
        raise ValueError("market-data call counts must be non-negative integers")
    prices = price_table(provider)
    lines = tuple(
        MarketDataQuoteLine(
            operation=op,
            calls=calls[op],
            credits=prices[op].credits * calls[op],
            usd=prices[op].unit_usd * calls[op],
        )
        for op in MARKET_DATA_OPERATIONS
        if calls.get(op)
    )
    return MarketDataQuote(
        provider=provider,
        pricing_version=config.MARKETAI_PRICE_REGISTRY_VERSION,
        lines=lines,
        credits=sum(line.credits for line in lines),
        usd=sum((line.usd for line in lines), Decimal("0")),
    )
