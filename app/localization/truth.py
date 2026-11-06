"""Truth resolution module for Localization (#24).

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md §2C

`resolve_or_reuse_truth_package()` owns the complete resolution algorithm
described in §2C: it drives the registered :class:`FactSourceAdapter` for
each configured Market/DataSource, freezes raw Facts and a small set of
deterministic derived Facts, derives per-market readiness and package
status, computes a canonical hash over every frozen input, and reuses a
matching still-fresh package instead of writing a duplicate one.

`assess_truth_freshness()` is the synchronous freshness predicate: it checks
`valid_until`, the effective lifecycle of every workspace LibraryVersion
dependency, and the effective state of every platform MarketPolicyVersion
dependency, returning reasoned evidence rather than only a Boolean.

This module treats `product_version_id`, `offer_version_id`, and
`truth_resolution_policy_version_id` as already-resolved effective
LibraryVersion ids supplied by the caller (batch.py, a later task) --
it validates that they exist and are effective, but it does not parse
their `content_json`. Per-market MarketPolicyVersion rows *are* read
directly (workspace-independent, platform-scoped), because
`MarketPolicy.max_fact_staleness_seconds` and lifecycle state are part of
the frozen resolution algorithm (§2C step 7) and the concrete columns
already exist (Task 1).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Mapping

from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    FactLibraryDependencyRecord,
    FactMarketPolicyDependencyRecord,
    LibraryVersionRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    TruthDerivedFactInputRecord,
    TruthDerivedFactRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
    TruthPackageRecord,
    TruthSourceAttemptRecord,
    new_id,
)
from ..library.contracts import (
    ADAPTER_KINDS,
    EXECUTION_MODES,
    ON_FAILURE_MODES,
    PRICE_FORBIDDEN_ON_FAILURE,
)
from ..platform.tenancy import TenantContext
from .evidence_tier import EvidenceTier, derive_evidence_tier
from .fact_sources import (
    AvailabilityFactValue,
    ControlledMockFactSourceAdapter,
    DataSourceConfig,
    FactResolutionRequest,
    FactSourceAdapter,
    FactSourceFailure,
    FactSourceResult,
    ManualFactSet,
    ManualFactSourceAdapter,
    MarketContext,
    OfferContext,
    RawSourceFact,
    SourceMappingContext,
)

# `FACT_TYPES` (contracts.py's ManualFactSet vocabulary: price/
# shipping_availability/delivery_time/tax/discount) is intentionally not
# imported here: `prepare_truth_resolution_command()` treats a policy rule's
# `fact_type` as an opaque string and passes it straight through to the
# adapter layer. `fact_sources._build_fact_value()` (Task 8) recognizes a
# different, overlapping vocabulary (price/availability/discount/shipping/
# delivery/product_claim). Where the two disagree (e.g. "shipping_
# availability" vs "availability"), resolution surfaces a clear
# `ValueError: Unknown fact_type: ...` from the adapter rather than this
# module silently translating between them -- see the Task 9 fix-round
# report for why reconciling the two vocabularies is out of this task's
# scope.

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TruthResolutionError(RuntimeError):
    """Raised when a TruthResolutionCommand cannot be honored."""


class MixedTruthDataModeError(TruthResolutionError):
    """A resolution mixed real and Mock DataSources.

    Spec constraint: mixed real+mock data sources must be rejected outright,
    never silently downgraded to Mock evidence.
    """


# ---------------------------------------------------------------------------
# Command value objects (caller-constructed; already-resolved inputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactPolicy:
    """The frozen `on_failure` rule for one requested fact type in a market."""

    fact_type: str
    on_failure: Literal["block", "degrade", "skip"] = "skip"


@dataclass(frozen=True)
class SourceStep:
    """One entry in a market's `source_priority` list."""

    data_source_version_id: str
    adapter: FactSourceAdapter
    adapter_kind: str
    adapter_version: str
    truth_data_mode: Literal["real", "mock"]
    market_source_mapping_version_id: str | None = None
    manual_fact_set: ManualFactSet | None = None
    manual_fact_set_version_id: str | None = None
    mock_scenario: str | None = None
    mock_version: str | None = None


@dataclass(frozen=True)
class MarketResolutionCommand:
    """One requested Market within a TruthResolutionCommand."""

    market: str
    locale: str
    country: str
    currency: str
    market_policy_version_id: str
    sources: tuple[SourceStep, ...]
    fact_policies: tuple[FactPolicy, ...]
    allow_fallback: bool = False


@dataclass(frozen=True)
class TruthResolutionCommand:
    """The full input to `resolve_or_reuse_truth_package`.

    `product_version_id`, `offer_version_id`, and
    `truth_resolution_policy_version_id` are effective LibraryVersion ids the
    caller already resolved; `max_staleness_seconds` is that
    TruthResolutionPolicyVersion's frozen staleness bound. `now`, when
    supplied, pins the resolution clock (tests); otherwise the wall clock is
    used.
    """

    product_version_id: str
    offer_version_id: str
    truth_resolution_policy_version_id: str
    truth_data_mode: Literal["real", "mock"]
    max_staleness_seconds: int
    resolution_rule_version: str
    markets: tuple[MarketResolutionCommand, ...]
    now: datetime | None = None


# ---------------------------------------------------------------------------
# Result value objects (prescribed by the brief, verbatim)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TruthResolutionResult:
    truth_package_id: str
    status: str  # 'full' | 'degraded' | 'failed'
    reused: bool
    market_readiness: dict[str, str]


