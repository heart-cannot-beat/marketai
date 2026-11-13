# app/meta/scale_read_contracts.py
"""Scale's four Meta read contracts, as code (Scale contract §6.2, #101 S2).

Scale reads Meta through four independent contracts (§6.2): the ad-level daily
contract, the exact-window reach/frequency contract, one contract per
breakdown family, and the delivery-configuration contract. Each one freezes
the API version, level, fields, ``time_increment``, action report time, the
7d_click + 1d_view attribution windows, the purchase action type, the
breakdowns, pagination and the parser rules, and has a contract hash over
exactly that definition. The account, its time zone and its currency are not
part of the definition: they are frozen per connection on the
``ObservationContractGenerationRecord`` built from :func:`generation_spec`,
whose own ``contract_hash`` covers them.

A contract reads one or more **query instances** for each target day D
(#101 planning comment §3):

=====================  ==============================  =================  ==============
contract               instance (window role)          local days         time_increment
=====================  ==============================  =================  ==============
daily                  ``daily``                       [D-13, D]          1
exact_window           ``current_7d``                  [D-6, D]           all_days
exact_window           ``baseline_7d``                 [D-13, D-7]        all_days
exact_window           ``rolling_14d``                 [D-13, D]          all_days
breakdown_placement    ``current_7d``                  [D-6, D]           1
breakdown_country      ``current_7d``                  [D-6, D]           1
breakdown_age_gender   ``current_7d``                  [D-6, D]           1
delivery_configuration ``configuration``               read time (D)      --
=====================  ==============================  =================  ==============

The daily read covers the connection's 14 days; clamping to an Episode's
start day is the collector's job (#109), because one connection serves many
Episodes. The configuration instance reads four edges in order -- the
account, its ads, ad sets and campaigns -- as one page chain.

The breakdown contracts are defined, hashed and tested, but the first batch
neither schedules nor reads them: :data:`SCALE_READ_POLICY` declares the
empty set of breakdown families (#101 maintainer decision 2026-11-10, Q6).

This module is pure -- standard library only -- so Scale can import it
without reaching an adapter (ADR 0017).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

__all__ = [
    "DAILY",
    "EXACT_WINDOW",
    "BREAKDOWN_PLACEMENT",
    "BREAKDOWN_COUNTRY",
    "BREAKDOWN_AGE_GENDER",
    "DELIVERY_CONFIGURATION",
    "BREAKDOWN_CONTRACT_TYPES",
    "INSIGHTS_READ",
    "CONFIGURATION_READ",
    "META_GRAPH_API_VERSION",
    "ACTION_ATTRIBUTION_WINDOWS",
    "ACTION_REPORT_TIME",
    "PURCHASE_ACTION_TYPE",
    "PAGE_LIMIT",
    "PARSER_RULES",
    "CONFIGURATION_EDGES",
    "SANDBOX_DEFAULT_CURRENCY",
    "SANDBOX_DEFAULT_TIMEZONE",
    "QueryInstance",
    "ScaleReadContract",
    "AccountBinding",
    "ScaleReadPolicy",
    "ScaleReadContractError",
    "CONTRACTS",
    "SCALE_READ_POLICY",
    "contract_for",
    "generation_spec",
    "resolve_generation_contract",
    "sandbox_account_binding",
]

DAILY = "daily"
EXACT_WINDOW = "exact_window"
BREAKDOWN_PLACEMENT = "breakdown_placement"
BREAKDOWN_COUNTRY = "breakdown_country"
BREAKDOWN_AGE_GENDER = "breakdown_age_gender"
DELIVERY_CONFIGURATION = "delivery_configuration"
BREAKDOWN_CONTRACT_TYPES = frozenset({BREAKDOWN_PLACEMENT, BREAKDOWN_COUNTRY, BREAKDOWN_AGE_GENDER})

#: What a query instance reads: an insights report, or the configuration edges.
INSIGHTS_READ = "insights"
CONFIGURATION_READ = "delivery_configuration"

#: Frozen in every contract. The internal_validation read-only canary (#101
#: S6, risk 1) proves the fields, breakdowns and windows on this version
#: before a real account is read; a new version is a new contract.
META_GRAPH_API_VERSION = "v21.0"
ACTION_ATTRIBUTION_WINDOWS = ("7d_click", "1d_view")
ACTION_REPORT_TIME = "impression"
PURCHASE_ACTION_TYPE = "purchase"
PAGE_LIMIT = 25
#: Version of the parsing rules in ``app.meta.scale_read_parsers``.
PARSER_RULES = "scale-read-parse-v1"
_HASH_DOMAIN = "scale-meta-read-contract-v1"

_INSIGHTS_IDENTITY_FIELDS = ("account_id", "account_currency", "ad_id")
_DAILY_METRIC_FIELDS = ("spend", "impressions", "inline_link_clicks", "actions", "action_values")
_WINDOW_METRIC_FIELDS = ("reach", "frequency", *_DAILY_METRIC_FIELDS)

#: The configuration instance reads these edges, in this order, as one page
#: chain: the account first (its currency and time zone are checked against
#: the generation), then every ad, ad set and campaign of the account.
CONFIGURATION_EDGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account", ("account_id", "currency", "timezone_name")),
    ("ads", ("id", "account_id", "adset_id", "campaign_id", "creative", "status", "effective_status")),
    ("adsets", (
        "id", "campaign_id", "status", "effective_status", "daily_budget", "lifetime_budget",
        "optimization_goal", "bid_strategy", "attribution_spec", "targeting", "learning_stage_info",
    )),
    ("campaigns", ("id", "status", "effective_status", "daily_budget", "lifetime_budget", "bid_strategy")),
)

#: A sandbox account that its scenario does not describe (#101 S2): the
#: Workspace default currency's own default, and UTC.
SANDBOX_DEFAULT_CURRENCY = "USD"
SANDBOX_DEFAULT_TIMEZONE = "UTC"


class ScaleReadContractError(ValueError):
    """A generation that names a Scale read contract it does not match."""

    def __init__(self, message: str = "", *, code: str = "meta_read_contract_mismatch") -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class QueryInstance:
    """One read a contract makes for every target day D.

    ``since_offset_days`` and ``until_offset_days`` are relative to D (zero
    or negative). A configuration instance reads at a point in time and has
    no ``time_increment``.
    """

    name: str
    read: str
    since_offset_days: int = 0
    until_offset_days: int = 0
    time_increment: str | None = None
    window_role: str | None = None

    def time_range(self, target_date: date) -> tuple[date, date]:
        return (target_date + timedelta(days=self.since_offset_days),
                target_date + timedelta(days=self.until_offset_days))

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "read": self.read,
            "since_offset_days": self.since_offset_days,
            "until_offset_days": self.until_offset_days,
            "time_increment": self.time_increment,
            "window_role": self.window_role,
        }


@dataclass(frozen=True)
class ScaleReadContract:
    """One of §6.2's four contract kinds, at one version."""

    contract_type: str
    version: int
    fields: tuple[str, ...]
    instances: tuple[QueryInstance, ...]
    breakdowns: tuple[str, ...] = ()
    level: str = "ad"
    edges: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def is_configuration(self) -> bool:
        return self.contract_type == DELIVERY_CONFIGURATION

    def attribution(self) -> dict[str, Any]:
        """The attribution a metric read asks for; a configuration read has none."""
        if self.is_configuration:
            return {}
        return {
            "action_attribution_windows": list(ACTION_ATTRIBUTION_WINDOWS),
            "action_report_time": ACTION_REPORT_TIME,
            "purchase_action_type": PURCHASE_ACTION_TYPE,
        }

    def definition(self) -> dict[str, Any]:
        return {
            "contract_type": self.contract_type,
            "version": self.version,
            "api_version": META_GRAPH_API_VERSION,
            "level": self.level,
            "fields": sorted(self.fields),
            "breakdowns": list(self.breakdowns),
            "attribution": self.attribution(),
            "edges": {edge: list(fields) for edge, fields in self.edges},
            "pagination": {"limit": PAGE_LIMIT},
            "parser_rules": PARSER_RULES,
            "instances": [instance.definition() for instance in self.instances],
        }

    @property
    def contract_hash(self) -> str:
        """SHA-256 over the domain separator and the canonical definition."""
        payload = f"{_HASH_DOMAIN}\n{_canonical(self.definition())}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def instance(self, name: str) -> QueryInstance:
        for instance in self.instances:
            if instance.name == name:
                return instance
        raise ScaleReadContractError(
            f"contract {self.contract_type!r} has no query instance {name!r}",
            code="meta_read_instance_unknown",
        )


