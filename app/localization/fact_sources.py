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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Mapping, Protocol

from ..library.fact_types import FACT_VALUE_FIELDS, fact_value_problems


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


ROW_IN_FORCE = "in_force"
ROW_EXPIRED = "expired"
ROW_NOT_YET_VALID = "not_yet_valid"

# How a manual row was matched to the request's Offer (#54).
#: The row names the Offer entry of the request's OfferVersion.
OFFER_MATCH_ROW = "row"
#: The row names no Offer (a set published before #54), and the request's
#: Offer is the only Offer of the Workspace in the row's market.
OFFER_MATCH_ONLY_OFFER = "only_offer_in_market"
#: The row names no Offer, and other Offers of the Workspace cover its
#: market too: it cannot be told whose fact it is.
OFFER_MATCH_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManualFactRow:
    """One row of a ManualFactSetVersion for one market, with its own window (#60).

    `index` is the row's position in the version's `facts`. A row is in
    force while `valid_from <= at < valid_to`. `offer_entry_id` is the Offer
    entry the row names (None on a set published before #54), and
    `offer_match` how it was matched to the request's Offer
    (`OFFER_MATCH_*`; None when built without one).
    """

    index: int
    fact_id: str | None
    fact_type: str
    valid_from: datetime
    valid_to: datetime
    raw: Mapping[str, object]
    offer_entry_id: str | None = None
    offer_match: str | None = None

    def status_at(self, at: datetime) -> str:
        if at < self.valid_from:
            return ROW_NOT_YET_VALID
        if at >= self.valid_to:
            return ROW_EXPIRED
        return ROW_IN_FORCE


@dataclass(frozen=True)
class ManualFactSet:
    """The rows of one ManualFactSetVersion that a market can use.

    Truth builds it with `rows` (#60): every row of the market, each with its
    own window, judged one by one at resolution time. The other form,
    `facts` (one row per fact type) with one `valid_from`/`valid_to` for all
    of them, is a set whose rows share a window: it is in force as a whole or
    not at all (`manual_fact_set_expired`).

    `rows` are the rows of the request's Offer (#54). `unattributed_rows`
    are rows of the market that name no Offer and cannot be attributed to
    it: a fact type that has only those fails `manual_fact_offer_ambiguous`
    instead of taking another Offer's fact.
    """

    version_id: str
    facts: dict = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    rows: tuple[ManualFactRow, ...] = ()
    unattributed_rows: tuple[ManualFactRow, ...] = ()


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
    # Every source row of this fact considered for the market, with its
    # window and whether it was in force and used (#60). Not part of
    # `evidence_hash`, which covers the row actually used.
    source_rows: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class FactSourceFailure:
    fact_type: str
    scope_key: str
    code: str
    message: str
    is_transient: bool
    source_rows: tuple[Mapping[str, object], ...] = ()


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


# One FactValue class per Truth fact family. Each class's fields are exactly
# `FACT_VALUE_FIELDS[family]`, in order (guarded by tests/test_fact_families.py).
_FACT_VALUE_CLASSES: dict[str, type[FactValue]] = {
    "price": MoneyFactValue,
    "availability": AvailabilityFactValue,
    "discount": DiscountFactValue,
    "shipping": ShippingFactValue,
    "delivery": DeliveryFactValue,
    "product_claim": ProductClaimFactValue,
}


def _build_fact_value(
    fact_type: str, raw: Mapping[str, object], *, scope_key: str
) -> FactValue | FactSourceFailure:
    """Parse one source row into its family's FactValue.

    A row this parser cannot turn into a fact -- a name outside the Truth
    fact families (e.g. a ManualFactSet published before #26 with `tax`),
    or a value that breaks its family's structure -- fails that one fact
    with a non-transient failure, which the policy's `on_failure` then
    decides. It never raises: one bad row must not fail the whole request.
    """
    value_class = _FACT_VALUE_CLASSES.get(fact_type)
    if value_class is None:
        return FactSourceFailure(
            fact_type=fact_type,
            scope_key=scope_key,
            code="fact_type_unsupported",
            message=(
                f"fact_type {fact_type!r} is not a Truth fact family "
                f"(valid: {sorted(_FACT_VALUE_CLASSES)})"
            ),
            is_transient=False,
        )
    problems = fact_value_problems(fact_type, raw)
    if problems:
        return FactSourceFailure(
            fact_type=fact_type,
            scope_key=scope_key,
            code="fact_value_invalid",
            message=f"{fact_type} value is invalid: " + ", ".join(
                f"{problem.field} ({problem.code})" for problem in problems
            ),
            is_transient=False,
        )
    return value_class(**{
        field: Decimal(raw[field]) if kind == "decimal" else raw[field]
        for field, kind in FACT_VALUE_FIELDS[fact_type].items()
    })