@dataclass(frozen=True)
class FreshnessReport:
    is_current: bool
    reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# Internal working drafts (not persisted directly; mirror ORM rows 1:1)
# ---------------------------------------------------------------------------


_READY = "ready"
_DEGRADED = "degraded"
_FAILED = "failed"
_UNAVAILABLE = "unavailable"


@dataclass
class _AttemptDraft:
    id: str
    fact_type: str
    scope_key: str
    priority_ordinal: int
    data_source_version_id: str
    market_source_mapping_version_id: str | None
    adapter_kind: str
    adapter_version: str
    manual_fact_set_version_id: str | None
    requested_context: Mapping[str, object]
    mock_scenario: str | None
    mock_version: str | None
    outcome: Literal["accepted", "failed", "skipped"]
    failure_code: str | None
    skip_code: str | None
    evidence_hash: str | None
    started_at: datetime
    completed_at: datetime


@dataclass
class _AcceptedFactDraft:
    fact_type: str
    scope_key: str
    accepted_attempt_id: str
    raw_fact: RawSourceFact
    value_json: dict
    valid_until: datetime
    dependency_edges: tuple[tuple[str, str], ...]


@dataclass
class _MarketOutcome:
    market: str
    market_policy_version_id: str
    readiness: str
    readiness_detail: dict
    attempts: list[_AttemptDraft]
    accepted_facts: list[_AcceptedFactDraft]
    valid_until: datetime