def _insights_contract(
    contract_type: str, metric_fields: tuple[str, ...], instances: tuple[QueryInstance, ...],
    breakdowns: tuple[str, ...] = (),
) -> ScaleReadContract:
    return ScaleReadContract(
        contract_type=contract_type,
        version=1,
        fields=tuple(sorted({*_INSIGHTS_IDENTITY_FIELDS, *metric_fields})),
        instances=instances,
        breakdowns=breakdowns,
    )


def _breakdown_contract(contract_type: str, breakdowns: tuple[str, ...]) -> ScaleReadContract:
    return _insights_contract(
        contract_type, _DAILY_METRIC_FIELDS,
        (QueryInstance("current_7d", INSIGHTS_READ, -6, 0, "1", "current_7d"),),
        breakdowns,
    )


#: The current version of each contract kind.
CONTRACTS: Mapping[str, ScaleReadContract] = {
    DAILY: _insights_contract(
        DAILY, _DAILY_METRIC_FIELDS,
        (QueryInstance("daily", INSIGHTS_READ, -13, 0, "1"),),
    ),
    EXACT_WINDOW: _insights_contract(
        EXACT_WINDOW, _WINDOW_METRIC_FIELDS,
        (
            QueryInstance("current_7d", INSIGHTS_READ, -6, 0, "all_days", "current_7d"),
            QueryInstance("baseline_7d", INSIGHTS_READ, -13, -7, "all_days", "baseline_7d"),
            QueryInstance("rolling_14d", INSIGHTS_READ, -13, 0, "all_days", "rolling_14d"),
        ),
    ),
    BREAKDOWN_PLACEMENT: _breakdown_contract(BREAKDOWN_PLACEMENT, ("publisher_platform", "platform_position")),
    BREAKDOWN_COUNTRY: _breakdown_contract(BREAKDOWN_COUNTRY, ("country",)),
    BREAKDOWN_AGE_GENDER: _breakdown_contract(BREAKDOWN_AGE_GENDER, ("age", "gender")),
    DELIVERY_CONFIGURATION: ScaleReadContract(
        contract_type=DELIVERY_CONFIGURATION,
        version=1,
        fields=tuple(sorted(f"{edge}.{field}" for edge, fields in CONFIGURATION_EDGES for field in fields)),
        instances=(QueryInstance("configuration", CONFIGURATION_READ),),
        level="account",
        edges=CONFIGURATION_EDGES,
    ),
}


