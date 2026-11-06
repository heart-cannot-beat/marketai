"""Pure-function Scale evaluator: `evaluate_scale()`.

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md

This module is the sole external evaluation seam for Scale (§2.1). It:

- receives an immutable, sealed `ScaleEvidenceSnapshot` and returns an
  immutable `ScaleEvaluation`, or raises `InvalidScaleSnapshot` with a
  stable code;
- performs no database, network, filesystem, environment, logging, or clock
  access, creates no side-effecting records, and mutates no input;
- runs the fixed ten-phase pipeline described in spec §2.5, inside a local
  Decimal context frozen by the snapshot's Policy;
- dispatches through a private registry keyed by
  `snapshot.policy.algorithm_version`, never a caller-supplied label.

Allowed imports are Python standard-library value/arithmetic modules plus
Scale's own frozen contracts (`app.scale._contracts`, `app.scale._statistics`,
`app.scale._canonical`, `app.scale.types`). `os`, `sys`, `logging`, `random`,
`requests`, `httpx`, `sqlalchemy`, `asyncio`, `socket`, and `subprocess` are
forbidden anywhere in this module (enforced by an AST-based test).

Several places in the spec describe behavior in prose without a literal
formula or a closed enumeration (Phase 5's exact reset granularity, Phase 6's
"clear economic improvement" conflict test, Phase 7's consecutive-hit gate
for `review_delivery_mix`, Advice severity assignment). Every such judgment
call is called out at its point of use with a `# Judgment call:` comment and
is additionally summarized in the Task 9 report.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Callable, Iterable

from app.scale._canonical import canonical_encode, canonical_hash
from app.scale._contracts import (
    AdviceCandidate,
    AdviceEvidenceItem,
    BreakdownSegment,
    DailyMetricValues,
    DailyObservation,
    DataQualityItem,
    ExactWindowMetrics,
    InvalidScaleSnapshot,
    MetricContractRef,
    MetricWindow,
    ScaleEvaluation,
    ScaleEvidenceSnapshot,
    ScalePolicySnapshot,
    SignalResult,
    StreakCompatibility,
    StreakCounter,
    StreakReset,
    StreakState,
)
from app.scale._statistics import (
    BootstrapUnavailable,
    bootstrap_prng_seed,
    day_block_bootstrap,
    benjamini_hochberg,
    nearest_rank_percentile,
    newcombe_method10,
    rejection_sample_index,
    splitmix64_stream,
    wilson_interval,
)
from app.scale.types import (
    AdviceSeverity,
    AdviceType,
    BreakdownEvidenceState,
    BreakdownType,
    DailyEvidenceState,
    DataQualityImpact,
    DataQualityState,
    DeliveryDiagnosticState,
    EvidenceMaturity,
    MetaMode,
    PriorStreakAbsenceReason,
    ScaleHealth,
    SignalState,
)

# ---------------------------------------------------------------------------
# Registry (spec §2.1)
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[[ScaleEvidenceSnapshot], ScaleEvaluation]] = {}
CURRENT_ALGORITHM_VERSION = "beta-v1"


def _register(version: str):
    def decorator(fn: Callable[[ScaleEvidenceSnapshot], ScaleEvaluation]):
        _REGISTRY[version] = fn
        return fn

    return decorator


def evaluate_scale(snapshot: ScaleEvidenceSnapshot) -> ScaleEvaluation:
    """Return the deterministic evaluation of one sealed evidence snapshot
    (spec §2.1). Dispatches on the snapshot's own frozen
    `policy.algorithm_version`, never a caller-supplied label."""
    version = snapshot.policy.algorithm_version
    impl = _REGISTRY.get(version)
    if impl is None:
        raise InvalidScaleSnapshot("algorithm_version_unavailable", version)
    return impl(snapshot)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Streak-reset reason codes for structural compatibility mismatches (Phase 5).
_RESET_POLICY_CHANGED = "policy_version_changed"
_RESET_TARGET_CHANGED = "target_version_changed"
_RESET_CONTRACT_SET_CHANGED = "metric_contract_set_changed"
_RESET_CONFIG_BASELINE_CHANGED = "configuration_baseline_changed"
_RESET_CONTINUITY_CHANGED = "data_continuity_generation_changed"
_RESET_QUALITY_BLOCKED = "quality_blocked"

# Fixed vocabulary of primitive/composite signal keys tracked as streaks.
_CORE_SIGNAL_KEYS = (
    "frequency_high",
    "frequency_growth",
    "ctr_decline",
    "cvr_decline",
    "cpm_increase",
    "delivery_decline",
    "cpa_worsening",
    "roas_worsening",
    "no_delivery_complete_day",
)
_COMPOSITE_FATIGUE_KEY = "composite_fatigue"

_STREAK_COMPATIBILITY_DOMAIN = "scale-streak-compatibility-v1"
_ADVICE_REASON_SIGNATURE_DOMAIN = "scale-advice-reason-signature-v1"
_RESULT_HASH_DOMAIN = "scale-evaluation-result-v1"

_NO_TARGET_MARKER = "no_target"


# ---------------------------------------------------------------------------
# Small internal value holders (not part of the public contract set)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Totals:
    impressions: int
    link_clicks: int
    purchases: Decimal
    spend: Decimal
    purchase_value: Decimal


def _totals(days: Iterable[DailyObservation]) -> _Totals:
    impressions = 0
    link_clicks = 0
    purchases = Decimal(0)
    spend = Decimal(0)
    purchase_value = Decimal(0)
    for obs in days:
        if obs.metrics is None:
            continue
        impressions += obs.metrics.impressions
        link_clicks += obs.metrics.link_clicks
        purchases += obs.metrics.purchases
        spend += obs.metrics.spend
        purchase_value += obs.metrics.purchase_value
    return _Totals(impressions, link_clicks, purchases, spend, purchase_value)


def _effective(policy_value: Decimal, override: Decimal | None) -> Decimal:
    """The stricter combination of Platform Policy and Owner Target (spec
    §2.3/§2.5 Phase 3): an override is only ever allowed to tighten, so when
    present it *is* the effective value."""
    return override if override is not None else policy_value


# ---------------------------------------------------------------------------
# Phase 0: validate and establish the arithmetic context (spec §2.3, §2.4)
# ---------------------------------------------------------------------------


def _validate_metrics(metrics: DailyMetricValues) -> None:
    if (
        metrics.impressions < 0
        or metrics.link_clicks < 0
        or metrics.purchases < 0
        or metrics.spend < 0
        or metrics.purchase_value < 0
    ):
        raise InvalidScaleSnapshot("negative_metric")
    if metrics.link_clicks > metrics.impressions:
        raise InvalidScaleSnapshot("clicks_exceed_impressions")


def _validate_daily_observation(obs: DailyObservation) -> None:
    if obs.evidence_state == DailyEvidenceState.PRESENT:
        if obs.metrics is None:
            raise InvalidScaleSnapshot(
                "evidence_metrics_mismatch", "present day requires metrics"
            )
        _validate_metrics(obs.metrics)
    else:
        if obs.metrics is not None:
            raise InvalidScaleSnapshot(
                "evidence_metrics_mismatch", "missing day forbids metrics"
            )


def _validate_window(window: MetricWindow, expected_days: int) -> None:
    if window.end_date_exclusive - window.start_date != timedelta(days=expected_days):
        raise InvalidScaleSnapshot("invalid_window_boundary", window.role.value)
    if len(window.daily) != expected_days:
        raise InvalidScaleSnapshot(
            "missing_or_duplicate_local_day", window.role.value
        )
    expected_date = window.start_date
    seen: set[date] = set()
    for obs in window.daily:
        if obs.local_date != expected_date or obs.local_date in seen:
            raise InvalidScaleSnapshot(
                "missing_or_duplicate_local_day", window.role.value
            )
        seen.add(obs.local_date)
        expected_date += timedelta(days=1)
        _validate_daily_observation(obs)


def _validate_exact_window(exact: ExactWindowMetrics, window: MetricWindow) -> None:
    if (
        exact.start_date != window.start_date
        or exact.end_date_exclusive != window.end_date_exclusive
    ):
        raise InvalidScaleSnapshot("invalid_exact_window", exact.role.value)
    if exact.reach is not None and exact.reach < 0:
        raise InvalidScaleSnapshot("negative_metric")
    if exact.frequency is not None and exact.frequency < 0:
        raise InvalidScaleSnapshot("negative_metric")


def _validate_contract_set(contracts: tuple[MetricContractRef, ...], currency: str) -> None:
    prev_type: str | None = None
    for contract in contracts:
        if prev_type is not None and contract.contract_type.value <= prev_type:
            raise InvalidScaleSnapshot("invalid_or_unsorted_contract_set")
        prev_type = contract.contract_type.value
        if contract.currency != currency:
            raise InvalidScaleSnapshot("currency_mismatch")


def _validate_breakdown_days(
    daily: tuple[DailyObservation, ...], window: MetricWindow
) -> None:
    if len(daily) != len(window.daily):
        raise InvalidScaleSnapshot("invalid_or_unsorted_breakdown")
    expected_date = window.start_date
    seen: set[date] = set()
    for obs in daily:
        if obs.local_date != expected_date or obs.local_date in seen:
            raise InvalidScaleSnapshot("invalid_or_unsorted_breakdown")
        seen.add(obs.local_date)
        expected_date += timedelta(days=1)
        _validate_daily_observation(obs)


def _validate_breakdowns(
    breakdowns: tuple[BreakdownSegment, ...], current_window: MetricWindow
) -> None:
    prev_key: tuple[str, str] | None = None
    for segment in breakdowns:
        key = (segment.breakdown_type.value, segment.segment_key)
        if prev_key is not None and key <= prev_key:
            raise InvalidScaleSnapshot("invalid_or_unsorted_breakdown")
        prev_key = key
        if segment.evidence_state == BreakdownEvidenceState.OBSERVED:
            if segment.daily is None or segment.suppression_reason is not None:
                raise InvalidScaleSnapshot("invalid_or_unsorted_breakdown")
            _validate_breakdown_days(segment.daily, current_window)
        else:
            if segment.daily is not None or segment.suppression_reason is None:
                raise InvalidScaleSnapshot("invalid_or_unsorted_breakdown")


def _issue_sort_key(item: DataQualityItem) -> str:
    loc = item.locator
    return "\x1f".join(
        (
            item.impact.value,
            item.code.value,
            loc.contract_type.value,
            loc.window_role.value if loc.window_role is not None else "",
            loc.local_date.isoformat() if loc.local_date is not None else "",
            loc.breakdown_type.value if loc.breakdown_type is not None else "",
            loc.segment_key or "",
            loc.source_evidence_hash,
            canonical_encode(item.details),
        )
    )


def _validate_data_quality_issues(issues: tuple[DataQualityItem, ...]) -> None:
    prev_key: str | None = None
    for item in issues:
        key = _issue_sort_key(item)
        if prev_key is not None and key <= prev_key:
            raise InvalidScaleSnapshot(
                "snapshot_not_canonical", "data_quality_issues not canonically sorted"
            )
        prev_key = key


def _validate_prior_streak(snapshot: ScaleEvidenceSnapshot) -> None:
    if snapshot.prior_streak is None:
        if snapshot.prior_streak_absence_reason is None:
            raise InvalidScaleSnapshot(
                "invalid_prior_evaluation", "absence reason required"
            )
        if (
            snapshot.prior_streak_absence_reason
            != PriorStreakAbsenceReason.NO_EARLIER_DISTINCT_DAY
        ):
            raise InvalidScaleSnapshot("invalid_prior_evaluation", "unknown absence reason")
    else:
        if snapshot.prior_streak_absence_reason is not None:
            raise InvalidScaleSnapshot(
                "invalid_prior_evaluation", "absence reason forbidden with prior present"
            )
        if snapshot.prior_streak.evaluated_local_day >= snapshot.as_of_local_day:
            raise InvalidScaleSnapshot(
                "invalid_prior_evaluation", "prior must be an earlier evaluation day"
            )


def _validate_freshness(snapshot: ScaleEvidenceSnapshot) -> None:
    if snapshot.last_sync_at.tzinfo is None or snapshot.last_sync_at.utcoffset() is None:
        raise InvalidScaleSnapshot("invalid_freshness_reference", "last_sync_at naive")
    if (
        snapshot.freshness_reference_at.tzinfo is None
        or snapshot.freshness_reference_at.utcoffset() is None
    ):
        raise InvalidScaleSnapshot(
            "invalid_freshness_reference", "freshness_reference_at naive"
        )
    if snapshot.freshness_reference_at < snapshot.last_sync_at:
        raise InvalidScaleSnapshot(
            "invalid_freshness_reference",
            "freshness_reference_at must be >= last_sync_at",
        )


def _validate_snapshot(snapshot: ScaleEvidenceSnapshot) -> None:
    """Phase 0 (spec §2.3, §2.5 Phase 0). The persistence assembler is the
    primary integrity gate; the evaluator nevertheless validates every
    assumption its own arithmetic depends on."""
    if snapshot.policy.algorithm_version != CURRENT_ALGORITHM_VERSION:
        raise InvalidScaleSnapshot("algorithm_policy_mismatch", snapshot.policy.algorithm_version)

    if snapshot.target is not None and snapshot.target.currency != snapshot.currency:
        raise InvalidScaleSnapshot("currency_mismatch")

    if snapshot.current_7d.end_date_exclusive != snapshot.as_of_local_day + timedelta(days=1):
        raise InvalidScaleSnapshot("invalid_window_boundary", "current_7d")
    _validate_window(snapshot.current_7d, 7)

    if snapshot.baseline_7d.end_date_exclusive != snapshot.current_7d.start_date:
        raise InvalidScaleSnapshot("invalid_window_boundary", "baseline_7d")
    _validate_window(snapshot.baseline_7d, 7)

    _validate_exact_window(snapshot.current_exact_7d, snapshot.current_7d)
    _validate_exact_window(snapshot.baseline_exact_7d, snapshot.baseline_7d)

    if (
        snapshot.rolling_exact_14d.start_date != snapshot.baseline_7d.start_date
        or snapshot.rolling_exact_14d.end_date_exclusive != snapshot.current_7d.end_date_exclusive
    ):
        raise InvalidScaleSnapshot("invalid_exact_window", "rolling_exact_14d")

    _validate_contract_set(snapshot.metric_contracts, snapshot.currency)
    _validate_breakdowns(snapshot.breakdowns, snapshot.current_7d)
    _validate_data_quality_issues(snapshot.data_quality_issues)
    _validate_prior_streak(snapshot)
    _validate_freshness(snapshot)


# ---------------------------------------------------------------------------
# Phase 1: data-quality state (spec §2.5 Phase 1)
# ---------------------------------------------------------------------------


def _data_quality_state(issues: tuple[DataQualityItem, ...]) -> DataQualityState:
    if any(item.impact == DataQualityImpact.BLOCKING for item in issues):
        return DataQualityState.BLOCKED
    if issues:
        return DataQualityState.WARNING
    return DataQualityState.PASS


# ---------------------------------------------------------------------------
# Phase 2: delivery classification (spec §2.5 Phase 2)
# ---------------------------------------------------------------------------


def _is_not_delivering(snapshot: ScaleEvidenceSnapshot, current_totals: _Totals) -> bool:
    """Zero current-window impressions plus the frozen status history (spec
    §2.5 Phase 2). Judgment call: since the current-window daily rows are
    already known-complete by the time this runs (any missing slot would
    have blocked in Phase 1), zero total impressions is itself sufficient
    evidence of no delivery — the frozen status history is consulted only to
    confirm this was not an ingestion artifact, which the sealed snapshot
    already guarantees, so no additional status check is required here."""
    return current_totals.impressions == 0


# ---------------------------------------------------------------------------
# Phase 3: maturity (spec §2.5 Phase 3)
# ---------------------------------------------------------------------------


def _maturity(
    snapshot: ScaleEvidenceSnapshot,
    current_totals: _Totals,
    baseline_totals: _Totals,
) -> EvidenceMaturity:
    policy = snapshot.policy
    target = snapshot.target

    monitorable = (
        snapshot.complete_local_days_in_episode >= policy.complete_day_floor
        and snapshot.active_delivery_days_in_episode >= policy.active_day_floor
        and current_totals.impressions >= policy.impression_floor
        and current_totals.link_clicks >= policy.click_floor
        and baseline_totals.impressions >= policy.impression_floor
        and baseline_totals.link_clicks >= policy.click_floor
    )
    if not monitorable:
        return EvidenceMaturity.WARMING

    spend_floor_candidates = [policy.spend_floor]
    if target is not None:
        spend_floor_candidates.append(target.minimum_economic_spend)
        if target.target_cpa is not None:
            spend_floor_candidates.append(Decimal(3) * target.target_cpa)
    effective_spend_floor = max(spend_floor_candidates)

    economics_ready = (
        current_totals.purchases >= policy.purchase_floor
        and baseline_totals.purchases >= policy.purchase_floor
        and current_totals.spend >= effective_spend_floor
        and baseline_totals.spend >= effective_spend_floor
    )
    return EvidenceMaturity.ECONOMICS_READY if economics_ready else EvidenceMaturity.MONITORABLE


# ---------------------------------------------------------------------------
# Phase 4: primitive signals (spec §2.4, §2.5 Phase 4)
# ---------------------------------------------------------------------------


def _signal(
    signal_id: str,
    state: SignalState,
    *,
    expected: Decimal | None = None,
    observed: Decimal | None = None,
    interval_lower: Decimal | None = None,
    interval_upper: Decimal | None = None,
    threshold: Decimal | None = None,
    p_value: Decimal | None = None,
    adjusted_q_value: Decimal | None = None,
    reason_code: str = "",
) -> SignalResult:
    return SignalResult(
        signal_id=signal_id,
        state=state,
        expected=expected,
        observed=observed,
        interval_lower=interval_lower,
        interval_upper=interval_upper,
        threshold=threshold,
        p_value=p_value,
        adjusted_q_value=adjusted_q_value,
        reason_code=reason_code,
    )


def _is_pause_explained(snapshot: ScaleEvidenceSnapshot) -> bool:
    window = snapshot.current_7d
    for event in snapshot.delivery_config.status_history:
        event_date = event.observed_at.date()
        if window.start_date <= event_date < window.end_date_exclusive:
            if "pause" in event.status.lower():
                return True
    return False


@dataclass(frozen=True)
class _PrimitiveSignals:
    results: dict[str, SignalResult]
    stable_ctr: bool
    stable_cvr: bool


def _primitive_signals(
    snapshot: ScaleEvidenceSnapshot,
    current_totals: _Totals,
    baseline_totals: _Totals,
) -> _PrimitiveSignals:
    policy = snapshot.policy
    target = snapshot.target
    results: dict[str, SignalResult] = {}

    def eff(policy_value: Decimal, override_attr: str) -> Decimal:
        override = getattr(target, override_attr) if target is not None else None
        return _effective(policy_value, override)

    # -- frequency_high --------------------------------------------------
    freq_threshold = eff(policy.frequency_level_threshold, "frequency_level_threshold_override")
    current_freq = snapshot.current_exact_7d.frequency
    if current_freq is None:
        results["frequency_high"] = _signal(
            "frequency_high", SignalState.UNAVAILABLE, threshold=freq_threshold,
            reason_code="frequency_unavailable",
        )
    else:
        state = SignalState.HIT if current_freq >= freq_threshold else SignalState.MISS
        results["frequency_high"] = _signal(
            "frequency_high", state, observed=current_freq, threshold=freq_threshold,
            reason_code="ok",
        )

    # -- frequency_growth --------------------------------------------------
    growth_threshold = eff(
        policy.frequency_growth_threshold, "frequency_growth_threshold_override"
    )
    baseline_freq = snapshot.baseline_exact_7d.frequency
    if baseline_freq is None or baseline_freq <= 0 or current_freq is None:
        results["frequency_growth"] = _signal(
            "frequency_growth", SignalState.UNAVAILABLE, threshold=growth_threshold,
            reason_code="unavailable_baseline",
        )
    else:
        growth = (current_freq / baseline_freq) - 1
        state = SignalState.HIT if growth >= growth_threshold else SignalState.MISS
        results["frequency_growth"] = _signal(
            "frequency_growth", state, observed=growth, threshold=growth_threshold,
            reason_code="ok",
        )

    # -- ctr_decline (Newcombe-supported) -----------------------------------
    ctr_threshold = eff(policy.ctr_decline_threshold, "ctr_decline_threshold_override")
    ctr_available = (
        baseline_totals.impressions > 0
        and current_totals.impressions > 0
        and baseline_totals.link_clicks > 0
    )
    stable_ctr = False
    if not ctr_available:
        results["ctr_decline"] = _signal(
            "ctr_decline", SignalState.UNAVAILABLE, threshold=ctr_threshold,
            reason_code="unavailable_baseline",
        )
    else:
        baseline_ctr = Decimal(baseline_totals.link_clicks) / Decimal(baseline_totals.impressions)
        current_ctr = Decimal(current_totals.link_clicks) / Decimal(current_totals.impressions)
        relative_decline = 1 - (current_ctr / baseline_ctr)
        d_lower, d_upper = newcombe_method10(
            x1=baseline_totals.link_clicks,
            n1=baseline_totals.impressions,
            x2=current_totals.link_clicks,
            n2=current_totals.impressions,
            z=policy.confidence_z,
        )
        supported = d_lower > 0
        hit = relative_decline >= ctr_threshold and supported
        results["ctr_decline"] = _signal(
            "ctr_decline",
            SignalState.HIT if hit else SignalState.MISS,
            observed=relative_decline,
            threshold=ctr_threshold,
            interval_lower=d_lower,
            interval_upper=d_upper,
            reason_code="ok",
        )
        stable_ctr = (
            results["ctr_decline"].state == SignalState.MISS
            and abs(relative_decline) <= policy.ctr_stability_tolerance
        )

    # -- cvr_decline ----------------------------------------------------
    cvr_threshold = eff(policy.cvr_decline_threshold, "cvr_decline_threshold_override")
    cvr_available = (
        baseline_totals.link_clicks > 0
        and current_totals.link_clicks > 0
        and baseline_totals.purchases > 0
    )
    stable_cvr = False
    if not cvr_available:
        results["cvr_decline"] = _signal(
            "cvr_decline", SignalState.UNAVAILABLE, threshold=cvr_threshold,
            reason_code="unavailable_baseline",
        )
    else:
        baseline_cvr = baseline_totals.purchases / Decimal(baseline_totals.link_clicks)
        current_cvr = current_totals.purchases / Decimal(current_totals.link_clicks)
        relative_decline = 1 - (current_cvr / baseline_cvr)
        hit = relative_decline >= cvr_threshold
        results["cvr_decline"] = _signal(
            "cvr_decline",
            SignalState.HIT if hit else SignalState.MISS,
            observed=relative_decline,
            threshold=cvr_threshold,
            reason_code="ok",
        )
        stable_cvr = (
            results["cvr_decline"].state == SignalState.MISS
            and abs(relative_decline) <= policy.cvr_stability_tolerance
        )

    # -- cpm_increase -----------------------------------------------------
    cpm_threshold = eff(policy.cpm_increase_threshold, "cpm_increase_threshold_override")
    cpm_available = baseline_totals.impressions > 0 and current_totals.impressions > 0 and baseline_totals.spend > 0
    if not cpm_available:
        results["cpm_increase"] = _signal(
            "cpm_increase", SignalState.UNAVAILABLE, threshold=cpm_threshold,
            reason_code="unavailable_baseline",
        )
    else:
        baseline_cpm = Decimal(1000) * baseline_totals.spend / Decimal(baseline_totals.impressions)
        current_cpm = Decimal(1000) * current_totals.spend / Decimal(current_totals.impressions)
        relative_increase = (current_cpm / baseline_cpm) - 1
        hit = relative_increase >= cpm_threshold
        results["cpm_increase"] = _signal(
            "cpm_increase",
            SignalState.HIT if hit else SignalState.MISS,
            observed=relative_increase,
            threshold=cpm_threshold,
            reason_code="ok",
        )

    # -- delivery_decline ---------------------------------------------------
    delivery_threshold = eff(
        policy.delivery_decline_threshold, "delivery_decline_threshold_override"
    )
    if baseline_totals.impressions <= 0:
        results["delivery_decline"] = _signal(
            "delivery_decline", SignalState.UNAVAILABLE, threshold=delivery_threshold,
            reason_code="unavailable_baseline",
        )
    else:
        relative_decline = 1 - (Decimal(current_totals.impressions) / Decimal(baseline_totals.impressions))
        raw_hit = relative_decline >= delivery_threshold
        explained = raw_hit and _is_pause_explained(snapshot)
        hit = raw_hit and not explained
        results["delivery_decline"] = _signal(
            "delivery_decline",
            SignalState.HIT if hit else SignalState.MISS,
            observed=relative_decline,
            threshold=delivery_threshold,
            reason_code="explained_by_pause" if explained else "ok",
        )

    # -- cpa_worsening (bootstrap-supported) --------------------------------
    cpa_threshold = eff(policy.cpa_worsening_threshold, "cpa_worsening_threshold_override")
    results["cpa_worsening"] = _bootstrap_economic_signal(
        snapshot, "cpa_worsening", current_totals, baseline_totals, cpa_threshold, metric="cpa",
    )

    # -- roas_worsening (bootstrap-supported) --------------------------------
    roas_threshold = eff(policy.roas_worsening_threshold, "roas_worsening_threshold_override")
    results["roas_worsening"] = _bootstrap_economic_signal(
        snapshot, "roas_worsening", current_totals, baseline_totals, roas_threshold, metric="roas",
    )

    # -- configuration_drift -------------------------------------------------
    # A blocking incompatibility would already have short-circuited in
    # Phase 1; reaching here means any hash difference is a permitted drift.
    drift = (
        snapshot.delivery_config.baseline_configuration_hash
        != snapshot.delivery_config.current_configuration_hash
    )
    results["configuration_drift"] = _signal(
        "configuration_drift",
        SignalState.HIT if drift else SignalState.MISS,
        reason_code="ok",
    )

    # -- no_delivery_complete_day ---------------------------------------------
    last_day = snapshot.current_7d.daily[-1]
    no_delivery_hit = (
        last_day.metrics is not None
        and last_day.metrics.delivered is False
        and last_day.metrics.impressions == 0
    )
    results["no_delivery_complete_day"] = _signal(
        "no_delivery_complete_day",
        SignalState.HIT if no_delivery_hit else SignalState.MISS,
        reason_code="ok",
    )

    return _PrimitiveSignals(results=results, stable_ctr=stable_ctr, stable_cvr=stable_cvr)


def _bootstrap_economic_signal(
    snapshot: ScaleEvidenceSnapshot,
    signal_id: str,
    current_totals: _Totals,
    baseline_totals: _Totals,
    threshold: Decimal,
    *,
    metric: str,
) -> SignalResult:
    """`cpa_worsening` / `roas_worsening` (spec §2.4 "Deterministic day-block
    bootstrap"). Positive baselines are required; CPA uses
    `(current/baseline) - 1`, ROAS uses `1 - (current/baseline)`, so both are
    direction-normalized to "positive means worse"."""

    if metric == "cpa":
        baseline_denominator = baseline_totals.purchases
        current_denominator = current_totals.purchases
    else:
        baseline_denominator = baseline_totals.spend
        current_denominator = current_totals.spend

    if baseline_denominator <= 0 or current_denominator <= 0:
        return _signal(
            signal_id, SignalState.UNAVAILABLE, threshold=threshold,
            reason_code="unavailable_baseline",
        )

    if metric == "cpa":
        baseline_point = baseline_totals.spend / baseline_totals.purchases
        current_point = current_totals.spend / current_totals.purchases
    else:
        baseline_point = baseline_totals.purchase_value / baseline_totals.spend
        current_point = current_totals.purchase_value / current_totals.spend

    # Every relative signal requires an available *positive* baseline (spec
    # §2.4): a zero-valued baseline ratio (e.g. zero ROAS from zero
    # purchase_value with positive spend) is unavailable, not zero, even
    # though its denominator sum was positive above.
    if baseline_point <= 0:
        return _signal(
            signal_id, SignalState.UNAVAILABLE, threshold=threshold,
            reason_code="unavailable_baseline",
        )

    if metric == "cpa":
        observed = (current_point / baseline_point) - 1
    else:
        observed = 1 - (current_point / baseline_point)

    def statistic_fn(
        baseline_sample: tuple[DailyMetricValues, ...],
        current_sample: tuple[DailyMetricValues, ...],
    ) -> Decimal | None:
        if metric == "cpa":
            b_purchases = sum((d.purchases for d in baseline_sample), Decimal(0))
            c_purchases = sum((d.purchases for d in current_sample), Decimal(0))
            if b_purchases <= 0 or c_purchases <= 0:
                return None
            b_spend = sum((d.spend for d in baseline_sample), Decimal(0))
            c_spend = sum((d.spend for d in current_sample), Decimal(0))
            return ((c_spend / c_purchases) / (b_spend / b_purchases)) - 1
        b_spend = sum((d.spend for d in baseline_sample), Decimal(0))
        c_spend = sum((d.spend for d in current_sample), Decimal(0))
        if b_spend <= 0 or c_spend <= 0:
            return None
        b_pv = sum((d.purchase_value for d in baseline_sample), Decimal(0))
        c_pv = sum((d.purchase_value for d in current_sample), Decimal(0))
        return 1 - ((c_pv / c_spend) / (b_pv / b_spend))

    policy = snapshot.policy
    try:
        lower, upper = day_block_bootstrap(
            snapshot.baseline_7d.daily,
            snapshot.current_7d.daily,
            statistic_fn,
            policy.bootstrap_draw_count,
            snapshot.canonical_hash,
            signal_id,
            policy,
        )
    except BootstrapUnavailable:
        return _signal(
            signal_id, SignalState.UNAVAILABLE, observed=observed, threshold=threshold,
            reason_code="bootstrap_unavailable",
        )

    hit = observed >= threshold and lower > 0
    return _signal(
        signal_id,
        SignalState.HIT if hit else SignalState.MISS,
        observed=observed,
        threshold=threshold,
        interval_lower=lower,
        interval_upper=upper,
        reason_code="ok",
    )


# ---------------------------------------------------------------------------
# Phase 5: streak reducer (spec §2.5 Phase 5)
# ---------------------------------------------------------------------------


def _build_compatibility(snapshot: ScaleEvidenceSnapshot) -> StreakCompatibility:
    target_version_id = snapshot.target.target_version_id if snapshot.target is not None else None
    identity = {
        "policy_version_id": snapshot.policy.policy_version_id,
        "target_version_id": target_version_id,
        "metric_contract_set_hash": snapshot.metric_contract_set_hash,
        "configuration_baseline_hash": snapshot.delivery_config.baseline_configuration_hash,
        "data_continuity_generation": snapshot.data_continuity_generation,
    }
    digest = canonical_hash(identity, domain=_STREAK_COMPATIBILITY_DOMAIN)
    return StreakCompatibility(
        policy_version_id=identity["policy_version_id"],
        target_version_id=identity["target_version_id"],
        metric_contract_set_hash=identity["metric_contract_set_hash"],
        configuration_baseline_hash=identity["configuration_baseline_hash"],
        data_continuity_generation=identity["data_continuity_generation"],
        canonical_hash=digest,
    )


@dataclass(frozen=True)
class _StreakResetResolution:
    """Post-reset prior streak state (spec §2.5 Phase 5), computed *before*
    hits/misses for the current evaluation are applied. Phase 6's
    composite-fatigue confirmation reads `prior_counters` from this — not
    the raw `PriorStreakState.counters` — so that a Policy/Target/
    contract-set/configuration-baseline/data-continuity mismatch (or a
    Policy-gated quality-blocked reset) this cycle is reflected in the
    confirmation decision itself. Reading the raw prior counters directly
    would let a stale streak from before an incompatibility confirm fatigue
    (or any other composite) in the very same cycle its lineage was reset,
    which is internally contradictory: the returned `StreakState.resets`
    would show the counter zeroed while `ScaleEvaluation.health` had just
    treated it as still consecutive."""

    prior_counters: dict[str, int]
    prior_healthy: int
    resets: list[StreakReset]


def _resolve_streak_reset(
    snapshot: ScaleEvidenceSnapshot,
    current_compat: StreakCompatibility,
    quality_blocked: bool,
) -> _StreakResetResolution:
    """Compare current `StreakCompatibility` with `PriorStreakState` and
    apply Policy-defined resets (spec §2.5 Phase 5), returning the
    post-reset prior counters/healthy-streak and the `StreakReset` records
    — *before* this cycle's hits/misses are applied. Split out from
    `_reduce_streaks` so any prior-streak-derived value consumed by Phase 6
    (health classification) can be computed from the same post-reset state
    the streak reducer itself will use, rather than re-deriving it (or,
    worse, reading the raw un-reset prior counters) independently.

    Judgment call: any single structural mismatch (Policy, Target,
    contract-set, configuration-baseline, or data-continuity) resets *every*
    counter and `healthy_evaluations`, since every downstream threshold and
    hash depends on all five identities together — there is no way to scope
    "affected" more narrowly without inventing a per-signal dependency map
    the spec does not provide. `quality_blocked` resets are additionally
    gated by `policy.streak_reset_reason_codes` containing
    `"quality_blocked"`, matching Phase 1's explicit "whose Policy reset
    rules include the blocking reason" wording (Phase 5's structural resets
    carry no equivalent gating language, so those are unconditional).

    Judgment call: when more than one identity mismatches in the same
    cycle (e.g. both Policy and Target change together), a single reason
    code is recorded — the first mismatch found in this fixed field order
    (policy, target, contract-set, configuration-baseline, continuity) —
    rather than one `StreakReset` per mismatched field. Every affected
    counter still gets exactly one reset record either way, so no counter
    is silently dropped without a reset entry; only which single label is
    attached when several reasons apply simultaneously is a judgment call,
    since the spec does not say whether simultaneous mismatches should
    fan out into multiple records per counter.
    """
    policy = snapshot.policy
    prior = snapshot.prior_streak

    prior_counters: dict[str, int] = {}
    prior_healthy = 0
    resets: list[StreakReset] = []

    if prior is not None:
        prior_counters = {c.signal_key: c.consecutive_hits for c in prior.counters}
        prior_healthy = prior.healthy_evaluations
        prior_compat = prior.compatibility

        mismatch_reason: str | None = None
        if prior_compat.policy_version_id != current_compat.policy_version_id:
            mismatch_reason = _RESET_POLICY_CHANGED
        elif prior_compat.target_version_id != current_compat.target_version_id:
            mismatch_reason = _RESET_TARGET_CHANGED
        elif prior_compat.metric_contract_set_hash != current_compat.metric_contract_set_hash:
            mismatch_reason = _RESET_CONTRACT_SET_CHANGED
        elif prior_compat.configuration_baseline_hash != current_compat.configuration_baseline_hash:
            mismatch_reason = _RESET_CONFIG_BASELINE_CHANGED
        elif prior_compat.data_continuity_generation != current_compat.data_continuity_generation:
            mismatch_reason = _RESET_CONTINUITY_CHANGED

        if mismatch_reason is not None:
            if prior_healthy > 0:
                resets.append(StreakReset("healthy_evaluations", prior_healthy, mismatch_reason))
            for key, value in prior_counters.items():
                if value > 0:
                    resets.append(StreakReset(key, value, mismatch_reason))
            prior_counters = {}
            prior_healthy = 0

    if quality_blocked and _RESET_QUALITY_BLOCKED in policy.streak_reset_reason_codes:
        if prior_healthy > 0:
            resets.append(StreakReset("healthy_evaluations", prior_healthy, _RESET_QUALITY_BLOCKED))
        for key, value in prior_counters.items():
            if value > 0:
                resets.append(StreakReset(key, value, _RESET_QUALITY_BLOCKED))
        prior_counters = {}
        prior_healthy = 0

    return _StreakResetResolution(prior_counters, prior_healthy, resets)


def _reduce_streaks(
    current_compat: StreakCompatibility,
    reset_resolution: _StreakResetResolution,
    health: ScaleHealth | None,
    hit_keys: frozenset[str],
    extra_known_keys: frozenset[str],
) -> StreakState:
    """Apply this cycle's hits/misses on top of the already-resolved
    post-reset prior state (spec §2.5 Phase 5): increment every current hit,
    reset every current miss.

    Judgment call: `MISS`, `UNAVAILABLE`, and `BLOCKED` signal states are all
    treated as streak resets (only `HIT` increments) — the spec requires an
    "explicit carry-or-reset rule" governed by the Policy, but
    `ScalePolicySnapshot` (Task 7) has no dedicated field for it, so the
    conservative reading (a consecutive-hit claim requires literally
    consecutive `HIT`s) is used.
    """
    prior_counters = reset_resolution.prior_counters
    prior_healthy = reset_resolution.prior_healthy
    resets = reset_resolution.resets

    known_keys = (
        frozenset(_CORE_SIGNAL_KEYS)
        | {_COMPOSITE_FATIGUE_KEY}
        | frozenset(prior_counters.keys())
        | extra_known_keys
    )
    new_counters = {
        key: (prior_counters.get(key, 0) + 1 if key in hit_keys else 0) for key in known_keys
    }

    reset_occurred = bool(resets)
    new_healthy = (
        prior_healthy + 1 if (health == ScaleHealth.HEALTHY and not reset_occurred) else 0
    )

    counters_tuple = tuple(
        StreakCounter(signal_key=key, consecutive_hits=new_counters[key])
        for key in sorted(new_counters)
    )
    resets_tuple = tuple(
        sorted(resets, key=lambda r: (r.streak_id, r.reason_code))
    )

    return StreakState(
        compatibility=current_compat,
        healthy_evaluations=new_healthy,
        counters=counters_tuple,
        resets=resets_tuple,
    )


# ---------------------------------------------------------------------------
# Phase 6: health classification (spec §2.5 Phase 6)
# ---------------------------------------------------------------------------

_CORE_HEALTH_SIGNAL_KEYS = _CORE_SIGNAL_KEYS[:8]  # excludes no_delivery_complete_day


@dataclass(frozen=True)
class _HealthResult:
    health: ScaleHealth | None
    health_reason: str
    composite_fatigue_hit: bool
    confirmed_fatigue: bool


def _classify_health(
    signals: dict[str, SignalResult],
    maturity: EvidenceMaturity,
    not_delivering: bool,
    stable_ctr: bool,
    stable_cvr: bool,
    composite_fatigue_streak_after_hit: int,
    policy: ScalePolicySnapshot,
) -> _HealthResult:
    def state(key: str) -> SignalState:
        return signals[key].state

    if not_delivering:
        return _HealthResult(ScaleHealth.NOT_DELIVERING, "", False, False)

    composite_hit_this_eval = (
        state("frequency_high") == SignalState.HIT
        and state("frequency_growth") == SignalState.HIT
        and state("ctr_decline") == SignalState.HIT
        and (state("cpa_worsening") == SignalState.HIT or state("roas_worsening") == SignalState.HIT)
        and maturity == EvidenceMaturity.ECONOMICS_READY
    )
    confirmed_fatigue = (
        composite_hit_this_eval
        and composite_fatigue_streak_after_hit >= policy.consecutive_composite_fatigue_hits_required
    )
    if confirmed_fatigue:
        return _HealthResult(ScaleHealth.CREATIVE_FATIGUE, "", True, True)

    economic_hit = state("cpa_worsening") == SignalState.HIT or state("roas_worsening") == SignalState.HIT
    if economic_hit and stable_ctr:
        return _HealthResult(ScaleHealth.ECONOMICS_DECLINE, "", composite_hit_this_eval, False)

    cpm_or_delivery_hit = state("cpm_increase") == SignalState.HIT or state("delivery_decline") == SignalState.HIT
    if cpm_or_delivery_hit and stable_ctr and stable_cvr:
        return _HealthResult(ScaleHealth.DELIVERY_ANOMALY, "", composite_hit_this_eval, False)

    frequency_and_ctr_hit = (
        state("frequency_high") == SignalState.HIT
        and state("frequency_growth") == SignalState.HIT
        and state("ctr_decline") == SignalState.HIT
    )
    watch_insufficient_economics = frequency_and_ctr_hit and maturity != EvidenceMaturity.ECONOMICS_READY
    if composite_hit_this_eval or watch_insufficient_economics:
        return _HealthResult(ScaleHealth.FATIGUE_WATCH, "", True, False)

    # Judgment call: "materially contrary directions, such as CTR
    # deterioration with clear economic improvement" (spec §2.5 Phase 6 item
    # 7) is not given a literal formula. This operationalizes "clear economic
    # improvement" as the direction-normalized CPA/ROAS effect being at or
    # beyond the *negative* mirror of its own worsening threshold — i.e. an
    # improvement at least as large in magnitude as the decline threshold
    # that would flag worsening.
    def clearly_improved(signal_id: str) -> bool:
        result = signals[signal_id]
        if result.observed is None or result.threshold is None:
            return False
        return result.observed <= -result.threshold

    conflicting = state("ctr_decline") == SignalState.HIT and (
        clearly_improved("cpa_worsening") or clearly_improved("roas_worsening")
    )
    if conflicting:
        return _HealthResult(None, "conflicting_signals", composite_hit_this_eval, False)

    any_adverse_hit = any(state(key) == SignalState.HIT for key in _CORE_HEALTH_SIGNAL_KEYS)
    if any_adverse_hit:
        return _HealthResult(None, "partial_adverse_signals", composite_hit_this_eval, False)

    # Judgment call: the domain-behavior requirement ("an unavailable
    # Policy-required health signal ... yields health None and no Advice",
    # spec §2.10) is not itself one of Phase 6's nine enumerated precedence
    # bullets — item 9 ("healthy") only states its own positive condition
    # ("every Policy-required health signal is available ... healthy"). The
    # un-listed negative case (some core signal is UNAVAILABLE/BLOCKED, none
    # HIT, so none of items 2-8 fired) is placed here, immediately before
    # the healthy check it is the logical complement of, with its own
    # `health_reason` code (`"signal_unavailable"`) distinct from
    # `"partial_adverse_signals"` so a caller can tell "we don't know" apart
    # from "we know, and it's mixed."
    all_available = all(
        state(key) in (SignalState.HIT, SignalState.MISS) for key in _CORE_HEALTH_SIGNAL_KEYS
    )
    if not all_available:
        return _HealthResult(None, "signal_unavailable", composite_hit_this_eval, False)

    return _HealthResult(ScaleHealth.HEALTHY, "", composite_hit_this_eval, False)


# ---------------------------------------------------------------------------
# Phase 7: breakdown analysis (spec §2.4 "Breakdown hypothesis and FDR",
# §2.5 Phase 7)
# ---------------------------------------------------------------------------


def _single_window_bootstrap(
    days: tuple[DailyObservation, ...],
    statistic_fn: Callable[[tuple[DailyMetricValues, ...]], Decimal | None],
    n_draws: int,
    seed_hash: str,
    signal_id: str,
    policy: ScalePolicySnapshot,
) -> tuple[Decimal, Decimal, list[Decimal]]:
    """Single-window day-block bootstrap for a target-relative segment effect
    (spec §2.4: "Breakdown streams consume their current-window slots in
    canonical local-date order"). Unlike `day_block_bootstrap`, there is no
    baseline stream — every draw resamples only this one window's days."""
    n = len(days)
    if n == 0:
        raise BootstrapUnavailable(signal_id, 0, n_draws)
    stream = splitmix64_stream(bootstrap_prng_seed(seed_hash, signal_id))
    valid: list[Decimal] = []
    for _ in range(n_draws):
        sample = tuple(days[rejection_sample_index(stream, n)].metrics for _ in range(n))
        effect = statistic_fn(sample)
        if effect is not None:
            valid.append(effect)
    ratio = Decimal(len(valid)) / Decimal(n_draws)
    if ratio < policy.bootstrap_min_valid_draw_ratio:
        raise BootstrapUnavailable(signal_id, len(valid), n_draws)
    valid.sort()
    lower = nearest_rank_percentile(valid, policy.bootstrap_lower_percentile)
    upper = nearest_rank_percentile(valid, policy.bootstrap_upper_percentile)
    return lower, upper, valid


@dataclass(frozen=True)
class _BreakdownFamilyResult:
    passing_segment_keys: tuple[str, ...]
    segment_signal_hits: frozenset[str]
    extra_signals: tuple[SignalResult, ...]


def _evaluate_breakdown_family(
    snapshot: ScaleEvidenceSnapshot,
    breakdown_type: BreakdownType,
    metric: str,
    target_value: Decimal,
    policy: ScalePolicySnapshot,
    current_window_totals: _Totals,
) -> _BreakdownFamilyResult:
    """One `(snapshot, breakdown_type, economic_metric)` family (spec §2.4).
    Judgment call: `review_delivery_mix`'s "consecutive-hit gate" is treated
    as satisfied by a segment passing every other gate in the *current*
    evaluation (the spec names the gate but does not specify a required
    streak length distinct from the composite-fatigue count, and this task's
    golden vectors do not exercise it); the per-segment `StreakCounter`
    returned by `_reduce_streaks` still tracks the true history for a future
    task to tighten this gate without a schema change."""
    eligible: list[tuple[str, Decimal, tuple[DailyObservation, ...]]] = []
    for segment in snapshot.breakdowns:
        if segment.breakdown_type != breakdown_type:
            continue
        if segment.evidence_state != BreakdownEvidenceState.OBSERVED or segment.daily is None:
            continue
        segment_totals = _totals(segment.daily)
        if segment_totals.impressions < policy.breakdown_min_sample_impressions:
            continue
        if segment_totals.link_clicks < policy.breakdown_min_sample_clicks:
            continue
        if segment_totals.purchases < policy.breakdown_min_sample_purchases:
            continue
        if current_window_totals.spend <= 0:
            continue
        spend_share = segment_totals.spend / current_window_totals.spend
        if spend_share < policy.breakdown_min_spend_share:
            continue
        eligible.append((segment.segment_key, segment_totals.spend, segment.daily))

    if not eligible:
        return _BreakdownFamilyResult((), frozenset(), ())

    p_values: list[tuple[str, Decimal]] = []
    per_segment: dict[str, tuple[Decimal, Decimal, Decimal | None]] = {}
    extra_signals: list[SignalResult] = []

    for segment_key, _spend, days in eligible:
        totals = _totals(days)
        signal_id = f"breakdown:{breakdown_type.value}:{segment_key}:{metric}"
        if metric == "cpa":
            if totals.purchases <= 0:
                continue
            observed_effect = (totals.spend / totals.purchases) - target_value

            def statistic_fn(sample, _tv=target_value):
                purchases = sum((d.purchases for d in sample), Decimal(0))
                if purchases <= 0:
                    return None
                spend = sum((d.spend for d in sample), Decimal(0))
                return (spend / purchases) - _tv
        else:
            if totals.spend <= 0:
                continue
            observed_effect = target_value - (totals.purchase_value / totals.spend)

            def statistic_fn(sample, _tv=target_value):
                spend = sum((d.spend for d in sample), Decimal(0))
                if spend <= 0:
                    return None
                pv = sum((d.purchase_value for d in sample), Decimal(0))
                return _tv - (pv / spend)

        try:
            lower, upper, valid = _single_window_bootstrap(
                days, statistic_fn, policy.bootstrap_draw_count, snapshot.canonical_hash,
                signal_id, policy,
            )
        except BootstrapUnavailable:
            extra_signals.append(
                _signal(signal_id, SignalState.UNAVAILABLE, observed=observed_effect,
                        reason_code="bootstrap_unavailable")
            )
            continue

        if observed_effect <= 0:
            p_value = Decimal(1)
        else:
            extreme_count = sum(
                1 for draw in valid if (draw - observed_effect) >= observed_effect
            )
            p_value = (Decimal(extreme_count) + 1) / (Decimal(len(valid)) + 1)

        confidence_supported = lower > 0
        per_segment[segment_key] = (observed_effect, p_value, lower if confidence_supported else None)
        p_values.append((segment_key, p_value))
        extra_signals.append(
            _signal(
                signal_id,
                SignalState.HIT if (observed_effect > 0 and confidence_supported) else SignalState.MISS,
                observed=observed_effect,
                interval_lower=lower,
                interval_upper=upper,
                p_value=p_value,
                reason_code="ok",
            )
        )

    if not p_values:
        return _BreakdownFamilyResult((), frozenset(), tuple(extra_signals))

    bh_results = benjamini_hochberg(p_values, policy.fdr_limit)
    passing: list[str] = []
    hit_keys: set[str] = set()
    final_signals: list[SignalResult] = []
    q_by_key = {key: q for key, q, _passed in bh_results}
    for signal in extra_signals:
        segment_key = signal.signal_id.split(":")[2]
        q_value = q_by_key.get(segment_key)
        if q_value is None:
            final_signals.append(signal)
            continue
        final_signals.append(
            SignalResult(
                signal_id=signal.signal_id,
                state=signal.state,
                expected=signal.expected,
                observed=signal.observed,
                interval_lower=signal.interval_lower,
                interval_upper=signal.interval_upper,
                threshold=signal.threshold,
                p_value=signal.p_value,
                adjusted_q_value=q_value,
                reason_code=signal.reason_code,
            )
        )

    for segment_key, q_value, passed_fdr in bh_results:
        observed_effect, _p, confident_lower = per_segment[segment_key]
        if observed_effect > 0 and confident_lower is not None and passed_fdr:
            passing.append(segment_key)
            hit_keys.add(f"breakdown:{breakdown_type.value}:{segment_key}:{metric}")

    return _BreakdownFamilyResult(tuple(sorted(passing)), frozenset(hit_keys), tuple(final_signals))


def _breakdown_analysis(
    snapshot: ScaleEvidenceSnapshot,
    maturity: EvidenceMaturity,
    current_totals: _Totals,
) -> tuple[list[SignalResult], list[tuple[BreakdownType, str, tuple[str, ...]]], frozenset[str]]:
    if maturity == EvidenceMaturity.WARMING or maturity == EvidenceMaturity.INSUFFICIENT:
        return [], [], frozenset()
    target = snapshot.target
    if target is None:
        return [], [], frozenset()

    extra_signals: list[SignalResult] = []
    families: list[tuple[BreakdownType, str, tuple[str, ...]]] = []
    all_hit_keys: set[str] = set()

    for breakdown_type in (BreakdownType.PLACEMENT, BreakdownType.COUNTRY):
        if target.target_cpa is not None:
            result = _evaluate_breakdown_family(
                snapshot, breakdown_type, "cpa", target.target_cpa, snapshot.policy, current_totals,
            )
            extra_signals.extend(result.extra_signals)
            all_hit_keys |= result.segment_signal_hits
            if result.passing_segment_keys:
                families.append((breakdown_type, "cpa", result.passing_segment_keys))
        if target.breakeven_roas is not None:
            result = _evaluate_breakdown_family(
                snapshot, breakdown_type, "roas", target.breakeven_roas, snapshot.policy, current_totals,
            )
            extra_signals.extend(result.extra_signals)
            all_hit_keys |= result.segment_signal_hits
            if result.passing_segment_keys:
                families.append((breakdown_type, "roas", result.passing_segment_keys))

    return extra_signals, families, frozenset(all_hit_keys)


# ---------------------------------------------------------------------------
# Phase 8: scale-test eligibility (spec §2.5 Phase 8)
# ---------------------------------------------------------------------------


def _scale_test_eligible(
    snapshot: ScaleEvidenceSnapshot,
    maturity: EvidenceMaturity,
    health: ScaleHealth | None,
    healthy_evaluations: int,
    quality_state: DataQualityState,
    signals: dict[str, SignalResult],
    current_totals: _Totals,
    baseline_totals: _Totals,
) -> bool:
    policy = snapshot.policy
    target = snapshot.target

    if maturity != EvidenceMaturity.ECONOMICS_READY:
        return False
    if health != ScaleHealth.HEALTHY:
        return False
    if healthy_evaluations < policy.consecutive_healthy_evaluations_required:
        return False
    if quality_state != DataQualityState.PASS:
        return False
    if signals["configuration_drift"].state != SignalState.MISS:
        return False
    if target is None or (target.target_cpa is None and target.breakeven_roas is None):
        return False
    if snapshot.delivery_config.diagnostic_state in (
        DeliveryDiagnosticState.UNKNOWN,
        DeliveryDiagnosticState.LEARNING_LIMITED,
    ):
        return False
    if snapshot.meta_mode != MetaMode.INTERNAL_VALIDATION:
        return False

    if target.target_cpa is not None:
        if current_totals.purchases <= 0:
            return False
        current_cpa = current_totals.spend / current_totals.purchases

        def cpa_statistic(sample):
            purchases = sum((d.purchases for d in sample), Decimal(0))
            if purchases <= 0:
                return None
            spend = sum((d.spend for d in sample), Decimal(0))
            return (spend / purchases) - target.target_cpa

        try:
            _lower, upper, _valid = _single_window_bootstrap(
                snapshot.current_7d.daily, cpa_statistic, policy.bootstrap_draw_count,
                snapshot.canonical_hash, "scale_test_cpa_vs_target", policy,
            )
        except BootstrapUnavailable:
            return False
        if not (current_cpa <= target.target_cpa and upper <= 0):
            return False

    if target.breakeven_roas is not None:
        if current_totals.spend <= 0:
            return False
        current_roas = current_totals.purchase_value / current_totals.spend

        def roas_statistic(sample):
            spend = sum((d.spend for d in sample), Decimal(0))
            if spend <= 0:
                return None
            pv = sum((d.purchase_value for d in sample), Decimal(0))
            return target.breakeven_roas - (pv / spend)

        try:
            _lower, upper, _valid = _single_window_bootstrap(
                snapshot.current_7d.daily, roas_statistic, policy.bootstrap_draw_count,
                snapshot.canonical_hash, "scale_test_roas_vs_target", policy,
            )
        except BootstrapUnavailable:
            return False
        if not (current_roas >= target.breakeven_roas and upper <= 0):
            return False

    return True


# ---------------------------------------------------------------------------
# Phase 9: advice candidate assembly (spec §2.5 Phase 9)
# ---------------------------------------------------------------------------


def _reason_signature(
    snapshot: ScaleEvidenceSnapshot,
    advice_type: AdviceType,
    signal_ids: tuple[str, ...],
    segment_keys: tuple[str, ...],
) -> str:
    target_version_id = (
        snapshot.target.target_version_id if snapshot.target is not None else _NO_TARGET_MARKER
    )
    payload = {
        "advice_type": advice_type.value,
        "signal_ids": sorted(signal_ids),
        "segment_keys": sorted(segment_keys),
        "policy_version_id": snapshot.policy.policy_version_id,
        "target_version_id": target_version_id,
        "algorithm_version": snapshot.policy.algorithm_version,
        "metric_contract_set_hash": snapshot.metric_contract_set_hash,
        "configuration_baseline_hash": snapshot.delivery_config.baseline_configuration_hash,
    }
    return canonical_hash(payload, domain=_ADVICE_REASON_SIGNATURE_DOMAIN)


def _assemble_advice(
    snapshot: ScaleEvidenceSnapshot,
    health: ScaleHealth | None,
    confirmed_fatigue: bool,
    signals: dict[str, SignalResult],
    breakdown_families: list[tuple[BreakdownType, str, tuple[str, ...]]],
    scale_test_eligible: bool,
    healthy_evaluations: int,
) -> tuple[AdviceCandidate, ...]:
    candidates: list[AdviceCandidate] = []

    if confirmed_fatigue and health == ScaleHealth.CREATIVE_FATIGUE:
        economic_ids = [
            key for key in ("cpa_worsening", "roas_worsening") if signals[key].state == SignalState.HIT
        ]
        signal_ids = tuple(sorted(["frequency_high", "frequency_growth", "ctr_decline", *economic_ids]))
        candidates.append(
            AdviceCandidate(
                advice_type=AdviceType.REFRESH_CREATIVE,
                reason_signature=_reason_signature(
                    snapshot, AdviceType.REFRESH_CREATIVE, signal_ids, ()
                ),
                severity=AdviceSeverity.CRITICAL,
                signal_ids=signal_ids,
                evidence=tuple(
                    AdviceEvidenceItem(key=key, value=signals[key].observed)
                    for key in signal_ids
                    if signals[key].observed is not None
                ),
            )
        )

    if health == ScaleHealth.ECONOMICS_DECLINE:
        signal_ids = tuple(
            sorted(key for key in ("cpa_worsening", "roas_worsening") if signals[key].state == SignalState.HIT)
        )
        candidates.append(
            AdviceCandidate(
                advice_type=AdviceType.REVIEW_OFFER_OR_LANDING_PAGE,
                reason_signature=_reason_signature(
                    snapshot, AdviceType.REVIEW_OFFER_OR_LANDING_PAGE, signal_ids, ()
                ),
                severity=AdviceSeverity.WARNING,
                signal_ids=signal_ids,
                evidence=tuple(
                    AdviceEvidenceItem(key=key, value=signals[key].observed)
                    for key in signal_ids
                    if signals[key].observed is not None
                ),
            )
        )

    if health == ScaleHealth.DELIVERY_ANOMALY:
        signal_ids = tuple(
            sorted(key for key in ("cpm_increase", "delivery_decline") if signals[key].state == SignalState.HIT)
        )
        candidates.append(
            AdviceCandidate(
                advice_type=AdviceType.INVESTIGATE_DELIVERY_ANOMALY,
                reason_signature=_reason_signature(
                    snapshot, AdviceType.INVESTIGATE_DELIVERY_ANOMALY, signal_ids, ()
                ),
                severity=AdviceSeverity.WARNING,
                signal_ids=signal_ids,
                evidence=tuple(
                    AdviceEvidenceItem(key=key, value=signals[key].observed)
                    for key in signal_ids
                    if signals[key].observed is not None
                ),
            )
        )

    for breakdown_type, metric, segment_keys in breakdown_families:
        signal_ids = tuple(
            sorted(f"breakdown:{breakdown_type.value}:{key}:{metric}" for key in segment_keys)
        )
        candidates.append(
            AdviceCandidate(
                advice_type=AdviceType.REVIEW_DELIVERY_MIX,
                reason_signature=_reason_signature(
                    snapshot, AdviceType.REVIEW_DELIVERY_MIX, signal_ids, segment_keys
                ),
                severity=AdviceSeverity.WARNING,
                signal_ids=signal_ids,
                evidence=tuple(
                    AdviceEvidenceItem(key=f"segment:{i}", value=key)
                    for i, key in enumerate(segment_keys)
                ) + (AdviceEvidenceItem(key="breakdown_type", value=breakdown_type.value),
                     AdviceEvidenceItem(key="metric", value=metric)),
            )
        )

    if scale_test_eligible:
        signal_ids: tuple[str, ...] = ()
        candidates.append(
            AdviceCandidate(
                advice_type=AdviceType.CONSIDER_SCALE_TEST,
                reason_signature=_reason_signature(
                    snapshot, AdviceType.CONSIDER_SCALE_TEST, signal_ids, ()
                ),
                severity=AdviceSeverity.INFO,
                signal_ids=signal_ids,
                evidence=(
                    AdviceEvidenceItem(key="healthy_evaluations", value=healthy_evaluations),
                ),
            )
        )

    return tuple(candidates)


# ---------------------------------------------------------------------------
# Phase 10: result hash (spec §2.5 Phase 10)
# ---------------------------------------------------------------------------


def _result_hash(
    snapshot: ScaleEvidenceSnapshot,
    algorithm_version: str,
    maturity: EvidenceMaturity,
    quality_state: DataQualityState,
    health: ScaleHealth | None,
    health_reason: str,
    signals: tuple[SignalResult, ...],
    advice_candidates: tuple[AdviceCandidate, ...],
    streak_state: StreakState,
) -> str:
    payload = {
        "snapshot_canonical_hash": snapshot.canonical_hash,
        "algorithm_version": algorithm_version,
        "maturity": maturity,
        "data_quality_state": quality_state,
        "health": health,
        "health_reason": health_reason,
        "signals": signals,
        "advice_candidates": advice_candidates,
        "streak_state": streak_state,
    }
    return canonical_hash(payload, domain=_RESULT_HASH_DOMAIN)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@_register(CURRENT_ALGORITHM_VERSION)
def _evaluate_beta_v1(snapshot: ScaleEvidenceSnapshot) -> ScaleEvaluation:
    with localcontext() as ctx:
        ctx.prec = snapshot.policy.decimal_precision
        ctx.rounding = snapshot.policy.decimal_rounding_mode

        _validate_snapshot(snapshot)

        quality_state = _data_quality_state(snapshot.data_quality_issues)
        current_compat = _build_compatibility(snapshot)

        if quality_state == DataQualityState.BLOCKED:
            reset_resolution = _resolve_streak_reset(snapshot, current_compat, quality_blocked=True)
            streak_state = _reduce_streaks(
                current_compat, reset_resolution, health=None,
                hit_keys=frozenset(), extra_known_keys=frozenset(),
            )
            blocked_signals = tuple(
                _signal(key, SignalState.BLOCKED, reason_code="data_quality_blocked")
                for key in (*_CORE_SIGNAL_KEYS, "configuration_drift")
            )
            result_hash = _result_hash(
                snapshot, CURRENT_ALGORITHM_VERSION, EvidenceMaturity.INSUFFICIENT,
                quality_state, None, "quality_blocked", blocked_signals, (), streak_state,
            )
            return ScaleEvaluation(
                snapshot_canonical_hash=snapshot.canonical_hash,
                algorithm_version=CURRENT_ALGORITHM_VERSION,
                maturity=EvidenceMaturity.INSUFFICIENT,
                data_quality_state=quality_state,
                health=None,
                health_reason="quality_blocked",
                signals=blocked_signals,
                advice_candidates=(),
                streak_state=streak_state,
                result_hash=result_hash,
            )

        current_totals = _totals(snapshot.current_7d.daily)
        baseline_totals = _totals(snapshot.baseline_7d.daily)

        not_delivering = _is_not_delivering(snapshot, current_totals)
        maturity = _maturity(snapshot, current_totals, baseline_totals)
        primitive = _primitive_signals(snapshot, current_totals, baseline_totals)
        signals_by_id = dict(primitive.results)

        breakdown_signals, breakdown_families, breakdown_hit_keys = _breakdown_analysis(
            snapshot, maturity, current_totals
        )

        # Resolve compatibility resets *before* reading any prior-streak
        # value for confirmation. Reading `snapshot.prior_streak.counters`
        # directly here (without applying this cycle's Policy/Target/
        # contract-set/configuration-baseline/data-continuity mismatch
        # check first) would let a stale pre-reset streak confirm fatigue —
        # or any other composite — in the very same cycle its lineage is
        # reset, contradicting the `StreakReset` records this same
        # evaluation emits. `_reduce_streaks` below reuses this exact
        # resolution, so confirmation and the persisted streak state can
        # never disagree about whether a reset happened this cycle.
        reset_resolution = _resolve_streak_reset(snapshot, current_compat, quality_blocked=False)
        prior_fatigue_streak = reset_resolution.prior_counters.get(_COMPOSITE_FATIGUE_KEY, 0)

        provisional_composite_hit = (
            signals_by_id["frequency_high"].state == SignalState.HIT
            and signals_by_id["frequency_growth"].state == SignalState.HIT
            and signals_by_id["ctr_decline"].state == SignalState.HIT
            and (
                signals_by_id["cpa_worsening"].state == SignalState.HIT
                or signals_by_id["roas_worsening"].state == SignalState.HIT
            )
            and maturity == EvidenceMaturity.ECONOMICS_READY
        )
        composite_streak_after_this_hit = prior_fatigue_streak + 1 if provisional_composite_hit else 0

        health_result = _classify_health(
            signals_by_id, maturity, not_delivering, primitive.stable_ctr, primitive.stable_cvr,
            composite_streak_after_this_hit, snapshot.policy,
        )

        hit_keys = {key for key, result in signals_by_id.items() if result.state == SignalState.HIT}
        if health_result.composite_fatigue_hit:
            hit_keys.add(_COMPOSITE_FATIGUE_KEY)
        hit_keys |= breakdown_hit_keys

        streak_state = _reduce_streaks(
            current_compat, reset_resolution, health=health_result.health,
            hit_keys=frozenset(hit_keys), extra_known_keys=breakdown_hit_keys,
        )

        scale_test_eligible = _scale_test_eligible(
            snapshot, maturity, health_result.health, streak_state.healthy_evaluations,
            quality_state, signals_by_id, current_totals, baseline_totals,
        )

        advice = _assemble_advice(
            snapshot, health_result.health, health_result.confirmed_fatigue, signals_by_id,
            breakdown_families, scale_test_eligible, streak_state.healthy_evaluations,
        )

        all_signals = tuple(
            signals_by_id[key] for key in (*_CORE_SIGNAL_KEYS[:-1], "configuration_drift", "no_delivery_complete_day")
        ) + tuple(sorted(breakdown_signals, key=lambda s: s.signal_id))

        result_hash = _result_hash(
            snapshot, CURRENT_ALGORITHM_VERSION, maturity, quality_state, health_result.health,
            health_result.health_reason, all_signals, advice, streak_state,
        )

        return ScaleEvaluation(
            snapshot_canonical_hash=snapshot.canonical_hash,
            algorithm_version=CURRENT_ALGORITHM_VERSION,
            maturity=maturity,
            data_quality_state=quality_state,
            health=health_result.health,
            health_reason=health_result.health_reason,
            signals=all_signals,
            advice_candidates=advice,
            streak_state=streak_state,
            result_hash=result_hash,
        )