def _aware(value: datetime) -> datetime:
    """SQLite round-trips `DateTime(timezone=True)` as naive; normalize to UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _fact_value_to_json(value: object) -> dict:
    if is_dataclass(value):
        raw = asdict(value)
    else:
        raw = dict(value)  # pragma: no cover - defensive, all FactValue variants are dataclasses
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in raw.items()}


def _scope_key(offer_version_id: str, market: str, fact_type: str) -> str:
    return f"{offer_version_id}:{market}:{fact_type}"


def _requested_context(
    command: TruthResolutionCommand, market_cmd: MarketResolutionCommand, step: SourceStep
) -> dict:
    return {
        "offer_version_id": command.offer_version_id,
        "product_version_id": command.product_version_id,
        "market": market_cmd.market,
        "locale": market_cmd.locale,
        "country": market_cmd.country,
        "currency": market_cmd.currency,
        "mapping_version_id": step.market_source_mapping_version_id,
        "data_source_version_id": step.data_source_version_id,
    }


def _canonical_json(payload: object) -> str:
    def _default(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    return json.dumps(payload, sort_keys=True, default=_default)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_uniform_truth_data_mode(command: TruthResolutionCommand) -> None:
    """Reject a resolution that mixes real and Mock DataSources.

    This must run before anything else: the spec forbids silently
    downgrading a mixed resolution to Mock evidence via the evidence-tier
    fold, so the mix has to be caught before `derive_evidence_tier` (which
    would otherwise fold a mix down to MOCK without complaint) ever runs.
    """
    modes: set[str] = {command.truth_data_mode}
    for market_cmd in command.markets:
        if not market_cmd.sources:
            raise TruthResolutionError(
                f"market {market_cmd.market!r} has no configured source_priority"
            )
        for step in market_cmd.sources:
            modes.add(step.truth_data_mode)

    if len(modes) > 1:
        raise MixedTruthDataModeError(
            "TruthResolutionCommand mixes truth_data_modes "
            f"{sorted(modes)!r}; a TruthPackage cannot mix real and Mock "
            "DataSources in one resolution"
        )

    provider_alias = "byok" if command.truth_data_mode == "real" else "mock"
    tier = derive_evidence_tier(
        source_modes=[provider_alias],
        experiment_modes=[],
        truth_data_mode=command.truth_data_mode,
        provider_mode=provider_alias,
    )
    expected = EvidenceTier.PRODUCTION if command.truth_data_mode == "real" else EvidenceTier.MOCK
    if tier != expected:  # pragma: no cover - defensive; the maps above are fixed and 1:1
        raise TruthResolutionError(
            f"internal evidence-tier mismatch for uniform mode {command.truth_data_mode!r}"
        )

    for market_cmd in command.markets:
        for fact_policy in market_cmd.fact_policies:
            if fact_policy.fact_type == "price" and fact_policy.on_failure == "degrade":
                raise TruthResolutionError(
                    "price fact_type cannot use on_failure='degrade' "
                    "(permits 'skip' only)"
                )


def _load_effective_version(
    session: Session, ctx: TenantContext, version_id: str, label: str
) -> LibraryVersionRecord:
    version = (
        session.query(LibraryVersionRecord)
        .filter(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == version_id,
        )
        .one_or_none()
    )
    if version is None:
        raise TruthResolutionError(f"{label} {version_id!r} does not exist in this Workspace")
    if version.state != "effective":
        raise TruthResolutionError(f"{label} {version_id!r} is not effective (state={version.state!r})")
    return version


def _load_effective_market_policy_version(
    session: Session, market_policy_version_id: str
) -> MarketPolicyVersionRecord:
    mpv = session.get(MarketPolicyVersionRecord, market_policy_version_id)
    if mpv is None:
        raise TruthResolutionError(f"MarketPolicyVersion {market_policy_version_id!r} does not exist")
    if mpv.version_state != "effective":
        raise TruthResolutionError(
            f"MarketPolicyVersion {market_policy_version_id!r} is not effective "
            f"(state={mpv.version_state!r})"
        )
    return mpv


# ---------------------------------------------------------------------------
# Per-market resolution
# ---------------------------------------------------------------------------


def _resolve_market(
    market_cmd: MarketResolutionCommand,
    command: TruthResolutionCommand,
    now: datetime,
    max_fact_staleness_seconds: int,
) -> _MarketOutcome:
    remaining: set[str] = {fp.fact_type for fp in market_cmd.fact_policies}
    on_failure_by_type = {fp.fact_type: fp.on_failure for fp in market_cmd.fact_policies}

    attempts: list[_AttemptDraft] = []
    accepted: dict[str, _AcceptedFactDraft] = {}
    sources = market_cmd.sources

    for ordinal, step in enumerate(sources, start=1):
        if not remaining:
            break
        # `allow_fallback=false`: the first configured source is final.
        if ordinal > 1 and not market_cmd.allow_fallback:
            break

        request = FactResolutionRequest(
            requested_fact_types=tuple(sorted(remaining)),
            offer=OfferContext(
                offer_version_id=command.offer_version_id,
                product_version_id=command.product_version_id,
            ),
            market=MarketContext(
                market=market_cmd.market,
                locale=market_cmd.locale,
                country=market_cmd.country,
                currency=market_cmd.currency,
            ),
            source_mapping=SourceMappingContext(
                mapping_version_id=step.market_source_mapping_version_id or ""
            ),
            data_source=DataSourceConfig(data_source_version_id=step.data_source_version_id),
            manual_fact_set=step.manual_fact_set,
            truth_data_mode=step.truth_data_mode,
            mock_scenario=step.mock_scenario,
        )

        started_at = now
        result = step.adapter.resolve(request)
        completed_at = now

        for raw_fact in result.facts:
            attempt_id = new_id("tsa")
            attempts.append(
                _AttemptDraft(
                    id=attempt_id,
                    fact_type=raw_fact.fact_type,
                    scope_key=raw_fact.scope_key,
                    priority_ordinal=ordinal,
                    data_source_version_id=step.data_source_version_id,
                    market_source_mapping_version_id=step.market_source_mapping_version_id,
                    adapter_kind=step.adapter_kind,
                    adapter_version=step.adapter_version,
                    manual_fact_set_version_id=step.manual_fact_set_version_id,
                    requested_context=raw_fact.requested_context,
                    mock_scenario=raw_fact.mock_scenario,
                    mock_version=raw_fact.mock_version,
                    outcome="accepted",
                    failure_code=None,
                    skip_code=None,
                    evidence_hash=raw_fact.evidence_hash,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )

            bounds = [raw_fact.observed_at + timedelta(seconds=command.max_staleness_seconds)]
            bounds.append(raw_fact.observed_at + timedelta(seconds=max_fact_staleness_seconds))
            if raw_fact.source_valid_until is not None:
                bounds.append(raw_fact.source_valid_until)
            valid_until = min(bounds)

            dependency_edges: list[tuple[str, str]] = [
                ("data_source_version", step.data_source_version_id)
            ]
            if step.market_source_mapping_version_id:
                dependency_edges.append(
                    ("market_source_mapping_version", step.market_source_mapping_version_id)
                )
            if step.manual_fact_set_version_id:
                dependency_edges.append(
                    ("manual_fact_set_version", step.manual_fact_set_version_id)
                )

            accepted[raw_fact.fact_type] = _AcceptedFactDraft(
                fact_type=raw_fact.fact_type,
                scope_key=raw_fact.scope_key,
                accepted_attempt_id=attempt_id,
                raw_fact=raw_fact,
                value_json=_fact_value_to_json(raw_fact.value),
                valid_until=valid_until,
                dependency_edges=tuple(dependency_edges),
            )
            remaining.discard(raw_fact.fact_type)

        for failure in result.failures:
            attempts.append(
                _AttemptDraft(
                    id=new_id("tsa"),
                    fact_type=failure.fact_type,
                    scope_key=failure.scope_key,
                    priority_ordinal=ordinal,
                    data_source_version_id=step.data_source_version_id,
                    market_source_mapping_version_id=step.market_source_mapping_version_id,
                    adapter_kind=step.adapter_kind,
                    adapter_version=step.adapter_version,
                    manual_fact_set_version_id=step.manual_fact_set_version_id,
                    requested_context=_requested_context(command, market_cmd, step),
                    mock_scenario=step.mock_scenario,
                    mock_version=step.mock_version,
                    outcome="failed",
                    failure_code=failure.code,
                    skip_code=None,
                    evidence_hash=None,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
            # `failure.fact_type` stays in `remaining`; the next source is
            # tried only when `allow_fallback=true` (spec §2C step 5). A real
            # failure never falls back to Mock -- enforced separately by
            # `_validate_uniform_truth_data_mode`, which rejects any mix of
            # modes before resolution starts.

    # Sources exhausted (or fallback disabled): record the frozen
    # `on_failure` rule of whatever is still unresolved; the same rules
    # decide the market's readiness below.
    skip_priority = len(sources) + 1
    last_step = sources[-1] if sources else None
    for fact_type in sorted(remaining):
        on_failure = on_failure_by_type[fact_type]
        attempts.append(
            _AttemptDraft(
                id=new_id("tsa"),
                fact_type=fact_type,
                scope_key=_scope_key(command.offer_version_id, market_cmd.market, fact_type),
                priority_ordinal=skip_priority,
                data_source_version_id=last_step.data_source_version_id,
                market_source_mapping_version_id=(
                    last_step.market_source_mapping_version_id if last_step else None
                ),
                adapter_kind="policy",
                adapter_version=command.resolution_rule_version,
                manual_fact_set_version_id=None,
                requested_context={"market": market_cmd.market, "fact_type": fact_type},
                mock_scenario=(last_step.mock_scenario if command.truth_data_mode == "mock" else None),
                mock_version=(last_step.mock_version if command.truth_data_mode == "mock" else None),
                outcome="skipped",
                failure_code=None,
                skip_code=f"on_failure_{on_failure}",
                evidence_hash=None,
                started_at=now,
                completed_at=now,
            )
        )

    readiness, readiness_detail = _derive_market_readiness(
        accepted, remaining, on_failure_by_type
    )

    if accepted:
        market_valid_until = min(f.valid_until for f in accepted.values())
    else:
        market_valid_until = now + timedelta(seconds=command.max_staleness_seconds)

    return _MarketOutcome(
        market=market_cmd.market,
        market_policy_version_id=market_cmd.market_policy_version_id,
        readiness=readiness,
        readiness_detail=readiness_detail,
        attempts=attempts,
        accepted_facts=list(accepted.values()),
        valid_until=market_valid_until,
    )


def _derive_market_readiness(
    accepted: dict[str, _AcceptedFactDraft],
    remaining: set[str],
    on_failure_by_type: Mapping[str, str],
) -> tuple[str, dict]:
    """`truth-rule-v2` (contract §7.3): an available=false fact wins, then
    any unresolved `block` fact, then price, then the permitted rest."""
    availability = accepted.get("availability")
    if availability is not None:
        value = availability.raw_fact.value
        if isinstance(value, AvailabilityFactValue) and value.available is False:
            return _UNAVAILABLE, {"reason": "skipped_unavailable"}

    if not remaining:
        return _READY, {"resolved_fact_types": sorted(accepted.keys())}

    blocked = sorted(ft for ft in remaining if on_failure_by_type[ft] == "block")
    if blocked:
        # `block` stops the market whatever the fact is, price included:
        # this reason never takes batch.py's price-omission exception.
        return _FAILED, {"reason": "fact_missing_blocked", "blocked": blocked, "missing": sorted(remaining)}

    if "price" in remaining:
        # Price forbids `on_failure=degrade`; a `skip`ped price still makes
        # the market non-executable at the truth layer (whether it can
        # become an executable Item once source-price Claims are removed
        # is decided later, in claims.py/batch.py -- out of this module's
        # scope per the brief).
        return _FAILED, {"reason": "price_unresolved", "missing": sorted(remaining)}

    return _DEGRADED, {"reason": "fact_missing_permitted", "missing": sorted(remaining)}


def _derive_package_status(outcomes: list[_MarketOutcome]) -> str:
    executable = [o for o in outcomes if o.readiness in (_READY, _DEGRADED)]
    if not executable:
        return "failed"
    if all(o.readiness == _READY for o in outcomes):
        return "full"
    return "degraded"


def _format_price(value_json: dict) -> dict:
    amount = value_json.get("amount")
    currency = value_json.get("currency")
    return {"formatted": f"{currency} {amount}"}


def _compute_canonical_hash(
    command: TruthResolutionCommand, outcomes: list[_MarketOutcome], package_status: str
) -> str:
    payload = {
        "product_version_id": command.product_version_id,
        "offer_version_id": command.offer_version_id,
        "truth_resolution_policy_version_id": command.truth_resolution_policy_version_id,
        "truth_data_mode": command.truth_data_mode,
        "resolution_rule_version": command.resolution_rule_version,
        "status": package_status,
        "markets": sorted(
            (
                {
                    "market": outcome.market,
                    "market_policy_version_id": outcome.market_policy_version_id,
                    "readiness": outcome.readiness,
                    "facts": sorted(
                        (
                            {
                                "fact_type": fact.fact_type,
                                "scope_key": fact.scope_key,
                                "evidence_hash": fact.raw_fact.evidence_hash,
                                "external_object_ref": fact.raw_fact.external_object_ref,
                                "mock_scenario": fact.raw_fact.mock_scenario,
                                "mock_version": fact.raw_fact.mock_version,
                            }
                            for fact in outcome.accepted_facts
                        ),
                        key=lambda d: (d["fact_type"], d["scope_key"]),
                    ),
                }
                for outcome in outcomes
            ),
            key=lambda d: d["market"],
        ),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public interfaces
# ---------------------------------------------------------------------------


def resolve_or_reuse_truth_package(
    session: Session,
    ctx: TenantContext,
    command: TruthResolutionCommand,
) -> TruthResolutionResult:
    now = command.now or datetime.now(timezone.utc)

    _validate_uniform_truth_data_mode(command)

    _load_effective_version(session, ctx, command.product_version_id, "ProductVersion")
    _load_effective_version(session, ctx, command.offer_version_id, "OfferVersion")
    _load_effective_version(
        session, ctx, command.truth_resolution_policy_version_id, "TruthResolutionPolicyVersion"
    )

    market_policies = {
        market_cmd.market_policy_version_id: _load_effective_market_policy_version(
            session, market_cmd.market_policy_version_id
        )
        for market_cmd in command.markets
    }

    outcomes = [
        _resolve_market(
            market_cmd,
            command,
            now,
            market_policies[market_cmd.market_policy_version_id].max_fact_staleness_seconds,
        )
        for market_cmd in command.markets
    ]

    package_status = _derive_package_status(outcomes)
    canonical_hash = _compute_canonical_hash(command, outcomes, package_status)

    existing = (
        session.query(TruthPackageRecord)
        .filter(
            TruthPackageRecord.workspace_id == ctx.workspace_id,
            TruthPackageRecord.canonical_hash == canonical_hash,
        )
        .one_or_none()
    )
    if existing is not None:
        freshness = assess_truth_freshness(session, ctx, existing.id, at=now)
        if freshness.is_current:
            readiness = {
                row.market: row.readiness
                for row in session.query(TruthPackageMarketRecord)
                .filter(
                    TruthPackageMarketRecord.workspace_id == ctx.workspace_id,
                    TruthPackageMarketRecord.truth_package_id == existing.id,
                )
                .all()
            }
            return TruthResolutionResult(
                truth_package_id=existing.id,
                status=existing.status,
                reused=True,
                market_readiness=readiness,
            )

    if outcomes:
        package_valid_until = min(o.valid_until for o in outcomes)
    else:  # pragma: no cover - a command with zero markets is degenerate
        package_valid_until = now + timedelta(seconds=command.max_staleness_seconds)

    package = TruthPackageRecord(
        id=new_id("tp"),
        workspace_id=ctx.workspace_id,
        product_version_id=command.product_version_id,
        offer_version_id=command.offer_version_id,
        truth_resolution_policy_version_id=command.truth_resolution_policy_version_id,
        truth_data_mode=command.truth_data_mode,
        canonical_hash=canonical_hash,
        status=package_status,
        valid_until=package_valid_until,
        created_at=now,
    )
    session.add(package)
    session.flush()

    for dependency_kind, dependency_version_id in (
        ("product_version", command.product_version_id),
        ("offer_version", command.offer_version_id),
        ("truth_resolution_policy_version", command.truth_resolution_policy_version_id),
    ):
        session.add(
            FactLibraryDependencyRecord(
                id=new_id("fld"),
                workspace_id=ctx.workspace_id,
                truth_package_id=package.id,
                truth_fact_id=None,
                dependency_kind=dependency_kind,
                dependency_version_id=dependency_version_id,
                created_at=now,
            )
        )

    market_readiness: dict[str, str] = {}

    for outcome in outcomes:
        market_record = TruthPackageMarketRecord(
            id=new_id("tpm"),
            workspace_id=ctx.workspace_id,
            truth_package_id=package.id,
            market_policy_version_id=outcome.market_policy_version_id,
            market=outcome.market,
            readiness=outcome.readiness,
            readiness_detail_json=outcome.readiness_detail,
            created_at=now,
        )
        session.add(market_record)
        session.flush()

        session.add(
            FactMarketPolicyDependencyRecord(
                id=new_id("fmpd"),
                workspace_id=ctx.workspace_id,
                truth_package_id=package.id,
                truth_package_market_id=market_record.id,
                market_policy_version_id=outcome.market_policy_version_id,
                created_at=now,
            )
        )

        for attempt in outcome.attempts:
            session.add(
                TruthSourceAttemptRecord(
                    id=attempt.id,
                    workspace_id=ctx.workspace_id,
                    truth_package_id=package.id,
                    truth_package_market_id=market_record.id,
                    fact_type=attempt.fact_type,
                    scope_key=attempt.scope_key,
                    priority_ordinal=attempt.priority_ordinal,
                    data_source_version_id=attempt.data_source_version_id,
                    market_source_mapping_version_id=attempt.market_source_mapping_version_id,
                    adapter_kind=attempt.adapter_kind,
                    adapter_version=attempt.adapter_version,
                    manual_fact_set_version_id=attempt.manual_fact_set_version_id,
                    resolution_rule_version=command.resolution_rule_version,
                    truth_data_mode=command.truth_data_mode,
                    requested_context_json=dict(attempt.requested_context),
                    mock_scenario=attempt.mock_scenario,
                    mock_version=attempt.mock_version,
                    outcome=attempt.outcome,
                    failure_code=attempt.failure_code,
                    skip_code=attempt.skip_code,
                    evidence_hash=attempt.evidence_hash,
                    started_at=attempt.started_at,
                    completed_at=attempt.completed_at,
                    created_at=now,
                )
            )
        session.flush()

        for fact in outcome.accepted_facts:
            fact_record = TruthFactRecord(
                id=new_id("tf"),
                workspace_id=ctx.workspace_id,
                truth_package_id=package.id,
                truth_package_market_id=market_record.id,
                fact_type=fact.fact_type,
                scope_key=fact.scope_key,
                accepted_source_attempt_id=fact.accepted_attempt_id,
                value_json=fact.value_json,
                evidence_hash=fact.raw_fact.evidence_hash,
                external_object_ref=fact.raw_fact.external_object_ref,
                requested_context_json=dict(fact.raw_fact.requested_context),
                mock_scenario=fact.raw_fact.mock_scenario,
                mock_version=fact.raw_fact.mock_version,
                observed_at=fact.raw_fact.observed_at,
                valid_until=fact.valid_until,
                source_valid_until=fact.raw_fact.source_valid_until,
                created_at=now,
            )
            session.add(fact_record)
            session.flush()

            for dependency_kind, dependency_version_id in fact.dependency_edges:
                session.add(
                    FactLibraryDependencyRecord(
                        id=new_id("fld"),
                        workspace_id=ctx.workspace_id,
                        truth_package_id=package.id,
                        truth_fact_id=fact_record.id,
                        dependency_kind=dependency_kind,
                        dependency_version_id=dependency_version_id,
                        created_at=now,
                    )
                )

            if fact.fact_type == "price":
                derived = TruthDerivedFactRecord(
                    id=new_id("tdf"),
                    workspace_id=ctx.workspace_id,
                    truth_package_id=package.id,
                    truth_package_market_id=market_record.id,
                    derived_type="formatted_price",
                    scope_key=fact.scope_key,
                    value_json=_format_price(fact.value_json),
                    derivation_rule_version=command.resolution_rule_version,
                    created_at=now,
                )
                session.add(derived)
                session.flush()
                session.add(
                    TruthDerivedFactInputRecord(
                        id=new_id("tdfi"),
                        workspace_id=ctx.workspace_id,
                        truth_package_id=package.id,
                        truth_package_market_id=market_record.id,
                        derived_fact_id=derived.id,
                        input_fact_id=fact_record.id,
                    )
                )

        market_readiness[outcome.market] = outcome.readiness

    session.flush()

    return TruthResolutionResult(
        truth_package_id=package.id,
        status=package_status,
        reused=False,
        market_readiness=market_readiness,
    )


def assess_truth_freshness(
    session: Session,
    ctx: TenantContext,
    truth_package_id: str,
    *,
    at: datetime,
) -> FreshnessReport:
    package = (
        session.query(TruthPackageRecord)
        .filter(
            TruthPackageRecord.workspace_id == ctx.workspace_id,
            TruthPackageRecord.id == truth_package_id,
        )
        .one_or_none()
    )
    if package is None:
        return FreshnessReport(is_current=False, reasons=("truth_package_not_found",))

    reasons: list[str] = []

    if not (_aware(package.valid_until) > at):
        reasons.append("valid_until_expired")

    library_deps = (
        session.query(FactLibraryDependencyRecord)
        .filter(
            FactLibraryDependencyRecord.workspace_id == ctx.workspace_id,
            FactLibraryDependencyRecord.truth_package_id == truth_package_id,
        )
        .all()
    )
    for dep in library_deps:
        version = (
            session.query(LibraryVersionRecord)
            .filter(
                LibraryVersionRecord.workspace_id == ctx.workspace_id,
                LibraryVersionRecord.id == dep.dependency_version_id,
            )
            .one_or_none()
        )
        if version is None or version.state != "effective":
            reasons.append(
                f"library_dependency_not_effective:{dep.dependency_kind}:{dep.dependency_version_id}"
            )

    market_policy_deps = (
        session.query(FactMarketPolicyDependencyRecord)
        .filter(
            FactMarketPolicyDependencyRecord.workspace_id == ctx.workspace_id,
            FactMarketPolicyDependencyRecord.truth_package_id == truth_package_id,
        )
        .all()
    )
    for dep in market_policy_deps:
        mpv = session.get(MarketPolicyVersionRecord, dep.market_policy_version_id)
        if mpv is None or mpv.version_state != "effective":
            reasons.append(f"market_policy_version_not_effective:{dep.market_policy_version_id}")

    return FreshnessReport(is_current=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Policy-driven command factory
# ---------------------------------------------------------------------------
#
# `resolve_or_reuse_truth_package()` above takes an already-built
# `TruthResolutionCommand` (adapter instances included) because the brief's
# "Consumes" list for this module does not include `LibraryVersionRecord`
# content parsing. `prepare_truth_resolution_command()` is the missing piece
# spec §2C step 1-2 actually describes ("derive sources from policy and
# mappings; callers do not provide arbitrary source version IDs"): it loads
# the effective TruthResolutionPolicyVersion and DataSourceVersion rows,
# reads their `content_json` using the shapes `app/library/contracts.py`
# already validates at publish time (`_validate_truth_resolution_policy`,
# `_validate_data_source`, `_validate_manual_fact_set`), and builds the
# `SourceStep`/`FactPolicy`/`MarketResolutionCommand` objects the existing
# (unchanged) resolution algorithm consumes.


@dataclass(frozen=True)
class MarketResolutionInput:
    """One requested Market for `prepare_truth_resolution_command`.

    `market_source_mapping_version_id`, when given, is threaded onto every
    `SourceStep` built for this market so the fact-level
    `market_source_mapping_version` dependency edge is recorded.
    """

    market_policy_version_id: str
    market_source_mapping_version_id: str | None = None


class _UnregisteredAdapter:
    """Placeholder for an `ADAPTER_KINDS` entry with no registered
    implementation (e.g. ``shopify``).

    Spec §3D: "resolution produces `adapter_not_enabled` without importing
    or instantiating a stub." This adapter never imports or knows about any
    specific unregistered provider -- it fails every requested fact type
    with a generic `adapter_not_enabled` code, for whichever `adapter_kind`
    it was told about.
    """

    def __init__(self, adapter_kind: str) -> None:
        self.adapter_kind = adapter_kind

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult:
        failures = tuple(
            FactSourceFailure(
                fact_type=fact_type,
                scope_key=_scope_key(request.offer.offer_version_id, request.market.market, fact_type),
                code="adapter_not_enabled",
                message=f"adapter_kind {self.adapter_kind!r} has no registered FactSourceAdapter",
                is_transient=False,
            )
            for fact_type in request.requested_fact_types
        )
        return FactSourceResult(facts=(), failures=failures)


def _parse_content_date(value: object) -> datetime:
    if not isinstance(value, str):
        raise TruthResolutionError(f"expected an ISO date/datetime string, got {value!r}")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validate_policy_rule(rule: object) -> dict:
    if not isinstance(rule, dict):
        raise TruthResolutionError("TruthResolutionPolicy rule must be an object")
    fact_type = rule.get("fact_type")
    if not isinstance(fact_type, str) or not fact_type:
        raise TruthResolutionError("TruthResolutionPolicy rule is missing fact_type")
    source_priority = rule.get("source_priority")
    if not isinstance(source_priority, list) or not source_priority:
        raise TruthResolutionError(f"TruthResolutionPolicy rule {fact_type!r} has an empty source_priority")
    for kind in source_priority:
        if kind not in ADAPTER_KINDS:
            raise TruthResolutionError(
                f"TruthResolutionPolicy rule {fact_type!r} references unknown adapter_kind "
                f"{kind!r} (valid: {sorted(ADAPTER_KINDS)})"
            )
    on_failure = rule.get("on_failure")
    if on_failure not in ON_FAILURE_MODES:
        raise TruthResolutionError(
            f"TruthResolutionPolicy rule {fact_type!r} has invalid on_failure {on_failure!r} "
            f"(valid: {sorted(ON_FAILURE_MODES)})"
        )
    if fact_type == "price" and on_failure in PRICE_FORBIDDEN_ON_FAILURE:
        raise TruthResolutionError("price fact_type cannot use on_failure='degrade'")
    staleness = rule.get("max_staleness_seconds")
    if not isinstance(staleness, (int, float)) or isinstance(staleness, bool) or staleness < 0:
        raise TruthResolutionError(
            f"TruthResolutionPolicy rule {fact_type!r} has invalid max_staleness_seconds"
        )
    return rule


def _validate_data_source_content(content: object, version_id: str) -> dict:
    if not isinstance(content, dict):
        raise TruthResolutionError(f"DataSourceVersion {version_id!r} content_json must be an object")
    if content.get("adapter_kind") not in ADAPTER_KINDS:
        raise TruthResolutionError(
            f"DataSourceVersion {version_id!r} has invalid adapter_kind "
            f"(valid: {sorted(ADAPTER_KINDS)})"
        )
    if content.get("execution_mode") not in EXECUTION_MODES:
        raise TruthResolutionError(
            f"DataSourceVersion {version_id!r} has invalid execution_mode "
            f"(valid: {sorted(EXECUTION_MODES)})"
        )
    return content


def _manual_fact_set_for_market(
    manual_fact_set_version_id: str, content: dict, country_code: str
) -> ManualFactSet:
    facts_by_type: dict[str, dict] = {}
    windows: list[tuple[datetime, datetime]] = []
    for raw in content.get("facts", []) or []:
        if not isinstance(raw, dict) or raw.get("country_code") != country_code:
            continue
        fact_type = raw.get("fact_type")
        if not isinstance(fact_type, str) or not fact_type:
            continue
        facts_by_type[fact_type] = raw
        windows.append((_parse_content_date(raw["valid_from"]), _parse_content_date(raw["valid_to"])))

    if not facts_by_type:
        raise TruthResolutionError(
            f"ManualFactSet {manual_fact_set_version_id!r} has no facts for country {country_code!r}"
        )

    return ManualFactSet(
        version_id=manual_fact_set_version_id,
        facts=facts_by_type,
        valid_from=max(window[0] for window in windows),
        valid_to=min(window[1] for window in windows),
    )


def _build_source_step(
    adapter_kind: str,
    data_source_version_id: str,
    data_source_content: dict,
    *,
    manual_fact_set_version_id: str | None,
    manual_fact_set_content: dict | None,
    market_country: str,
    market_source_mapping_version_id: str | None,
) -> SourceStep:
    truth_data_mode = data_source_content.get("execution_mode", "real")

    if adapter_kind == "manual":
        if manual_fact_set_version_id is None or manual_fact_set_content is None:
            raise TruthResolutionError(
                "TruthResolutionPolicy references the 'manual' adapter_kind but no "
                "manual_fact_set_version_id was supplied"
            )
        return SourceStep(
            data_source_version_id=data_source_version_id,
            adapter=ManualFactSourceAdapter(),
            adapter_kind=ManualFactSourceAdapter.adapter_kind,
            adapter_version="manual-v1",
            truth_data_mode=truth_data_mode,
            market_source_mapping_version_id=market_source_mapping_version_id,
            manual_fact_set=_manual_fact_set_for_market(
                manual_fact_set_version_id, manual_fact_set_content, market_country
            ),
            manual_fact_set_version_id=manual_fact_set_version_id,
        )

    if adapter_kind == "mock":
        config = data_source_content.get("config") or {}
        mock_scenario = config.get("mock_scenario") if isinstance(config, dict) else None
        scenarios_by_country = (
            config.get("mock_scenarios_by_country")
            if isinstance(config, dict)
            else None
        )
        if isinstance(scenarios_by_country, dict):
            country_scenario = scenarios_by_country.get(market_country)
            if isinstance(country_scenario, str) and country_scenario:
                mock_scenario = country_scenario
        return SourceStep(
            data_source_version_id=data_source_version_id,
            adapter=ControlledMockFactSourceAdapter(),
            adapter_kind=ControlledMockFactSourceAdapter.adapter_kind,
            adapter_version=ControlledMockFactSourceAdapter._VERSION,
            truth_data_mode=truth_data_mode,
            market_source_mapping_version_id=market_source_mapping_version_id,
            mock_scenario=mock_scenario,
            mock_version=ControlledMockFactSourceAdapter._VERSION,
        )

    # Any other registered `ADAPTER_KINDS` entry (e.g. "shopify") has no
    # implementation in this ticket. Publishing such a DataSourceVersion is
    # valid (spec §3D); resolving against it always yields
    # `adapter_not_enabled` through the generic placeholder above.
    return SourceStep(
        data_source_version_id=data_source_version_id,
        adapter=_UnregisteredAdapter(adapter_kind),
        adapter_kind=adapter_kind,
        adapter_version="unregistered",
        truth_data_mode=truth_data_mode,
        market_source_mapping_version_id=market_source_mapping_version_id,
    )


def prepare_truth_resolution_command(
    session: Session,
    ctx: TenantContext,
    *,
    product_version_id: str,
    offer_version_id: str,
    truth_resolution_policy_version_id: str,
    data_source_version_ids: tuple[str, ...],
    markets: tuple[MarketResolutionInput, ...],
    manual_fact_set_version_id: str | None = None,
    allow_fallback: bool = True,
    now: datetime | None = None,
) -> TruthResolutionCommand:
    """Derive a `TruthResolutionCommand` from persisted policy/source content.

    Callers supply only version ids (per spec §2C step 2: "callers do not
    provide arbitrary source version IDs" -- they name which
    TruthResolutionPolicyVersion and DataSourceVersions are in scope, and
    this function derives which adapter serves which fact type, in what
    order, from the policy's own `rules[].source_priority`).

    All fact-type rules in the policy must share one `source_priority`: the
    resolution algorithm (`resolve_or_reuse_truth_package`, unchanged by
    this function) tries one shared, market-wide ordered list of sources per
    Market, grouping whichever fact types remain unresolved at each step
    (spec §2C step 3). A policy with genuinely divergent per-fact-type
    source ordering cannot be expressed against that shape yet and is
    rejected with a clear `TruthResolutionError` rather than silently
    resolved against the wrong order.
    """
    policy_version = _load_effective_version(
        session, ctx, truth_resolution_policy_version_id, "TruthResolutionPolicyVersion"
    )
    rules = policy_version.content_json.get("rules")
    if not isinstance(rules, list) or not rules:
        raise TruthResolutionError(
            f"TruthResolutionPolicyVersion {truth_resolution_policy_version_id!r} has no rules"
        )
    rules = [_validate_policy_rule(rule) for rule in rules]

    source_priorities = {tuple(rule["source_priority"]) for rule in rules}
    if len(source_priorities) > 1:
        raise TruthResolutionError(
            "prepare_truth_resolution_command does not support a "
            "TruthResolutionPolicy whose rules disagree on source_priority: "
            f"{sorted(source_priorities)}"
        )
    source_priority = next(iter(source_priorities))
    max_staleness_seconds = min(int(rule["max_staleness_seconds"]) for rule in rules)
    fact_policies = tuple(
        FactPolicy(fact_type=rule["fact_type"], on_failure=rule["on_failure"]) for rule in rules
    )

    data_sources: dict[str, tuple[str, dict]] = {}
    for version_id in data_source_version_ids:
        version = _load_effective_version(session, ctx, version_id, "DataSourceVersion")
        content = _validate_data_source_content(version.content_json, version_id)
        adapter_kind = content["adapter_kind"]
        if adapter_kind in data_sources:
            raise TruthResolutionError(
                f"multiple DataSourceVersions supplied for adapter_kind {adapter_kind!r}"
            )
        data_sources[adapter_kind] = (version_id, content)

    missing = [kind for kind in source_priority if kind not in data_sources]
    if missing:
        raise TruthResolutionError(
            f"no DataSourceVersion supplied for adapter_kind(s) {missing!r} "
            f"referenced by TruthResolutionPolicyVersion {truth_resolution_policy_version_id!r}"
        )

    manual_fact_set_content: dict | None = None
    if "manual" in source_priority:
        if manual_fact_set_version_id is None:
            raise TruthResolutionError(
                "TruthResolutionPolicy requires the 'manual' adapter_kind but no "
                "manual_fact_set_version_id was supplied"
            )
        manual_version = _load_effective_version(
            session, ctx, manual_fact_set_version_id, "ManualFactSetVersion"
        )
        manual_fact_set_content = manual_version.content_json

    truth_data_mode = data_sources[source_priority[0]][1]["execution_mode"]

    market_commands: list[MarketResolutionCommand] = []
    for market_input in markets:
        market_policy_version = _load_effective_market_policy_version(
            session, market_input.market_policy_version_id
        )
        market_policy = session.get(MarketPolicyRecord, market_policy_version.market_policy_id)
        if market_policy is None:  # pragma: no cover - FK-guaranteed, defensive only
            raise TruthResolutionError(
                f"MarketPolicyVersion {market_input.market_policy_version_id!r} has no MarketPolicy"
            )

        sources = tuple(
            _build_source_step(
                adapter_kind,
                *data_sources[adapter_kind],
                manual_fact_set_version_id=manual_fact_set_version_id,
                manual_fact_set_content=manual_fact_set_content,
                market_country=market_policy_version.country,
                market_source_mapping_version_id=market_input.market_source_mapping_version_id,
            )
            for adapter_kind in source_priority
        )

        market_commands.append(
            MarketResolutionCommand(
                market=market_policy.market_id,
                locale=market_policy_version.locale,
                country=market_policy_version.country,
                currency=market_policy_version.currency,
                market_policy_version_id=market_input.market_policy_version_id,
                sources=sources,
                fact_policies=fact_policies,
                allow_fallback=allow_fallback,
            )
        )

    return TruthResolutionCommand(
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        truth_resolution_policy_version_id=truth_resolution_policy_version_id,
        truth_data_mode=truth_data_mode,
        max_staleness_seconds=max_staleness_seconds,
        resolution_rule_version="truth-rule-v2",
        markets=tuple(market_commands),
        now=now,
    )
