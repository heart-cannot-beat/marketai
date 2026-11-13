# app/meta/scale_read_parsers.py
"""Parse a query instance's raw pages into typed observations (#101 S2).

Pure functions over the Meta-shaped page bodies a sync run stored (``data``
plus ``paging``): no database, no clock, no adapter. They return frozen value
objects for one ad, which is all Scale consumes -- Scale never imports the
Meta adapters (ADR 0017), and it never re-reads raw JSON itself.

What every parser proves, or refuses with :class:`ScaleReadParseError` and a
stable ``reason`` (Scale contract §8, #101 S2):

* every row belongs to the contract's account and currency
  (``account_mismatch``, ``currency_mismatch``), and the account read agrees
  with the contract's time zone and currency (``timezone_mismatch``,
  ``currency_mismatch``);
* every row lies inside the requested window, one local day per row for a
  daily read and the exact window for an ``all_days`` read
  (``outside_window``);
* the ad's rows are unique per day, or per segment and day
  (``duplicate_row``);
* the ad's values are well formed (``malformed_row``), non-negative
  (``negative_value``) and link clicks never exceed impressions
  (``clicks_exceed_impressions``).

Rows of other ads are checked for account and currency, then ignored: they
never change this ad's values. A day of a complete read with no row for the
ad is observed zero delivery -- present, ``delivered=False``, every primitive
zero -- not missing evidence (#101 Q7). A breakdown segment Meta withheld
(its row carries the segment and no metric) becomes a ``privacy_suppressed``
placeholder with a typed reason; none of its values are kept (§13).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from .scale_read_contracts import PURCHASE_ACTION_TYPE, AccountBinding

__all__ = [
    "ScaleReadParseError",
    "DayObservation",
    "DailyObservation",
    "WindowObservation",
    "BreakdownSegmentObservation",
    "BreakdownObservation",
    "DeliveryConfigurationObservation",
    "parse_daily",
    "parse_window",
    "parse_breakdown",
    "parse_delivery_configuration",
]

OBSERVED = "observed"
PRIVACY_SUPPRESSED = "privacy_suppressed"
API_PRIVACY_THRESHOLD = "api_privacy_threshold"
_METRIC_KEYS = ("spend", "impressions", "inline_link_clicks", "actions", "action_values")
_LEARNING_STAGES = {"LEARNING": "learning", "FAIL": "learning_limited", "SUCCESS": "none"}


class ScaleReadParseError(ValueError):
    """Pages that cannot be evidence. ``reason`` is stable; S3 maps it to a
    blocked source manifest (#109)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class DayObservation:
    """One account-local day of one ad (or one segment of it)."""

    local_day: date
    delivered: bool
    spend: Decimal
    impressions: int
    link_clicks: int
    purchases: int
    purchase_value: Decimal


@dataclass(frozen=True)
class DailyObservation:
    ad_id: str
    ad_account_id: str
    currency: str
    timezone_name: str
    since: date
    until: date
    days: tuple[DayObservation, ...]


@dataclass(frozen=True)
class WindowObservation:
    """An exact window's totals. ``reach`` and ``frequency`` come from the
    window query itself (§6.3); without delivery reach is 0 and frequency is
    unavailable (``None``)."""

    ad_id: str
    ad_account_id: str
    currency: str
    timezone_name: str
    window_role: str
    since: date
    until: date
    delivered: bool
    reach: int
    frequency: Decimal | None
    spend: Decimal
    impressions: int
    link_clicks: int
    purchases: int
    purchase_value: Decimal


@dataclass(frozen=True)
class BreakdownSegmentObservation:
    segment_key: str
    state: str
    suppression_reason: str | None
    days: tuple[DayObservation, ...]


@dataclass(frozen=True)
class BreakdownObservation:
    ad_id: str
    ad_account_id: str
    currency: str
    timezone_name: str
    breakdowns: tuple[str, ...]
    window_role: str
    since: date
    until: date
    segments: tuple[BreakdownSegmentObservation, ...]


@dataclass(frozen=True)
class DeliveryConfigurationObservation:
    """One ad's delivery configuration at read time. Budgets are in the
    account currency's minor units, as Meta reports them; targeting and the
    attribution spec are kept only as hashes (§4.2)."""

    ad_account_id: str
    currency: str
    timezone_name: str
    ad_id: str
    ad_creative_id: str
    ad_status: str
    ad_effective_status: str
    adset_id: str
    adset_status: str
    adset_effective_status: str
    campaign_id: str
    campaign_status: str
    campaign_effective_status: str
    budget_source: str
    budget_amount_minor: int
    optimization_goal: str
    bid_strategy: str
    attribution_spec_hash: str
    targeting_safety_hash: str
    learning_stage: str


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


def _fail(reason: str, detail: str) -> ScaleReadParseError:
    return ScaleReadParseError(reason, detail)


def _rows(pages: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for index, page in enumerate(pages, start=1):
        data = page.get("data") if isinstance(page, Mapping) else None
        if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
            raise _fail("malformed_row", f"page {index} has no list of rows")
        rows.extend(data)
    return rows


def _account_number(value: Any) -> str:
    return str(value or "").removeprefix("act_")


def _check_identity(row: Mapping[str, Any], expected: AccountBinding) -> None:
    if _account_number(row.get("account_id")) != _account_number(expected.ad_account_id):
        raise _fail("account_mismatch", f"row of account {row.get('account_id')!r}")
    if row.get("account_currency") != expected.currency:
        raise _fail("currency_mismatch",
                    f"row in {row.get('account_currency')!r}, contract in {expected.currency!r}")


def _day(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise _fail("malformed_row", f"{field} {value!r} is not a date") from None


def _check_span(row: Mapping[str, Any], since: date, until: date, *, exact: bool) -> date:
    start, stop = _day(row.get("date_start"), "date_start"), _day(row.get("date_stop"), "date_stop")
    if exact:
        if (start, stop) != (since, until):
            raise _fail("outside_window", f"row spans {start}..{stop}, window is {since}..{until}")
    elif start != stop or not since <= start <= until:
        raise _fail("outside_window", f"row spans {start}..{stop}, window is {since}..{until} by day")
    return start


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _fail("malformed_row", f"{field} {value!r} is not a number") from None
    if not number.is_finite():
        raise _fail("malformed_row", f"{field} {value!r} is not a number")
    if number < 0:
        raise _fail("negative_value", f"{field} is {value!r}")
    return number


def _count(value: Any, field: str) -> int:
    number = _decimal(value, field)
    if number != number.to_integral_value():
        raise _fail("malformed_row", f"{field} {value!r} is not a whole number")
    return int(number)


def _action_total(entries: Any, field: str, action_type: str) -> Decimal:
    if not isinstance(entries, list):
        raise _fail("malformed_row", f"{field} is not a list")
    total = Decimal(0)
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _fail("malformed_row", f"{field} entry is not an object")
        if entry.get("action_type") == action_type:
            total += _decimal(entry.get("value"), f"{field}[{action_type}]")
    return total


def _metrics(row: Mapping[str, Any], local_day: date, action_type: str) -> DayObservation:
    missing = [key for key in ("spend", "impressions", "inline_link_clicks") if key not in row]
    if missing:
        raise _fail("malformed_row", f"row of {local_day} lacks {missing}")
    impressions = _count(row["impressions"], "impressions")
    link_clicks = _count(row["inline_link_clicks"], "inline_link_clicks")
    if "clicks" in row and _count(row["clicks"], "clicks") > impressions:
        raise _fail("clicks_exceed_impressions", f"{row['clicks']} clicks, {impressions} impressions on {local_day}")
    if link_clicks > impressions:
        raise _fail("clicks_exceed_impressions", f"{link_clicks} link clicks, {impressions} impressions on {local_day}")
    purchases = _action_total(row.get("actions", []), "actions", action_type)
    if purchases != purchases.to_integral_value():
        raise _fail("malformed_row", f"{purchases} purchases is not a whole number")
    return DayObservation(
        local_day=local_day,
        delivered=impressions > 0,
        spend=_decimal(row["spend"], "spend"),
        impressions=impressions,
        link_clicks=link_clicks,
        purchases=int(purchases),
        purchase_value=_action_total(row.get("action_values", []), "action_values", action_type),
    )


def _zero(local_day: date) -> DayObservation:
    return DayObservation(local_day, False, Decimal(0), 0, 0, 0, Decimal(0))


def _days(since: date, until: date) -> list[date]:
    if since > until:
        raise _fail("outside_window", f"window {since}..{until} is empty")
    return [since + timedelta(days=offset) for offset in range((until - since).days + 1)]


def _ad_rows(rows: list[Mapping[str, Any]], expected: AccountBinding, ad_id: str) -> list[Mapping[str, Any]]:
    for row in rows:
        _check_identity(row, expected)
    return [row for row in rows if str(row.get("ad_id")) == ad_id]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_daily(
    pages: Sequence[Mapping[str, Any]],
    *,
    expected: AccountBinding,
    ad_id: str,
    since: date,
    until: date,
    purchase_action_type: str = PURCHASE_ACTION_TYPE,
) -> DailyObservation:
    """Every local day of ``[since, until]`` for ``ad_id``, from a daily read."""
    by_day: dict[date, DayObservation] = {}
    for row in _ad_rows(_rows(pages), expected, ad_id):
        local_day = _check_span(row, since, until, exact=False)
        if local_day in by_day:
            raise _fail("duplicate_row", f"two rows for ad {ad_id} on {local_day}")
        by_day[local_day] = _metrics(row, local_day, purchase_action_type)
    return DailyObservation(
        ad_id=ad_id,
        ad_account_id=expected.ad_account_id,
        currency=expected.currency,
        timezone_name=expected.timezone_name,
        since=since,
        until=until,
        days=tuple(by_day.get(day) or _zero(day) for day in _days(since, until)),
    )


def parse_window(
    pages: Sequence[Mapping[str, Any]],
    *,
    expected: AccountBinding,
    ad_id: str,
    window_role: str,
    since: date,
    until: date,
    purchase_action_type: str = PURCHASE_ACTION_TYPE,
) -> WindowObservation:
    """The exact window's totals for ``ad_id``, from an ``all_days`` read."""
    _days(since, until)
    rows = _ad_rows(_rows(pages), expected, ad_id)
    for row in rows:
        _check_span(row, since, until, exact=True)
    if len(rows) > 1:
        raise _fail("duplicate_row", f"{len(rows)} rows for ad {ad_id} in window {window_role}")
    if not rows:
        totals, reach, frequency = _zero(until), 0, None
    else:
        row = rows[0]
        totals = _metrics(row, until, purchase_action_type)
        if "reach" not in row or "frequency" not in row:
            raise _fail("malformed_row", f"window {window_role} row lacks reach or frequency")
        reach, frequency = _count(row["reach"], "reach"), _decimal(row["frequency"], "frequency")
        if reach > totals.impressions:
            raise _fail("malformed_row", f"reach {reach} exceeds {totals.impressions} impressions")
    return WindowObservation(
        ad_id=ad_id,
        ad_account_id=expected.ad_account_id,
        currency=expected.currency,
        timezone_name=expected.timezone_name,
        window_role=window_role,
        since=since,
        until=until,
        delivered=totals.delivered,
        reach=reach,
        frequency=frequency,
        spend=totals.spend,
        impressions=totals.impressions,
        link_clicks=totals.link_clicks,
        purchases=totals.purchases,
        purchase_value=totals.purchase_value,
    )


def parse_breakdown(
    pages: Sequence[Mapping[str, Any]],
    *,
    expected: AccountBinding,
    ad_id: str,
    breakdowns: Sequence[str],
    window_role: str,
    since: date,
    until: date,
    purchase_action_type: str = PURCHASE_ACTION_TYPE,
) -> BreakdownObservation:
    """Each segment Meta reported for ``ad_id``, day by day.

    A segment is ``privacy_suppressed`` when any of its rows carries no
    metric; it then keeps no value at all, only the typed reason. An observed
    segment's day without a row is observed zero delivery.
    """
    breakdowns = tuple(breakdowns)
    observed: dict[str, dict[date, DayObservation]] = {}
    suppressed: set[str] = set()
    seen: set[tuple[str, date]] = set()
    for row in _ad_rows(_rows(pages), expected, ad_id):
        local_day = _check_span(row, since, until, exact=False)
        if any(not isinstance(row.get(key), str) or not row.get(key) for key in breakdowns):
            raise _fail("malformed_row", f"row of {local_day} lacks a value for {list(breakdowns)}")
        segment_key = ":".join(str(row[key]) for key in breakdowns)
        if (segment_key, local_day) in seen:
            raise _fail("duplicate_row", f"two rows for ad {ad_id}, segment {segment_key} on {local_day}")
        seen.add((segment_key, local_day))
        present = [key for key in _METRIC_KEYS if key in row]
        if not present:
            suppressed.add(segment_key)
            continue
        observed.setdefault(segment_key, {})[local_day] = _metrics(row, local_day, purchase_action_type)
    days = _days(since, until)
    segments = [
        BreakdownSegmentObservation(key, PRIVACY_SUPPRESSED, API_PRIVACY_THRESHOLD, ())
        if key in suppressed else
        BreakdownSegmentObservation(key, OBSERVED, None,
                                    tuple(observed[key].get(day) or _zero(day) for day in days))
        for key in sorted(suppressed | set(observed))
    ]
    return BreakdownObservation(
        ad_id=ad_id,
        ad_account_id=expected.ad_account_id,
        currency=expected.currency,
        timezone_name=expected.timezone_name,
        breakdowns=breakdowns,
        window_role=window_role,
        since=since,
        until=until,
        segments=tuple(segments),
    )


def _hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text(row: Mapping[str, Any], field: str, edge: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise _fail("malformed_row", f"{edge} row {row.get('id')!r} lacks {field}")
    return value


def _unique(rows: list[Mapping[str, Any]], edge: str, object_id: str) -> Mapping[str, Any]:
    matches = [row for row in rows if str(row.get("id")) == object_id]
    if not matches:
        raise _fail("ad_not_found", f"no {edge} row {object_id!r}")
    if len(matches) > 1:
        raise _fail("duplicate_row", f"{len(matches)} {edge} rows {object_id!r}")
    return matches[0]


def _budget(row: Mapping[str, Any], level: str) -> tuple[str, int] | None:
    for kind in ("daily", "lifetime"):
        value = row.get(f"{kind}_budget")
        if value not in (None, "", "0"):
            return f"{level}_{kind}", _count(value, f"{level}.{kind}_budget")
    return None


def parse_delivery_configuration(
    edge_pages: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    expected: AccountBinding,
    ad_id: str,
) -> DeliveryConfigurationObservation:
    """``ad_id``'s configuration from the configuration instance's pages.

    ``edge_pages`` pairs each stored page with the edge its call intent read
    (``account``, ``ads``, ``adsets`` or ``campaigns``). The account row must
    agree with the contract's currency and time zone; the ad must resolve to
    exactly one ad, ad set and campaign row.
    """
    by_edge: dict[str, list[Mapping[str, Any]]] = {}
    for edge, page in edge_pages:
        by_edge.setdefault(edge, []).extend(_rows([page]))
    accounts = by_edge.get("account", [])
    if len(accounts) != 1:
        raise _fail("malformed_row", f"{len(accounts)} account rows")
    account = accounts[0]
    if _account_number(account.get("account_id") or account.get("id")) != _account_number(expected.ad_account_id):
        raise _fail("account_mismatch", f"account read of {account.get('id')!r}")
    if account.get("currency") != expected.currency:
        raise _fail("currency_mismatch", f"account in {account.get('currency')!r}, contract in {expected.currency!r}")
    if account.get("timezone_name") != expected.timezone_name:
        raise _fail("timezone_mismatch",
                    f"account in {account.get('timezone_name')!r}, contract in {expected.timezone_name!r}")

    ads = by_edge.get("ads", [])
    for row in ads:
        if "account_id" in row and _account_number(row["account_id"]) != _account_number(expected.ad_account_id):
            raise _fail("account_mismatch", f"ad {row.get('id')!r} of account {row.get('account_id')!r}")
    ad = _unique(ads, "ads", ad_id)
    adset = _unique(by_edge.get("adsets", []), "adsets", _text(ad, "adset_id", "ads"))
    campaign_id = _text(adset, "campaign_id", "adsets")
    if "campaign_id" in ad and ad["campaign_id"] != campaign_id:
        raise _fail("malformed_row", f"ad {ad_id} names campaign {ad['campaign_id']!r}, its ad set {campaign_id!r}")
    campaign = _unique(by_edge.get("campaigns", []), "campaigns", campaign_id)
    creative = ad.get("creative")
    creative_id = creative.get("id") if isinstance(creative, Mapping) else None
    if not isinstance(creative_id, str) or not creative_id:
        raise _fail("malformed_row", f"ad {ad_id} has no creative id")
    budget = _budget(campaign, "campaign") or _budget(adset, "adset")
    if budget is None:
        raise _fail("malformed_row", f"neither ad set {adset.get('id')!r} nor its campaign has a budget")
    learning = adset.get("learning_stage_info")
    stage = learning.get("status") if isinstance(learning, Mapping) else None
    return DeliveryConfigurationObservation(
        ad_account_id=expected.ad_account_id,
        currency=expected.currency,
        timezone_name=expected.timezone_name,
        ad_id=ad_id,
        ad_creative_id=creative_id,
        ad_status=_text(ad, "status", "ads"),
        ad_effective_status=_text(ad, "effective_status", "ads"),
        adset_id=str(adset["id"]),
        adset_status=_text(adset, "status", "adsets"),
        adset_effective_status=_text(adset, "effective_status", "adsets"),
        campaign_id=campaign_id,
        campaign_status=_text(campaign, "status", "campaigns"),
        campaign_effective_status=_text(campaign, "effective_status", "campaigns"),
        budget_source=budget[0],
        budget_amount_minor=budget[1],
        optimization_goal=_text(adset, "optimization_goal", "adsets"),
        bid_strategy=str(campaign.get("bid_strategy") or _text(adset, "bid_strategy", "adsets")),
        attribution_spec_hash=_hash(adset.get("attribution_spec")),
        targeting_safety_hash=_hash(adset.get("targeting")),
        learning_stage=_LEARNING_STAGES.get(stage, "unknown") if isinstance(stage, str) else "unknown",
    )
