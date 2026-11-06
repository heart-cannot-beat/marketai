"""FactSourceAdapter protocol, value objects, and registered adapters.

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md §2B, §3D

Issue #24 defines one shared FactSourceAdapter interface and ships exactly two
registered adapters: ManualFactSourceAdapter and ControlledMockFactSourceAdapter.
Do not add Shopify, Product Claim, or Shipping Policy stub classes here.

The request is persistence-free and secret-free: adapters never query ORM
state, format money, or derive Claims/discounts. The localization module
(app/localization/truth.py, a later task) loads and validates ORM records,
constructs these value objects, selects the adapter, and persists the result.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Mapping, Protocol


@dataclass(frozen=True)
class OfferContext:
    offer_version_id: str
    product_version_id: str


@dataclass(frozen=True)
class MarketContext:
    market: str
    locale: str
    country: str
    currency: str


@dataclass(frozen=True)
class SourceMappingContext:
    mapping_version_id: str


@dataclass(frozen=True)
class DataSourceConfig:
    data_source_version_id: str


@dataclass(frozen=True)
class ManualFactSet:
    version_id: str
    facts: dict
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True)
class MoneyFactValue:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class AvailabilityFactValue:
    available: bool


@dataclass(frozen=True)
class DiscountFactValue:
    discount_type: str
    amount: Decimal
    currency: str
    conditions: str


@dataclass(frozen=True)
class ShippingFactValue:
    method: str
    cost: Decimal
    currency: str
    delivery_days_min: int
    delivery_days_max: int


@dataclass(frozen=True)
class DeliveryFactValue:
    delivery_days_min: int
    delivery_days_max: int
    region: str


@dataclass(frozen=True)
class ProductClaimFactValue:
    claim_type: str
    claim_text: str
    evidence_ref: str


FactValue = (
    MoneyFactValue
    | AvailabilityFactValue
    | DiscountFactValue
    | ShippingFactValue
    | DeliveryFactValue
    | ProductClaimFactValue
)


@dataclass(frozen=True)
class FactResolutionRequest:
    requested_fact_types: tuple[str, ...]
    offer: OfferContext
    market: MarketContext
    source_mapping: SourceMappingContext
    data_source: DataSourceConfig
    manual_fact_set: ManualFactSet | None
    truth_data_mode: Literal["real", "mock"]
    mock_scenario: str | None


@dataclass(frozen=True)
class RawSourceFact:
    fact_type: str
    scope_key: str
    value: FactValue
    evidence_hash: str
    external_object_ref: str
    requested_context: Mapping[str, object]
    observed_at: datetime
    source_valid_until: datetime | None
    mock_scenario: str | None
    mock_version: str | None


@dataclass(frozen=True)
class FactSourceFailure:
    fact_type: str
    scope_key: str
    code: str
    message: str
    is_transient: bool


@dataclass(frozen=True)
class FactSourceResult:
    facts: tuple[RawSourceFact, ...]
    failures: tuple[FactSourceFailure, ...]


class FactSourceAdapter(Protocol):
    adapter_kind: str

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult: ...


def _scope_key(request: FactResolutionRequest, fact_type: str) -> str:
    """Build a concrete scope key identifying exactly which offer/market/fact
    this Fact applies to. Never a generic placeholder."""
    return (
        f"{request.offer.offer_version_id}:{request.market.market}:{fact_type}"
    )


def _requested_context(request: FactResolutionRequest) -> Mapping[str, object]:
    return {
        "offer_version_id": request.offer.offer_version_id,
        "product_version_id": request.offer.product_version_id,
        "market": request.market.market,
        "locale": request.market.locale,
        "country": request.market.country,
        "currency": request.market.currency,
        "mapping_version_id": request.source_mapping.mapping_version_id,
        "data_source_version_id": request.data_source.data_source_version_id,
    }


def _canonical_hash(payload: Mapping[str, object]) -> str:
    """SHA-256 over a canonical (sorted-key, Decimal-safe) JSON encoding."""

    def _default(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    encoded = json.dumps(payload, sort_keys=True, default=_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_fact_value(fact_type: str, raw: Mapping[str, object]) -> FactValue:
    if fact_type == "price":
        return MoneyFactValue(amount=Decimal(str(raw["amount"])), currency=str(raw["currency"]))
    if fact_type == "availability":
        return AvailabilityFactValue(available=bool(raw["available"]))
    if fact_type == "discount":
        return DiscountFactValue(
            discount_type=str(raw["discount_type"]),
            amount=Decimal(str(raw["amount"])),
            currency=str(raw["currency"]),
            conditions=str(raw["conditions"]),
        )
    if fact_type == "shipping":
        return ShippingFactValue(
            method=str(raw["method"]),
            cost=Decimal(str(raw["cost"])),
            currency=str(raw["currency"]),
            delivery_days_min=int(raw["delivery_days_min"]),
            delivery_days_max=int(raw["delivery_days_max"]),
        )
    if fact_type == "delivery":
        return DeliveryFactValue(
            delivery_days_min=int(raw["delivery_days_min"]),
            delivery_days_max=int(raw["delivery_days_max"]),
            region=str(raw["region"]),
        )
    if fact_type == "product_claim":
        return ProductClaimFactValue(
            claim_type=str(raw["claim_type"]),
            claim_text=str(raw["claim_text"]),
            evidence_ref=str(raw["evidence_ref"]),
        )
    raise ValueError(f"Unknown fact_type: {fact_type}")


class ManualFactSourceAdapter:
    """Persistence-free adapter that returns raw facts from a ManualFactSet.

    Manual facts are valid only when
    `valid_from <= resolution_time < valid_to`; the adapter enforces the
    interval at resolution time (publication enforces it separately,
    outside this adapter).
    """

    adapter_kind = "manual"

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult:
        facts: list[RawSourceFact] = []
        failures: list[FactSourceFailure] = []

        if request.manual_fact_set is None:
            return FactSourceResult(
                facts=(),
                failures=tuple(
                    FactSourceFailure(
                        fact_type=ft,
                        scope_key=_scope_key(request, ft),
                        code="no_manual_fact_set",
                        message="Manual fact set required",
                        is_transient=False,
                    )
                    for ft in request.requested_fact_types
                ),
            )

        manual = request.manual_fact_set
        now = datetime.now(timezone.utc)
        is_valid_interval = manual.valid_from <= now < manual.valid_to

        for fact_type in request.requested_fact_types:
            scope_key = _scope_key(request, fact_type)

            if not is_valid_interval:
                failures.append(
                    FactSourceFailure(
                        fact_type=fact_type,
                        scope_key=scope_key,
                        code="manual_fact_set_expired",
                        message=(
                            f"ManualFactSet {manual.version_id} is not valid at {now.isoformat()} "
                            f"(valid_from={manual.valid_from.isoformat()}, valid_to={manual.valid_to.isoformat()})"
                        ),
                        is_transient=False,
                    )
                )
                continue

            raw = manual.facts.get(fact_type)
            if raw is None:
                failures.append(
                    FactSourceFailure(
                        fact_type=fact_type,
                        scope_key=scope_key,
                        code="fact_type_not_in_manual_set",
                        message=f"Manual fact set {manual.version_id} has no entry for {fact_type}",
                        is_transient=False,
                    )
                )
                continue

            value = _build_fact_value(fact_type, raw)
            requested_context = _requested_context(request)
            evidence_hash = _canonical_hash(
                {
                    "adapter_kind": self.adapter_kind,
                    "manual_fact_set_version_id": manual.version_id,
                    "fact_type": fact_type,
                    "scope_key": scope_key,
                    "value": raw,
                    "requested_context": requested_context,
                }
            )
            facts.append(
                RawSourceFact(
                    fact_type=fact_type,
                    scope_key=scope_key,
                    value=value,
                    evidence_hash=evidence_hash,
                    external_object_ref=f"manual_fact_set:{manual.version_id}",
                    requested_context=requested_context,
                    observed_at=now,
                    source_valid_until=manual.valid_to,
                    mock_scenario=None,
                    mock_version=None,
                )
            )

        return FactSourceResult(facts=tuple(facts), failures=tuple(failures))


class ControlledMockFactSourceAdapter:
    """Deterministic Controlled Mock adapter.

    Output is deterministic over adapter version, scenario, Offer context,
    market context, mapping, and requested fact set: the same input always
    produces the same evidence hash, while a different scenario or a
    different adapter version produces different output.
    """

    adapter_kind = "controlled_mock"
    _VERSION = "mock-v1"

    _SCENARIOS: dict[str, dict[str, dict[str, object]]] = {
        "complete_with_discount": {
            "price": {"amount": "19.99", "currency": "EUR"},
            "availability": {"available": True},
            "discount": {
                "discount_type": "fixed",
                "amount": "2.00",
                "currency": "EUR",
                "conditions": "Controlled Mock E2E fixture",
            },
        },
        "standard_9_market": {
            "price": {"amount": "19.99", "currency": "EUR"},
            "availability": {"available": True},
        },
        "unavailable": {
            "price": {"amount": "19.99", "currency": "EUR"},
            "availability": {"available": False},
        },
    }
    _DEFAULT_SCENARIO = "standard_9_market"

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult:
        scenario = request.mock_scenario or self._DEFAULT_SCENARIO
        scenario_facts = self._SCENARIOS.get(scenario, self._SCENARIOS[self._DEFAULT_SCENARIO])

        facts: list[RawSourceFact] = []
        failures: list[FactSourceFailure] = []
        observed_at = datetime.now(timezone.utc)

        for fact_type in request.requested_fact_types:
            scope_key = _scope_key(request, fact_type)
            raw = scenario_facts.get(fact_type)
            if raw is None:
                failures.append(
                    FactSourceFailure(
                        fact_type=fact_type,
                        scope_key=scope_key,
                        code="fact_type_not_in_mock_scenario",
                        message=f"Mock scenario {scenario} has no entry for {fact_type}",
                        is_transient=False,
                    )
                )
                continue

            value = _build_fact_value(fact_type, raw)
            requested_context = _requested_context(request)
            evidence_hash = _canonical_hash(
                {
                    "adapter_kind": self.adapter_kind,
                    "adapter_version": self._VERSION,
                    "mock_scenario": scenario,
                    "fact_type": fact_type,
                    "scope_key": scope_key,
                    "value": raw,
                    "requested_context": requested_context,
                }
            )
            facts.append(
                RawSourceFact(
                    fact_type=fact_type,
                    scope_key=scope_key,
                    value=value,
                    evidence_hash=evidence_hash,
                    external_object_ref=f"controlled_mock:{scenario}:{self._VERSION}",
                    requested_context=requested_context,
                    observed_at=observed_at,
                    source_valid_until=None,
                    mock_scenario=scenario,
                    mock_version=self._VERSION,
                )
            )

        return FactSourceResult(facts=tuple(facts), failures=tuple(failures))