def contract_for(contract_type: str) -> ScaleReadContract:
    try:
        return CONTRACTS[contract_type]
    except KeyError:
        raise ScaleReadContractError(
            f"no Scale read contract {contract_type!r}", code="meta_read_contract_unknown",
        ) from None


@dataclass(frozen=True)
class ScaleReadPolicy:
    """Which contracts Scale schedules and reads for a connection."""

    breakdown_contract_types: frozenset[str]

    def required_contract_types(self) -> tuple[str, ...]:
        return (DAILY, EXACT_WINDOW, *sorted(self.breakdown_contract_types), DELIVERY_CONFIGURATION)


#: The first batch reads no breakdown family (#101 Q6): the Scale page shows
#: an empty breakdown area until a later slice adds placement and country.
SCALE_READ_POLICY = ScaleReadPolicy(breakdown_contract_types=frozenset())


@dataclass(frozen=True)
class AccountBinding:
    """The account a connection's contracts are frozen to."""

    ad_account_id: str
    currency: str
    timezone_name: str


def generation_spec(contract: ScaleReadContract, binding: AccountBinding) -> dict[str, Any]:
    """Keyword arguments for ``create_contract_generation`` of ``contract``.

    The generation names the contract, its version and hash in
    ``time_window``, with the account's currency and time zone, so the
    generation's own ``contract_hash`` covers all of them.
    """
    return {
        "contract_type": contract.contract_type,
        "ad_account_id": binding.ad_account_id,
        "fields": list(contract.fields),
        "attribution_window": contract.attribution(),
        "breakdowns": list(contract.breakdowns),
        "time_window": {
            "scale_read_contract": contract.contract_type,
            "version": contract.version,
            "definition_hash": contract.contract_hash,
            "instances": [instance.name for instance in contract.instances],
            "account_currency": binding.currency,
            "account_timezone": binding.timezone_name,
        },
        "delivery_config": (
            {"edges": {edge: list(fields) for edge, fields in contract.edges}}
            if contract.is_configuration else None
        ),
        "local_timezone": binding.timezone_name,
    }