class ManualFactSourceAdapter:
    """Persistence-free adapter that returns raw facts from a ManualFactSet.

    A manual row is valid only while `valid_from <= resolution_time <
    valid_to`; the adapter enforces it at resolution time (publication only
    warns about rows that already ended, #60). Rows are judged one by one: a
    fact type is served from a row of the market in force now, so a row out
    of force leaves the others usable, and a fact type with no row in force
    fails alone (`manual_fact_expired` / `manual_fact_not_yet_valid`) for the
    policy's `on_failure` to decide.
    """

    adapter_kind = "manual"

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult:
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
        if manual.rows or manual.unattributed_rows:
            return self._resolve_rows(request, manual, now)
        return self._resolve_shared_window(request, manual, now)

    def _resolve_rows(
        self, request: FactResolutionRequest, manual: ManualFactSet, now: datetime
    ) -> FactSourceResult:
        facts: list[RawSourceFact] = []
        failures: list[FactSourceFailure] = []

        for fact_type in request.requested_fact_types:
            scope_key = _scope_key(request, fact_type)
            rows = [row for row in manual.rows if row.fact_type == fact_type]
            statuses = [row.status_at(now) for row in rows]
            in_force = [row for row, status in zip(rows, statuses) if status == ROW_IN_FORCE]
            # Two rows in force for one fact: the later row of the version
            # wins, as the last row of a fact type always did.
            used = in_force[-1] if in_force else None
            source_rows = tuple(
                _row_evidence(row, status, used=row is used) for row, status in zip(rows, statuses)
            )

            if not rows:
                unattributed = [row for row in manual.unattributed_rows if row.fact_type == fact_type]
                if unattributed:
                    # Never another Offer's fact (#54): the set predates rows
                    # naming their Offer, and the market has several Offers.
                    failures.append(
                        FactSourceFailure(
                            fact_type=fact_type,
                            scope_key=scope_key,
                            code="manual_fact_offer_ambiguous",
                            message=(
                                f"The {fact_type} rows of ManualFactSet {manual.version_id} name no Offer, "
                                f"and other Offers sell in {request.market.country}; import facts whose "
                                "rows name their offer_entry_id"
                            ),
                            is_transient=False,
                            source_rows=tuple(
                                _row_evidence(row, row.status_at(now), used=False) for row in unattributed
                            ),
                        )
                    )
                    continue
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

            if used is None:
                if ROW_NOT_YET_VALID in statuses:
                    failures.append(
                        FactSourceFailure(
                            fact_type=fact_type,
                            scope_key=scope_key,
                            code="manual_fact_not_yet_valid",
                            message=(
                                f"No {fact_type} row of ManualFactSet {manual.version_id} is valid "
                                f"at {now.isoformat()}; the next one starts later"
                            ),
                            is_transient=False,
                            source_rows=source_rows,
                        )
                    )
                else:
                    failures.append(
                        FactSourceFailure(
                            fact_type=fact_type,
                            scope_key=scope_key,
                            code="manual_fact_expired",
                            message=(
                                f"Every {fact_type} row of ManualFactSet {manual.version_id} "
                                f"ended before {now.isoformat()}"
                            ),
                            is_transient=False,
                            source_rows=source_rows,
                        )
                    )
                continue

            outcome = self._fact(
                request, manual, fact_type, used.raw, scope_key=scope_key, now=now,
                source_valid_until=used.valid_to,
            )
            if isinstance(outcome, FactSourceFailure):
                failures.append(replace(outcome, source_rows=source_rows))
            else:
                facts.append(replace(outcome, source_rows=source_rows))

        return FactSourceResult(facts=tuple(facts), failures=tuple(failures))

    def _resolve_shared_window(
        self, request: FactResolutionRequest, manual: ManualFactSet, now: datetime
    ) -> FactSourceResult:
        facts: list[RawSourceFact] = []
        failures: list[FactSourceFailure] = []
        is_valid_interval = (
            manual.valid_from is not None
            and manual.valid_to is not None
            and manual.valid_from <= now < manual.valid_to
        )

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
                            f"(valid_from={manual.valid_from}, valid_to={manual.valid_to})"
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

            outcome = self._fact(
                request, manual, fact_type, raw, scope_key=scope_key, now=now,
                source_valid_until=manual.valid_to,
            )
            if isinstance(outcome, FactSourceFailure):
                failures.append(outcome)
            else:
                facts.append(outcome)

        return FactSourceResult(facts=tuple(facts), failures=tuple(failures))

    def _fact(
        self,
        request: FactResolutionRequest,
        manual: ManualFactSet,
        fact_type: str,
        raw: Mapping[str, object],
        *,
        scope_key: str,
        now: datetime,
        source_valid_until: datetime | None,
    ) -> RawSourceFact | FactSourceFailure:
        value = _build_fact_value(fact_type, raw, scope_key=scope_key)
        if isinstance(value, FactSourceFailure):
            return value
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
        return RawSourceFact(
            fact_type=fact_type,
            scope_key=scope_key,
            value=value,
            evidence_hash=evidence_hash,
            external_object_ref=f"manual_fact_set:{manual.version_id}",
            requested_context=requested_context,
            observed_at=now,
            source_valid_until=source_valid_until,
            mock_scenario=None,
            mock_version=None,
        )


def _row_evidence(row: ManualFactRow, status: str, *, used: bool) -> dict[str, object]:
    """How one manual row stood at resolution time, as the Truth attempt records it (#60).

    With the Offer the row names and how it matched the request's Offer
    when Truth recorded it (#54).
    """
    evidence: dict[str, object] = {
        "fact_id": row.fact_id,
        "row": row.index,
        "valid_from": row.valid_from.isoformat(),
        "valid_to": row.valid_to.isoformat(),
        "status": status,
        "used": used,
    }
    if row.offer_match is not None:
        evidence["offer_entry_id"] = row.offer_entry_id
        evidence["offer_match"] = row.offer_match
    return evidence


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

            value = _build_fact_value(fact_type, raw, scope_key=scope_key)
            if isinstance(value, FactSourceFailure):
                failures.append(value)
                continue
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
