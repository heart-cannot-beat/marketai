"""The sandbox account's delivery timeline (#101 S2).

Everything the sandbox adapter answers about an ad is a pure function of
``(ad id, account-local day)``: no clock, no randomness, no write. The
timeline gives every ad, in every run of 14 consecutive local days, exactly:

* one **paused** day -- the ad's status is ``PAUSED`` and it has no insights
  row;
* one **zero-delivery** day -- the ad is ``ACTIVE`` but spent nothing, so it
  has no insights row either (Meta omits rows without delivery);
* twelve **delivering** days, with deterministic metrics.

The two quiet days sit seven days apart at a per-ad phase, so any 14-day
window holds twelve delivery days (contract §9.1 asks for ten) and no ad is
quiet for seven days in a row (§4.3 would close its Episode).

Breakdown segments split a delivering day's metrics. Some segments are
**privacy suppressed** on some days: their row carries the segment but no
metric, which is how the sandbox simulates Meta withholding a small segment
(#101 risk 1: the real shape is confirmed by the S6 canary).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

__all__ = [
    "PAUSED",
    "ZERO_DELIVERY",
    "DELIVERING",
    "PERIOD_DAYS",
    "BREAKDOWN_SEGMENTS",
    "DayValues",
    "day_state",
    "day_values",
    "window_values",
    "segment_values",
    "segment_suppressed",
    "configuration_for_ad",
]

PAUSED = "paused"
ZERO_DELIVERY = "zero_delivery"
DELIVERING = "delivering"
PERIOD_DAYS = 14
_PAUSED_SLOT = 0
_ZERO_DELIVERY_SLOT = 7

#: Segments per breakdown, as the breakdown fields' values.
BREAKDOWN_SEGMENTS: dict[tuple[str, ...], tuple[tuple[str, ...], ...]] = {
    ("publisher_platform", "platform_position"): (
        ("facebook", "feed"), ("instagram", "stream"), ("audience_network", "classic"),
    ),
    ("country",): (("US",), ("CA",), ("GB",)),
    ("age", "gender"): (
        ("18-24", "female"), ("18-24", "male"), ("25-34", "female"), ("25-34", "male"),
        ("35-44", "female"), ("35-44", "male"),
    ),
}


@dataclass(frozen=True)
class DayValues:
    """Primitive metrics, in Meta's units (spend and value in cents)."""

    impressions: int
    clicks: int
    link_clicks: int
    spend_cents: int
    purchases: int
    purchase_value_cents: int


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("|".join(parts).encode("utf-8")).digest()


def _draw(digest: bytes, index: int, modulus: int) -> int:
    return int.from_bytes(digest[index * 4:index * 4 + 4], "big") % modulus


def day_state(ad_id: str, day: date) -> str:
    """``paused``, ``zero_delivery`` or ``delivering`` for one ad on one day."""
    phase = _draw(_digest("sandbox-timeline-phase", ad_id), 0, PERIOD_DAYS)
    slot = (day.toordinal() + phase) % PERIOD_DAYS
    if slot == _PAUSED_SLOT:
        return PAUSED
    if slot == _ZERO_DELIVERY_SLOT:
        return ZERO_DELIVERY
    return DELIVERING


def day_values(ad_id: str, day: date) -> DayValues | None:
    """The day's metrics, or ``None`` when the ad did not deliver."""
    if day_state(ad_id, day) != DELIVERING:
        return None
    digest = _digest("fake-insights", ad_id, day.isoformat())
    impressions = 500 + _draw(digest, 0, 4500)
    clicks = _draw(digest, 1, impressions // 20 + 1)
    link_clicks = _draw(digest, 2, clicks + 1)
    spend_cents = 500 + _draw(digest, 3, 9500)
    purchases = _draw(digest, 4, link_clicks // 5 + 1)
    purchase_value_cents = purchases * (2000 + _draw(digest, 5, 8000))
    return DayValues(impressions, clicks, link_clicks, spend_cents, purchases, purchase_value_cents)


def window_values(ad_id: str, days: list[date]) -> tuple[DayValues, int] | None:
    """The window's summed metrics and its deduplicated reach, or ``None``.

    Reach is drawn for the exact window, never summed from days: it stays at
    or below the window's impressions and at or above its busiest day's.
    """
    delivered = [values for values in (day_values(ad_id, day) for day in days) if values is not None]
    if not delivered:
        return None
    total = DayValues(*(sum(getattr(values, name) for values in delivered) for name in DayValues.__dataclass_fields__))
    digest = _digest("fake-reach", ad_id, days[0].isoformat(), days[-1].isoformat())
    busiest = max(values.impressions for values in delivered)
    reach = busiest + _draw(digest, 0, total.impressions - busiest + 1) * 3 // 5
    return total, min(max(reach, busiest), total.impressions)


def segment_suppressed(ad_id: str, day: date, segment: tuple[str, ...]) -> bool:
    """Whether Meta withholds this segment's numbers for the ad on that day."""
    return _draw(_digest("fake-privacy", ad_id, day.isoformat(), *segment), 0, 5) == 0


def segment_values(ad_id: str, day: date, breakdowns: tuple[str, ...], segment: tuple[str, ...]) -> DayValues | None:
    """One segment's share of a delivering day; ``None`` when the ad did not deliver.

    Each value is the segment's floor share of the day, so link clicks never
    exceed impressions in a segment either.
    """
    values = day_values(ad_id, day)
    if values is None:
        return None
    segments = BREAKDOWN_SEGMENTS[breakdowns]
    weights = [1 + _draw(_digest("fake-segment", ad_id, day.isoformat(), *item), 0, 9) for item in segments]
    weight, total = weights[segments.index(segment)], sum(weights)
    return DayValues(*(getattr(values, name) * weight // total for name in DayValues.__dataclass_fields__))


def configuration_for_ad(ad_id: str, day: date) -> dict[str, str]:
    """Deterministic delivery settings and the ad's status on ``day``."""
    digest = _digest("fake-configuration", ad_id)
    learning = ("SUCCESS", "LEARNING", "FAIL")[_draw(digest, 0, 3)]
    status = "PAUSED" if day_state(ad_id, day) == PAUSED else "ACTIVE"
    return {
        "status": status,
        "daily_budget": str(2000 + 500 * _draw(digest, 1, 17)),
        "learning_stage": learning,
    }