def resolve_generation_contract(
    *,
    contract_type: str,
    ad_account_id: str,
    fields: Any,
    attribution_window: Any,
    breakdowns: Any,
    time_window: Any,
    delivery_config: Any,
    local_timezone: str,
) -> tuple[ScaleReadContract, AccountBinding] | None:
    """The Scale read contract a generation was built from, or ``None``.

    A generation whose ``time_window`` does not name a Scale read contract is
    not one (``None``). One that names a contract must match the current code
    definition field for field, or this raises -- a contract never drops a
    field at run time and carries on (§6.2).
    """
    if not isinstance(time_window, Mapping) or "scale_read_contract" not in time_window:
        return None
    name = time_window.get("scale_read_contract")
    contract = CONTRACTS.get(name) if isinstance(name, str) else None
    if contract is None or time_window.get("version") != contract.version:
        raise ScaleReadContractError(
            f"generation names Scale read contract {name!r} version {time_window.get('version')!r}, "
            "which this deployment does not define",
            code="meta_read_contract_unknown",
        )
    binding = AccountBinding(
        ad_account_id=ad_account_id,
        currency=str(time_window.get("account_currency") or ""),
        timezone_name=str(time_window.get("account_timezone") or ""),
    )
    expected = generation_spec(contract, binding)
    actual = {
        "contract_type": contract_type,
        "ad_account_id": ad_account_id,
        "fields": fields,
        "attribution_window": attribution_window,
        "breakdowns": breakdowns,
        "time_window": time_window,
        "delivery_config": delivery_config,
        "local_timezone": local_timezone,
    }
    drifted = sorted(key for key in expected if _canonical(expected[key]) != _canonical(actual[key]))
    if drifted or not binding.currency or not binding.timezone_name:
        raise ScaleReadContractError(
            f"generation differs from Scale read contract {contract.contract_type!r} "
            f"v{contract.version} in {drifted or ['account binding']}",
            code="meta_read_contract_mismatch",
        )
    return contract, binding


def sandbox_account_binding(ad_account_id: str, scenario_config: Mapping[str, Any] | None) -> AccountBinding:
    """The account a sandbox scenario simulates.

    ``scenario_config["account"]`` may set ``currency`` and ``timezone_name``;
    otherwise the account is in :data:`SANDBOX_DEFAULT_CURRENCY` and
    :data:`SANDBOX_DEFAULT_TIMEZONE`. The sandbox adapter answers the account
    read with these facts, and the contract set for a sandbox connection is
    frozen to them, so the two agree unless a scenario says otherwise.
    """
    account = (scenario_config or {}).get("account") or {}
    return AccountBinding(
        ad_account_id=ad_account_id,
        currency=str(account.get("currency") or SANDBOX_DEFAULT_CURRENCY),
        timezone_name=str(account.get("timezone_name") or SANDBOX_DEFAULT_TIMEZONE),
    )
