# app/scale/platform_policy.py
"""The platform's published ScalePolicyVersions, as code (#127; #63 decision 2).

Contract §10.1: the platform publishes immutable, platform-global
ScalePolicyVersions; changing one means publishing a new version, and old
Evidence and Evaluations are never reinterpreted. Each version here is a
constant covering every column of ``ScalePolicyVersionRecord``, and exactly
one Alembic data migration publishes it. There is no UI and no admin API.

To change the Policy, add a new constant and a new migration that publishes
it. Never edit a published constant: ``tests/test_scale_platform_policy.py``
pins each one's digest, and rows already frozen into snapshots carry its id.

A live run picks the latest published version that permits its mode and runs
the deployed algorithm (``app.scale.live_trigger.eligible_live_policy``,
#101 Q8).

Where each value comes from (the v1 table is on #127, v2's on #132):

* "§n" is ``docs/product/scale-advice-semantics-contract.md``;
* "spec §2.n" is
  ``docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md``;
* "P1".."P15" are the parameters the contract leaves open, confirmed by the
  maintainer on #127 (2026-11-11);
* v2 (#132, #133) changes only what its constant names and keeps every
  other v1 value; its parameters were confirmed on #132 (2026-11-11).

The evaluator reads a Policy only through
``app.scale.evidence._policy_snapshot``: typed columns, plus flat keys of
``signal_thresholds`` for every snapshot field without a column of its own.
Columns marked "not read" are stored with the version but no code reads
them yet.

This module is pure (standard library only), so a migration can import it.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "PlatformPolicyVersion",
    "BETA_DEFAULT_POLICY_V1",
    "BETA_DEFAULT_POLICY_V2",
    "PUBLISHED_POLICY_VERSIONS",
]


def _frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return value


def _thawed(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thawed(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thawed(item) for item in value]
    return value


@dataclass(frozen=True)
class PlatformPolicyVersion:
    """One published ``ScalePolicyVersionRecord`` row, column for column.

    JSON columns are frozen on construction; :meth:`row` returns fresh,
    mutable copies, ready for an INSERT.
    """

    id: str
    algorithm_version: str
    canonical_schema_version: str
    decimal_precision: int
    decimal_rounding: str
    z_value: Decimal
    evidence_floor_minimum_spend: Decimal
    evidence_floor_minimum_days: int
    evidence_floor_minimum_conversions: int
    activity_floor_minimum_impressions: int
    sample_floor_minimum_clicks: int
    spend_floor_minimum_daily_spend: Decimal
    freshness_floor_maximum_lag_hours: int
    signal_thresholds: Mapping[str, Any]
    streak_rules: Mapping[str, Any]
    bootstrap_prng_algorithm: str
    bootstrap_draw_count: int
    bootstrap_valid_draw_ratio: Decimal
    bootstrap_percentile_method: str
    bootstrap_draw_order: str
    breakdown_families: Mapping[str, Any]
    breakdown_sample_gate_minimum: int
    breakdown_share_gate_minimum_ratio: Decimal
    bh_fdr_alpha: Decimal
    advice_severity_thresholds: Mapping[str, Any]
    notification_cooldown_hours: int
    conversion_safe_values: Mapping[str, Any]
    mode_permissions: Mapping[str, Any]
    is_published: bool
    created_at: datetime
    created_by: str
    published_at: datetime | None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Mapping):
                object.__setattr__(self, field.name, _frozen(value))

    def row(self) -> dict[str, Any]:
        """Every column and its value, as the publishing migration inserts it."""
        return {field.name: _thawed(getattr(self, field.name)) for field in fields(self)}


#: The instant the Beta default was published: the migration date, UTC.
_BETA_V1_PUBLISHED_AT = datetime(2026, 11, 11, tzinfo=timezone.utc)

BETA_DEFAULT_POLICY_V1 = PlatformPolicyVersion(
    # #127: a fixed id, so the publishing migration can run again without
    # adding a row.
    id="spol_beta_default_v1",
    # §11: a new run may only use the deployed CURRENT_ALGORITHM_VERSION.
    algorithm_version="beta-v1",
    # P10: the snapshot schema sealed today (app.scale.evidence). Not read.
    canonical_schema_version="scale-snapshot-v3",
    # P3: Python's default Decimal context, which every golden vector uses.
    decimal_precision=28,
    decimal_rounding="round_half_even",
    # P2: the 95% two-sided z. Not read; the evaluator reads
    # signal_thresholds["confidence_z"], which holds the same value.
    z_value=Decimal("1.96"),
    # P5: the same as the platform spend floor (P4). Not read.
    evidence_floor_minimum_spend=Decimal("100"),
    # §9.1: 14 complete account-local days.
    evidence_floor_minimum_days=14,
    # §9.2: at least 5 purchases in each 7-day window.
    evidence_floor_minimum_conversions=5,
    # §9.1: at least 1,000 impressions in each 7-day window.
    activity_floor_minimum_impressions=1000,
    # §9.1: at least 20 link clicks in each 7-day window.
    sample_floor_minimum_clicks=20,
    # P4: the platform economic floor. The evaluator compares it with each
    # 7-day window's total spend, together with the Target's minimum economic
    # spend and 3x target CPA (§9.2). It carries no currency.
    spend_floor_minimum_daily_spend=Decimal("100"),
    # §8: the latest successful sync is at most 36 hours old. Advice
    # expiry (§15.2) reads the same column.
    freshness_floor_maximum_lag_hours=36,
    signal_thresholds={
        # §11: 95% Wilson score intervals and Newcombe method 10.
        "confidence_level": "0.95",
        # P2.
        "confidence_z": "1.96",
        # §12.1: current 7-day exact-window frequency of at least 3.5 ...
        "frequency_level_threshold": "3.5",
        # ... at least 20% above the baseline 7 days.
        "frequency_growth_threshold": "0.2",
        # §12.1: link CTR down at least 25%.
        "ctr_decline_threshold": "0.25",
        # P8: |relative CTR change| within 5% counts as stable CTR (§12.2).
        "ctr_stability_tolerance": "0.05",
        # P6: CVR down at least 20% (§12.2 economics_decline).
        "cvr_decline_threshold": "0.2",
        # P8: |relative CVR change| within 5% counts as stable CVR (§12.2).
        "cvr_stability_tolerance": "0.05",
        # P7: CPM up at least 20% (§12.2 delivery_anomaly).
        "cpm_increase_threshold": "0.2",
        # P7: impressions down at least 20% (§12.2 delivery_anomaly).
        "delivery_decline_threshold": "0.2",
        # §12.1: CPA up at least 20% ...
        "cpa_worsening_threshold": "0.2",
        # ... or ROAS down at least 20%.
        "roas_worsening_threshold": "0.2",
        # Spec §2.4: nearest-rank bounds at 25/1000 and 975/1000.
        "bootstrap_lower_percentile": "0.025",
        "bootstrap_upper_percentile": "0.975",
        # §13: a segment holds at least 15% of the window's spend.
        "breakdown_min_spend_share": "0.15",
        # §9.1: at least 10 days with actual delivery.
        "active_day_floor": 10,
        # §12.1: two consecutive complete-day evaluations confirm fatigue.
        "consecutive_composite_fatigue_hits_required": 2,
        # §14: two consecutive healthy complete-day evaluations.
        "consecutive_healthy_evaluations_required": 2,
        # §13: at least 1,000 impressions, 20 link clicks and 5 purchases
        # per segment.
        "breakdown_min_sample_impressions": 1000,
        "breakdown_min_sample_clicks": 20,
        "breakdown_min_sample_purchases": 5,
        # §8: stale data (freshness_slo_exceeded, the only code this key
        # gates) blocks; no quality warning permits an evaluation.
        "quality_warning_permitted_codes": [],
        # §12.1: a data interruption resets the consecutive counts. The
        # evaluator resets on Policy, Target, contract-set,
        # configuration-baseline and continuity changes whatever this says.
        "streak_reset_reason_codes": ["quality_blocked"],
        # P15. Not read by the evaluator.
        "advice_construction_parameters": [],
    },
    # P12: what the evaluator does today (_reduce_streaks): only a hit
    # extends a streak. Not read.
    streak_rules={"miss": "reset", "unavailable": "reset", "blocked": "reset"},
    # Spec §2.4: SplitMix64 stream bootstrap-prng-v1 ...
    bootstrap_prng_algorithm="bootstrap-prng-v1",
    # ... 10,000 draws ...
    bootstrap_draw_count=10000,
    # P9: below half valid draws, the signal is unavailable.
    bootstrap_valid_draw_ratio=Decimal("0.5"),
    # Spec §2.4: nearest-rank without interpolation. Not read.
    bootstrap_percentile_method="nearest_rank",
    # Spec §2.4: baseline slots, then current slots, in local-date order.
    # Not read.
    bootstrap_draw_order="sequential",
    # #101 Q6: the first batch reads no breakdown family
    # (app.meta.scale_read_contracts.SCALE_READ_POLICY). Not read.
    breakdown_families={},
    # P11: §13's impressions gate. Not read; the three §13 sample gates are
    # in signal_thresholds.
    breakdown_sample_gate_minimum=1000,
    # §13: the 15% spend-share gate, as breakdown_min_spend_share. Not read.
    breakdown_share_gate_minimum_ratio=Decimal("0.15"),
    # §13: Benjamini-Hochberg at a 10% false discovery rate.
    bh_fdr_alpha=Decimal("0.10"),
    # P13. Not read: the evaluator sets severity by Advice type.
    advice_severity_thresholds={},
    # §15.2: a dismissal cools down for 7 days. Not read yet: no
    # notification decision applies it.
    notification_cooldown_hours=168,
    # P14. Not read.
    conversion_safe_values={},
    # P1: customer_sandbox and internal_validation may evaluate live;
    # controlled_mock is not decision-eligible (ADR 0009). Only
    # eligible_live_policy reads this.
    mode_permissions={"customer_sandbox": True, "internal_validation": True},
    is_published=True,
    # #127: fixed, so the constant and the published row match.
    created_at=_BETA_V1_PUBLISHED_AT,
    created_by="platform:migration",
    published_at=_BETA_V1_PUBLISHED_AT,
)

#: v2 is published on the same date as v1, so it takes noon UTC to stay
#: strictly later.
_BETA_V2_PUBLISHED_AT = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)

#: #132: the platform economic floor per currency, as the minimum total spend
#: of each 7-day window, keyed by ISO 4217 code exactly as the Episode
#: (the ad account) reports it. Confirmed on #132 (2026-11-11, table A): v1's
#: 100, read as USD, converted at the ECB euro reference rates of 2026-11-10
#: (USD 1.1214, GBP 0.83545, JPY 177.38, AUD 1.6244, CAD 1.5852,
#: KRW 1501.56) and rounded up to two significant figures, so no entry is
#: below USD 100 at those rates (§9.2: the platform floor is a minimum). The
#: currencies are USD and those of the nine v9 Beta markets
#: (app.localization.platform._BETA_CATALOG). The table does not follow
#: exchange rates; changing it means publishing a new version.
_ECONOMIC_SPEND_FLOOR_PER_7D_WINDOW_BY_CURRENCY = {
    "AUD": "150",
    "CAD": "150",
    "EUR": "90",
    "GBP": "75",
    "JPY": "16000",
    "KRW": "140000",
    "USD": "100",
}

BETA_DEFAULT_POLICY_V2 = replace(
    BETA_DEFAULT_POLICY_V1,
    # #132 and #133 together, so two versions are not published back to back.
    id="spol_beta_default_v2",
    # §11: v2 changes what the evaluator does, so it runs a new registered
    # implementation; beta-v1 stays registered to replay what froze v1.
    algorithm_version="beta-v2",
    # #132: snapshots freeze consecutive_breakdown_hits_required, which
    # changes the snapshot hash payload (app.scale.evidence). Not read.
    canonical_schema_version="scale-snapshot-v4",
    # #132 P-C A: beta-v2 reads neither old floor column; the floor comes
    # from economic_spend_floor_per_7d_window_by_currency below. Both keep
    # v1's 100 (the table's USD entry) only because they are NOT NULL, so a
    # stray reader gets v1's floor rather than none. #134's publish check
    # must read the table, not these columns.
    evidence_floor_minimum_spend=Decimal("100"),
    spend_floor_minimum_daily_spend=Decimal("100"),
    signal_thresholds={
        **_thawed(BETA_DEFAULT_POLICY_V1.signal_thresholds),
        # #132: see _ECONOMIC_SPEND_FLOOR_PER_7D_WINDOW_BY_CURRENCY. A
        # currency outside the table gets no economic evaluation:
        # cpa_worsening and roas_worsening are unavailable with
        # spend_floor_currency_unsupported, and maturity stops at
        # monitorable.
        "economic_spend_floor_per_7d_window_by_currency": _ECONOMIC_SPEND_FLOOR_PER_7D_WINDOW_BY_CURRENCY,
        # #133, §13: a placement or country segment forms review_delivery_mix
        # only on its second consecutive hit.
        "consecutive_breakdown_hits_required": 2,
    },
    created_at=_BETA_V2_PUBLISHED_AT,
    published_at=_BETA_V2_PUBLISHED_AT,
)

#: Every version the migrations publish, oldest first.
PUBLISHED_POLICY_VERSIONS: tuple[PlatformPolicyVersion, ...] = (
    BETA_DEFAULT_POLICY_V1,
    BETA_DEFAULT_POLICY_V2,
)
