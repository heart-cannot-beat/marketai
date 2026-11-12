"""Golden-vector tests for the pure-function Scale evaluator.

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-9-brief.md
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.scale._contracts import (
    BreakdownSegment,
    CanonicalKeyValue,
    DailyMetricValues,
    DailyObservation,
    DataQualityItem,
    DataQualityLocator,
    DeliveryConfigSnapshot,
    ExactWindowMetrics,
    InvalidScaleSnapshot,
    MetricContractRef,
    MetricWindow,
    PriorStreakState,
    ScaleEvidenceSnapshot,
    ScalePolicySnapshot,
    ScaleTargetSnapshot,
    StreakCompatibility,
    StreakCounter,
)
from app.scale.types import (
    BreakdownEvidenceState,
    BreakdownType,
    DailyEvidenceState,
    DataQualityImpact,
    DataQualityIssueCode,
    DeliveryDiagnosticState,
    EvidenceMaturity,
    MetaMode,
    MetricContractType,
    PriorStreakAbsenceReason,
    ScaleHealth,
    SignalState,
    WindowRole,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

CURRENT_START = date(2026, 8, 30)
CURRENT_END = date(2026, 9, 6)  # exclusive
BASELINE_START = date(2026, 8, 23)
AS_OF = date(2026, 9, 5)


def _policy(**overrides) -> ScalePolicySnapshot:
    defaults = dict(
        policy_version_id="policy-v1",
        algorithm_version="beta-v1",
        decimal_precision=28,
        decimal_rounding_mode="ROUND_HALF_EVEN",
        confidence_level=Decimal("0.95"),
        confidence_z=Decimal("1.96"),
        complete_day_floor=14,
        active_day_floor=10,
        impression_floor=1000,
        click_floor=20,
        purchase_floor=5,
        spend_floor=Decimal("100"),
        frequency_level_threshold=Decimal("3"),
        frequency_growth_threshold=Decimal("0.2"),
        ctr_decline_threshold=Decimal("0.2"),
        ctr_stability_tolerance=Decimal("0.05"),
        cvr_decline_threshold=Decimal("0.2"),
        cvr_stability_tolerance=Decimal("0.05"),
        cpm_increase_threshold=Decimal("0.2"),
        delivery_decline_threshold=Decimal("0.2"),
        cpa_worsening_threshold=Decimal("0.2"),
        roas_worsening_threshold=Decimal("0.2"),
        consecutive_composite_fatigue_hits_required=2,
        consecutive_healthy_evaluations_required=3,
        bootstrap_prng_version="bootstrap-prng-v1",
        bootstrap_draw_count=200,
        bootstrap_min_valid_draw_ratio=Decimal("0.5"),
        bootstrap_lower_percentile=Decimal("0.025"),
        bootstrap_upper_percentile=Decimal("0.975"),
        breakdown_min_spend_share=Decimal("0.05"),
        breakdown_min_sample_impressions=500,
        breakdown_min_sample_clicks=10,
        breakdown_min_sample_purchases=2,
        fdr_limit=Decimal("0.10"),
        quality_warning_permitted_codes=(DataQualityIssueCode.FRESHNESS_SLO_EXCEEDED,),
        streak_reset_reason_codes=("policy_version_changed", "quality_blocked"),
        advice_construction_parameters=(),
    )
    defaults.update(overrides)
    return ScalePolicySnapshot(**defaults)


def _day(local_date, impressions, link_clicks, purchases, spend, purchase_value, delivered=True):
    return DailyObservation(
        local_date=local_date,
        evidence_state=DailyEvidenceState.PRESENT,
        metrics=DailyMetricValues(
            impressions=impressions,
            link_clicks=link_clicks,
            purchases=Decimal(purchases),
            spend=Decimal(spend),
            purchase_value=Decimal(purchase_value),
            delivered=delivered,
        ),
    )


def _uniform_week(
    start,
    impressions,
    link_clicks,
    purchases,
    spend,
    purchase_value,
    delivered=True,
    last_day_override=None,
):
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        if i == 6 and last_day_override is not None:
            days.append(last_day_override(d))
        else:
            days.append(_day(d, impressions, link_clicks, purchases, spend, purchase_value, delivered))
    return tuple(days)


def _window(role, start, daily):
    return MetricWindow(
        role=role, start_date=start, end_date_exclusive=start + timedelta(days=7), daily=daily
    )


def _exact(role, start, end, reach, frequency):
    return ExactWindowMetrics(
        role=role,
        start_date=start,
        end_date_exclusive=end,
        reach=reach,
        frequency=Decimal(frequency) if frequency is not None else None,
        observation_contract_version_id="ocv-1",
        evidence_hash="e" * 64,
    )


def _contract(contract_type, currency="USD"):
    return MetricContractRef(
        contract_type=contract_type,
        scale_contract_version="scv-1",
        observation_contract_generation="gen-1",
        observation_contract_version_id="ocv-1",
        contract_hash="c" * 64,
        connection_id="conn-1",
        meta_mode=MetaMode.INTERNAL_VALIDATION,
        scenario_version_id=None,
        timezone_name="UTC",
        currency=currency,
    )


def _delivery_config(
    baseline_hash="cfg-baseline",
    current_hash="cfg-baseline",
    diagnostic=DeliveryDiagnosticState.NONE_,
    status_history=(),
):
    return DeliveryConfigSnapshot(
        baseline_configuration_hash=baseline_hash,
        current_configuration_hash=current_hash,
        effective_campaign_status="ACTIVE",
        effective_ad_set_status="ACTIVE",
        effective_ad_status="ACTIVE",
        status_history=status_history,
        attribution_setting="7d_click_1d_view",
        diagnostic_state=diagnostic,
        diagnostic_detail=(),
    )


def _snapshot(
    *,
    policy=None,
    target=None,
    current_daily,
    baseline_daily,
    current_frequency="1.5",
    baseline_frequency="1.5",
    complete_days=20,
    active_days=15,
    data_quality_issues=(),
    prior_streak=None,
    delivery_config=None,
    meta_mode=MetaMode.INTERNAL_VALIDATION,
    breakdowns=(),
    as_of=AS_OF,
    currency="USD",
):
    policy = policy or _policy()
    delivery_config = delivery_config or _delivery_config()
    current_7d = _window(WindowRole.CURRENT_7D, CURRENT_START, current_daily)
    baseline_7d = _window(WindowRole.BASELINE_7D, BASELINE_START, baseline_daily)
    current_exact = _exact(
        WindowRole.CURRENT_7D, CURRENT_START, CURRENT_END, reach=1000, frequency=current_frequency
    )
    baseline_exact = _exact(
        WindowRole.BASELINE_7D, BASELINE_START, CURRENT_START, reach=1000, frequency=baseline_frequency
    )
    rolling_exact = _exact(
        WindowRole.ROLLING_14D, BASELINE_START, CURRENT_END, reach=1800, frequency="2.0"
    )
    contracts = (
        _contract(MetricContractType.DAILY, currency),
        _contract(MetricContractType.DELIVERY_CONFIGURATION, currency),
        _contract(MetricContractType.EXACT_WINDOW, currency),
    )
    return ScaleEvidenceSnapshot(
        snapshot_id="ssnp_test",
        canonical_hash="a" * 64,
        canonical_schema_version="scale-snapshot-v1",
        episode_id="sepi_test",
        meta_mode=meta_mode,
        meta_connection_id="conn-1",
        scenario_version_id=None,
        timezone_name="UTC",
        currency=currency,
        policy=policy,
        target=target,
        metric_contract_set_hash="mcs-1",
        metric_contracts=contracts,
        delivery_config=delivery_config,
        current_7d=current_7d,
        baseline_7d=baseline_7d,
        current_exact_7d=current_exact,
        baseline_exact_7d=baseline_exact,
        rolling_exact_14d=rolling_exact,
        breakdowns=breakdowns,
        data_quality_issues=data_quality_issues,
        complete_local_days_in_episode=complete_days,
        active_delivery_days_in_episode=active_days,
        data_continuity_generation="gen-1",
        prior_streak=prior_streak,
        prior_streak_absence_reason=(
            None if prior_streak is not None else PriorStreakAbsenceReason.NO_EARLIER_DISTINCT_DAY
        ),
        last_complete_local_day=as_of,
        last_sync_at=datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc),
        freshness_reference_at=datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc),
        as_of_local_day=as_of,
    )


def _flat_snapshot(**overrides):
    """A stable, no-change 7-day week on both sides: every relative signal
    is available and MISS, so this is the base for a healthy evaluation.
    `purchase_value=60` (not 0) so ROAS is a positive, available baseline
    (spec §2.4: a zero-valued ratio, not just a zero denominator sum, makes
    a relative signal unavailable rather than stable)."""
    current_daily = _uniform_week(CURRENT_START, 2000, 200, 1, 20, 60)
    baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 60)
    kwargs = dict(
        current_daily=current_daily,
        baseline_daily=baseline_daily,
        current_frequency="1.5",
        baseline_frequency="1.5",
    )
    kwargs.update(overrides)
    return _snapshot(**kwargs)


class TestEvaluateScaleContract(unittest.TestCase):
    def test_import(self):
        from app.scale.evaluator import evaluate_scale, CURRENT_ALGORITHM_VERSION
        self.assertIsNotNone(evaluate_scale)
        self.assertEqual(CURRENT_ALGORITHM_VERSION, "beta-v2")

    def test_pure_function_no_io(self):
        """Verify evaluator module has no forbidden imports."""
        import ast
        import pathlib
        source = pathlib.Path("app/scale/evaluator.py").read_text()
        tree = ast.parse(source)
        forbidden = {"os", "sys", "logging", "random", "requests", "httpx",
                      "sqlalchemy", "asyncio", "socket", "subprocess"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif node.module:
                    names = [node.module.split(".")[0]]
                else:
                    names = []
                for name in names:
                    self.assertNotIn(name, forbidden,
                                     f"evaluator imports forbidden module: {name}")


class TestRegistry(unittest.TestCase):
    def test_unregistered_algorithm_version_rejected(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot(policy=_policy(algorithm_version="unknown-v9"))
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(snapshot)
        self.assertEqual(ctx.exception.code, "algorithm_version_unavailable")


class TestDeterminism(unittest.TestCase):
    def test_repeated_calls_are_byte_identical(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot()
        result_a = evaluate_scale(snapshot)
        result_b = evaluate_scale(snapshot)
        self.assertEqual(result_a.result_hash, result_b.result_hash)
        self.assertEqual(result_a, result_b)

    def test_evaluation_does_not_mutate_input(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot()
        before = snapshot.current_7d.daily[0].metrics.impressions
        evaluate_scale(snapshot)
        after = snapshot.current_7d.daily[0].metrics.impressions
        self.assertEqual(before, after)


class TestWarming(unittest.TestCase):
    def test_insufficient_days_is_warming(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot(complete_days=5, active_days=3)
        result = evaluate_scale(snapshot)
        self.assertEqual(result.maturity, EvidenceMaturity.WARMING)
        self.assertEqual(result.advice_candidates, ())


class TestBlockedQuality(unittest.TestCase):
    def test_blocking_issue_returns_insufficient_none_zero_advice(self):
        from app.scale.evaluator import evaluate_scale
        issue = DataQualityItem(
            code=DataQualityIssueCode.DAILY_SLOT_MISSING,
            impact=DataQualityImpact.BLOCKING,
            locator=DataQualityLocator(
                contract_type=MetricContractType.DAILY,
                window_role=WindowRole.CURRENT_7D,
                local_date=CURRENT_START,
                breakdown_type=None,
                segment_key=None,
                source_evidence_hash="h" * 64,
            ),
            details=(),
        )
        snapshot = _flat_snapshot(data_quality_issues=(issue,))
        result = evaluate_scale(snapshot)
        self.assertEqual(result.maturity, EvidenceMaturity.INSUFFICIENT)
        self.assertIsNone(result.health)
        self.assertEqual(result.advice_candidates, ())
        self.assertTrue(all(s.state == SignalState.BLOCKED for s in result.signals))

    def test_blocked_is_deterministic(self):
        from app.scale.evaluator import evaluate_scale
        issue = DataQualityItem(
            code=DataQualityIssueCode.DAILY_SLOT_MISSING,
            impact=DataQualityImpact.BLOCKING,
            locator=DataQualityLocator(
                contract_type=MetricContractType.DAILY,
                window_role=WindowRole.CURRENT_7D,
                local_date=CURRENT_START,
                breakdown_type=None,
                segment_key=None,
                source_evidence_hash="h" * 64,
            ),
            details=(),
        )
        snapshot = _flat_snapshot(data_quality_issues=(issue,))
        r1 = evaluate_scale(snapshot)
        r2 = evaluate_scale(snapshot)
        self.assertEqual(r1.result_hash, r2.result_hash)


class TestHealthyNoAdvice(unittest.TestCase):
    def test_flat_week_is_healthy_with_no_advice(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot()
        result = evaluate_scale(snapshot)
        self.assertEqual(result.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(result.health, ScaleHealth.HEALTHY)
        self.assertEqual(result.health_reason, "")
        self.assertEqual(result.advice_candidates, ())
        for signal in result.signals:
            if signal.signal_id in (
                "frequency_high", "frequency_growth", "ctr_decline", "cvr_decline",
                "cpm_increase", "delivery_decline", "cpa_worsening", "no_delivery_complete_day",
                "configuration_drift",
            ):
                self.assertEqual(signal.state, SignalState.MISS, signal.signal_id)


class TestNotDelivering(unittest.TestCase):
    def test_zero_current_impressions_is_not_delivering(self):
        from app.scale.evaluator import evaluate_scale
        current_daily = _uniform_week(CURRENT_START, 0, 0, 0, 0, 0, delivered=False)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        snapshot = _flat_snapshot(current_daily=current_daily, baseline_daily=baseline_daily)
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.NOT_DELIVERING)
        self.assertEqual(result.advice_candidates, ())


class TestFatigueWatchAndConfirmedFatigue(unittest.TestCase):
    """frequency_high + frequency_growth + ctr_decline (Newcombe-supported)
    + cpa_worsening (bootstrap-supported), all at economics_ready maturity."""

    def _fatigue_snapshot(self, prior_streak=None):
        # baseline: freq 2.0, CTR 10%, CPA 20; current: freq 3.5, CTR 5%, CPA 40
        current_daily = _uniform_week(CURRENT_START, 2000, 100, 1, 40, 0)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        return _flat_snapshot(
            current_daily=current_daily,
            baseline_daily=baseline_daily,
            current_frequency="3.5",
            baseline_frequency="2.0",
            prior_streak=prior_streak,
        )

    def test_first_hit_is_fatigue_watch(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = self._fatigue_snapshot()
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.FATIGUE_WATCH)
        self.assertEqual(result.advice_candidates, ())
        composite = next(
            c for c in result.streak_state.counters if c.signal_key == "composite_fatigue"
        )
        self.assertEqual(composite.consecutive_hits, 1)

    def test_second_consecutive_hit_confirms_fatigue(self):
        from app.scale.evaluator import evaluate_scale
        compat = StreakCompatibility(
            policy_version_id="policy-v1",
            target_version_id=None,
            metric_contract_set_hash="mcs-1",
            configuration_baseline_hash="cfg-baseline",
            data_continuity_generation="gen-1",
            canonical_hash="whatever",
        )
        prior = PriorStreakState(
            evaluation_id="seval_prior",
            evaluated_local_day=AS_OF - timedelta(days=1),
            compatibility=compat,
            health=ScaleHealth.FATIGUE_WATCH,
            healthy_evaluations=0,
            counters=(StreakCounter(signal_key="composite_fatigue", consecutive_hits=1),),
        )
        snapshot = self._fatigue_snapshot(prior_streak=prior)
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.CREATIVE_FATIGUE)
        self.assertEqual(len(result.advice_candidates), 1)
        self.assertEqual(result.advice_candidates[0].advice_type.value, "refresh_creative")
        composite = next(
            c for c in result.streak_state.counters if c.signal_key == "composite_fatigue"
        )
        self.assertEqual(composite.consecutive_hits, 2)

    def test_compatibility_mismatch_prevents_false_fatigue_confirmation(self):
        """Regression for a reported bug: Phase 6 read
        `snapshot.prior_streak.counters` directly to decide whether this
        cycle's composite-fatigue hit is *confirmed*, before applying the
        same Policy/Target/contract-set/configuration-baseline/data-
        continuity compatibility check the streak reducer (Phase 5) applies.
        With a prior `composite_fatigue` streak of 1,
        `consecutive_composite_fatigue_hits_required=2`, a genuine
        composite-fatigue hit this cycle, *and* a Policy-version mismatch
        this cycle, the old code produced `CREATIVE_FATIGUE` health plus a
        `refresh_creative` Advice candidate while `streak_state.resets`
        simultaneously recorded that same counter being reset to 0 — an
        internally contradictory result (confirmed fatigue from a streak the
        same evaluation says didn't survive). The fix must resolve the
        compatibility reset first and use the *post-reset* prior count for
        confirmation, so a mismatch this cycle can never produce a
        false-positive confirmation: the composite hit still counts (this
        cycle's own evidence is real), but only starts a fresh streak of 1,
        one short of the required 2."""
        from app.scale.evaluator import evaluate_scale
        compat = StreakCompatibility(
            policy_version_id="policy-v0",  # mismatches the snapshot's policy-v1
            target_version_id=None,
            metric_contract_set_hash="mcs-1",
            configuration_baseline_hash="cfg-baseline",
            data_continuity_generation="gen-1",
            canonical_hash="whatever",
        )
        prior = PriorStreakState(
            evaluation_id="seval_prior",
            evaluated_local_day=AS_OF - timedelta(days=1),
            compatibility=compat,
            health=ScaleHealth.FATIGUE_WATCH,
            healthy_evaluations=0,
            counters=(StreakCounter(signal_key="composite_fatigue", consecutive_hits=1),),
        )
        snapshot = self._fatigue_snapshot(prior_streak=prior)
        result = evaluate_scale(snapshot)

        self.assertEqual(result.health, ScaleHealth.FATIGUE_WATCH)
        self.assertNotEqual(result.health, ScaleHealth.CREATIVE_FATIGUE)
        self.assertEqual(result.advice_candidates, ())

        composite = next(
            c for c in result.streak_state.counters if c.signal_key == "composite_fatigue"
        )
        self.assertEqual(composite.consecutive_hits, 1)

        reset_ids = {(r.streak_id, r.reason_code) for r in result.streak_state.resets}
        self.assertIn(("composite_fatigue", "policy_version_changed"), reset_ids)


class TestEconomicsDecline(unittest.TestCase):
    def test_stable_ctr_with_cpa_worsening_routes_to_offer_review(self):
        from app.scale.evaluator import evaluate_scale
        # CTR unchanged (stable); CPA doubles.
        current_daily = _uniform_week(CURRENT_START, 2000, 200, 1, 40, 0)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        snapshot = _flat_snapshot(current_daily=current_daily, baseline_daily=baseline_daily)
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.ECONOMICS_DECLINE)
        self.assertEqual(len(result.advice_candidates), 1)
        self.assertEqual(
            result.advice_candidates[0].advice_type.value, "review_offer_or_landing_page"
        )


class TestZeroRatioBootstrapDraws(unittest.TestCase):
    """A resampled week can hold only the baseline days whose ratio is zero
    even when the whole baseline's is positive. That draw is unavailable
    (spec §2.4), not a division by zero that fails the evaluation (#110:
    live sandbox data, where purchases land on some days only)."""

    def _signal(self, result, signal_id):
        [signal] = [s for s in result.signals if s.signal_id == signal_id]
        return signal

    def test_baseline_days_without_purchase_value_do_not_divide_by_zero_in_roas(self):
        from app.scale.evaluator import evaluate_scale
        baseline_daily = _uniform_week(
            BASELINE_START, 2000, 200, 1, 20, 0,
            last_day_override=lambda d: _day(d, 2000, 200, 1, 20, 420),
        )
        snapshot = _flat_snapshot(baseline_daily=baseline_daily)

        result = evaluate_scale(snapshot)

        self.assertEqual(evaluate_scale(snapshot), result, "deterministic")
        self.assertIn(self._signal(result, "roas_worsening").state,
                      (SignalState.HIT, SignalState.MISS, SignalState.UNAVAILABLE))

    def test_baseline_days_without_spend_do_not_divide_by_zero_in_cpa(self):
        from app.scale.evaluator import evaluate_scale
        baseline_daily = _uniform_week(
            BASELINE_START, 2000, 200, 1, 0, 0,
            last_day_override=lambda d: _day(d, 2000, 200, 1, 140, 0),
        )
        snapshot = _flat_snapshot(baseline_daily=baseline_daily)

        result = evaluate_scale(snapshot)

        self.assertIn(self._signal(result, "cpa_worsening").state,
                      (SignalState.HIT, SignalState.MISS, SignalState.UNAVAILABLE))


class TestDeliveryAnomaly(unittest.TestCase):
    def test_stable_ctr_cvr_cpa_roas_with_delivery_decline_routes_to_delivery_anomaly(self):
        from app.scale.evaluator import evaluate_scale
        # Every current-window daily quantity is exactly half of baseline's,
        # so CTR, CVR, CPA, ROAS, and CPM are all unchanged pure ratios
        # (stable) while total current-window impressions/clicks/spend drop
        # — an unambiguous delivery decline that cannot be confused with an
        # economic or creative signal.
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 2, 40, 120)
        current_daily = _uniform_week(CURRENT_START, 1000, 100, 1, 20, 60)
        snapshot = _flat_snapshot(current_daily=current_daily, baseline_daily=baseline_daily)
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.DELIVERY_ANOMALY)
        self.assertEqual(len(result.advice_candidates), 1)
        self.assertEqual(
            result.advice_candidates[0].advice_type.value, "investigate_delivery_anomaly"
        )


class TestConflictingAndPartialAdverse(unittest.TestCase):
    def test_ctr_decline_with_clear_economic_improvement_is_none(self):
        from app.scale.evaluator import evaluate_scale
        # CTR declines a lot, but CPA improves dramatically (halved).
        current_daily = _uniform_week(CURRENT_START, 2000, 100, 2, 10, 0)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        snapshot = _flat_snapshot(current_daily=current_daily, baseline_daily=baseline_daily)
        result = evaluate_scale(snapshot)
        self.assertIsNone(result.health)
        self.assertEqual(result.health_reason, "conflicting_signals")
        self.assertEqual(result.advice_candidates, ())

    def test_isolated_adverse_hit_yields_partial_adverse_none(self):
        from app.scale.evaluator import evaluate_scale
        # Only CPM increases; nothing else moves.
        current_daily = _uniform_week(CURRENT_START, 2000, 200, 1, 40, 0)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        # Keep purchase_value at 0 both sides so ROAS stays unavailable and
        # only cpa_worsening + cpm_increase are live; force purchases equal
        # so CPA is unaffected by the spend change... instead scale spend
        # only for CPM by keeping purchases identical but this also moves
        # CPA. Use a dedicated no-spend-change fixture instead.
        current_daily = _uniform_week(CURRENT_START, 2000, 200, 1, 20, 0)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        # Only frequency moves (frequency_high alone, without growth/ctr).
        snapshot = _flat_snapshot(
            current_daily=current_daily,
            baseline_daily=baseline_daily,
            current_frequency="3.5",
            baseline_frequency="3.4",
        )
        result = evaluate_scale(snapshot)
        self.assertIsNone(result.health)
        self.assertEqual(result.health_reason, "partial_adverse_signals")
        self.assertEqual(result.advice_candidates, ())


class TestScaleTestEligible(unittest.TestCase):
    def test_healthy_streak_with_target_is_scale_test_eligible(self):
        from app.scale.evaluator import evaluate_scale
        target = ScaleTargetSnapshot(
            target_version_id="target-v1",
            currency="USD",
            target_cpa=Decimal("25"),
            breakeven_roas=None,
            minimum_economic_spend=Decimal("50"),
            frequency_level_threshold_override=None,
            frequency_growth_threshold_override=None,
            ctr_decline_threshold_override=None,
            cvr_decline_threshold_override=None,
            cpm_increase_threshold_override=None,
            delivery_decline_threshold_override=None,
            cpa_worsening_threshold_override=None,
            roas_worsening_threshold_override=None,
        )
        compat = StreakCompatibility(
            policy_version_id="policy-v1",
            target_version_id="target-v1",
            metric_contract_set_hash="mcs-1",
            configuration_baseline_hash="cfg-baseline",
            data_continuity_generation="gen-1",
            canonical_hash="whatever",
        )
        prior = PriorStreakState(
            evaluation_id="seval_prior",
            evaluated_local_day=AS_OF - timedelta(days=1),
            compatibility=compat,
            health=ScaleHealth.HEALTHY,
            healthy_evaluations=2,
            counters=(),
        )
        snapshot = _flat_snapshot(target=target, prior_streak=prior)
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.HEALTHY)
        self.assertEqual(result.streak_state.healthy_evaluations, 3)
        advice_types = {c.advice_type.value for c in result.advice_candidates}
        self.assertIn("consider_scale_test", advice_types)

    def test_sandbox_mode_cannot_produce_scale_test_advice(self):
        from app.scale.evaluator import evaluate_scale
        target = ScaleTargetSnapshot(
            target_version_id="target-v1",
            currency="USD",
            target_cpa=Decimal("25"),
            breakeven_roas=None,
            minimum_economic_spend=Decimal("50"),
            frequency_level_threshold_override=None,
            frequency_growth_threshold_override=None,
            ctr_decline_threshold_override=None,
            cvr_decline_threshold_override=None,
            cpm_increase_threshold_override=None,
            delivery_decline_threshold_override=None,
            cpa_worsening_threshold_override=None,
            roas_worsening_threshold_override=None,
        )
        compat = StreakCompatibility(
            policy_version_id="policy-v1",
            target_version_id="target-v1",
            metric_contract_set_hash="mcs-1",
            configuration_baseline_hash="cfg-baseline",
            data_continuity_generation="gen-1",
            canonical_hash="whatever",
        )
        prior = PriorStreakState(
            evaluation_id="seval_prior",
            evaluated_local_day=AS_OF - timedelta(days=1),
            compatibility=compat,
            health=ScaleHealth.HEALTHY,
            healthy_evaluations=2,
            counters=(),
        )
        snapshot = _flat_snapshot(
            target=target, prior_streak=prior, meta_mode=MetaMode.CUSTOMER_SANDBOX
        )
        result = evaluate_scale(snapshot)
        advice_types = {c.advice_type.value for c in result.advice_candidates}
        self.assertNotIn("consider_scale_test", advice_types)


class TestStreakReset(unittest.TestCase):
    def test_policy_version_change_resets_counters(self):
        from app.scale.evaluator import evaluate_scale
        compat = StreakCompatibility(
            policy_version_id="policy-v0",
            target_version_id=None,
            metric_contract_set_hash="mcs-1",
            configuration_baseline_hash="cfg-baseline",
            data_continuity_generation="gen-1",
            canonical_hash="whatever",
        )
        prior = PriorStreakState(
            evaluation_id="seval_prior",
            evaluated_local_day=AS_OF - timedelta(days=1),
            compatibility=compat,
            health=ScaleHealth.HEALTHY,
            healthy_evaluations=2,
            counters=(StreakCounter(signal_key="composite_fatigue", consecutive_hits=1),),
        )
        snapshot = _flat_snapshot(prior_streak=prior)  # policy-v1 now, was policy-v0
        result = evaluate_scale(snapshot)
        self.assertEqual(result.health, ScaleHealth.HEALTHY)
        # Spec §2.5 Phase 6: "any ... compatibility reset returns it to
        # zero" — a reset zeroes the counter outright rather than zeroing
        # then re-incrementing within the same cycle, even though this
        # evaluation is itself healthy.
        self.assertEqual(result.streak_state.healthy_evaluations, 0)
        reset_ids = {(r.streak_id, r.reason_code) for r in result.streak_state.resets}
        self.assertIn(("healthy_evaluations", "policy_version_changed"), reset_ids)
        self.assertIn(("composite_fatigue", "policy_version_changed"), reset_ids)


class TestHashSensitivity(unittest.TestCase):
    def test_changing_snapshot_canonical_hash_changes_result_hash(self):
        from app.scale.evaluator import evaluate_scale
        s1 = _flat_snapshot()
        s2 = _snapshot(
            current_daily=s1.current_7d.daily,
            baseline_daily=s1.baseline_7d.daily,
        )
        object.__setattr__(s2, "canonical_hash", "b" * 64)
        r1 = evaluate_scale(s1)
        r2 = evaluate_scale(s2)
        self.assertNotEqual(r1.result_hash, r2.result_hash)

    def test_healthy_flat_week_result_hash_is_byte_identical_to_pinned_value(self):
        """The brief requires asserting a byte-identical `result_hash`, not
        just self-consistency between two calls in the same test run: a
        hardcoded literal is the only thing that catches a silent change to
        canonical hashing (payload shape, domain separator, field order,
        Decimal/enum encoding, ...) that happens to still be internally
        consistent with itself. This value was captured from a passing run
        of `_flat_snapshot()` (the same fixture `TestHealthyNoAdvice` uses)
        against `_evaluate_beta_v1`; a deliberate algorithm change to
        `beta-v1` must recompute and update this literal, not silently pass
        because it never fails a self-comparison."""
        from app.scale.evaluator import evaluate_scale
        result = evaluate_scale(_flat_snapshot())
        self.assertEqual(result.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(result.health, ScaleHealth.HEALTHY)
        self.assertEqual(
            result.result_hash,
            "12e0d3bae29416f1b2c78df509df1b901f39e7887d3e6130e37c82b5ec108dab",
        )
        self.assertEqual(len(result.result_hash), 64)


class TestBreakdownAnalysis(unittest.TestCase):
    """End-to-end coverage for `_evaluate_breakdown_family`
    (bootstrap -> BH correction -> segment pass -> `review_delivery_mix`),
    previously exercised only by empty-`breakdowns` fixtures."""

    def _segment(self, segment_key="feed", impressions=100, link_clicks=15, purchases=1, spend=2, purchase_value=6):
        daily = _uniform_week(CURRENT_START, impressions, link_clicks, purchases, spend, purchase_value)
        return BreakdownSegment(
            breakdown_type=BreakdownType.PLACEMENT,
            segment_key=segment_key,
            evidence_state=BreakdownEvidenceState.OBSERVED,
            daily=daily,
            suppression_reason=None,
        )

    def _target(self, target_cpa):
        return ScaleTargetSnapshot(
            target_version_id="target-v1",
            currency="USD",
            target_cpa=target_cpa,
            breakeven_roas=None,
            minimum_economic_spend=Decimal("50"),
            frequency_level_threshold_override=None,
            frequency_growth_threshold_override=None,
            ctr_decline_threshold_override=None,
            cvr_decline_threshold_override=None,
            cpm_increase_threshold_override=None,
            delivery_decline_threshold_override=None,
            cpa_worsening_threshold_override=None,
            roas_worsening_threshold_override=None,
        )

    def test_segment_worse_than_target_cpa_produces_review_delivery_mix(self):
        from app.scale.evaluator import evaluate_scale
        # Segment CPA = spend/purchases = 14/7 = 2 (uniform across all 7
        # days), well above target_cpa=1: every bootstrap draw reproduces
        # the same effect (identical days), so the interval is confidently
        # supported and the one-sided p-value is far below fdr_limit=0.10.
        segment = self._segment()
        snapshot = _flat_snapshot(
            target=self._target(Decimal("1")), breakdowns=(segment,),
        )
        result = evaluate_scale(snapshot)

        self.assertEqual(result.health, ScaleHealth.HEALTHY)
        self.assertEqual(len(result.advice_candidates), 1)
        candidate = result.advice_candidates[0]
        self.assertEqual(candidate.advice_type.value, "review_delivery_mix")
        self.assertIn("breakdown:placement:feed:cpa", candidate.signal_ids)
        evidence_by_key = {e.key: e.value for e in candidate.evidence}
        self.assertEqual(evidence_by_key["segment:0"], "feed")
        self.assertEqual(evidence_by_key["breakdown_type"], "placement")
        self.assertEqual(evidence_by_key["metric"], "cpa")

        signal = next(
            s for s in result.signals if s.signal_id == "breakdown:placement:feed:cpa"
        )
        self.assertEqual(signal.state, SignalState.HIT)
        self.assertEqual(signal.observed, Decimal(1))
        self.assertIsNotNone(signal.p_value)
        self.assertIsNotNone(signal.adjusted_q_value)
        self.assertLessEqual(signal.adjusted_q_value, Decimal("0.10"))
        self.assertGreater(signal.interval_lower, 0)

    def test_segment_at_or_below_target_cpa_does_not_pass(self):
        from app.scale.evaluator import evaluate_scale
        # Same segment shape, but target_cpa now at/above the segment's
        # actual CPA (2): the segment is not worse than the Target, so it
        # must not contribute to review_delivery_mix.
        segment = self._segment()
        snapshot = _flat_snapshot(
            target=self._target(Decimal("5")), breakdowns=(segment,),
        )
        result = evaluate_scale(snapshot)
        advice_types = {c.advice_type.value for c in result.advice_candidates}
        self.assertNotIn("review_delivery_mix", advice_types)

    def test_breakdown_analysis_skipped_without_target(self):
        from app.scale.evaluator import evaluate_scale
        segment = self._segment()
        snapshot = _flat_snapshot(breakdowns=(segment,))  # no target
        result = evaluate_scale(snapshot)
        advice_types = {c.advice_type.value for c in result.advice_candidates}
        self.assertNotIn("review_delivery_mix", advice_types)
        self.assertFalse(any(s.signal_id.startswith("breakdown:") for s in result.signals))


class TestInputIntegrity(unittest.TestCase):
    def test_negative_metric_rejected(self):
        from app.scale.evaluator import evaluate_scale
        bad_day = _day(CURRENT_START, -1, 5, 0, 0, 0)
        current_daily = (bad_day,) + _uniform_week(CURRENT_START, 2000, 200, 1, 20, 0)[1:]
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        snapshot = _flat_snapshot(current_daily=current_daily, baseline_daily=baseline_daily)
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(snapshot)
        self.assertEqual(ctx.exception.code, "negative_metric")

    def test_clicks_exceed_impressions_rejected(self):
        from app.scale.evaluator import evaluate_scale
        bad_day = _day(CURRENT_START, 10, 50, 0, 0, 0)
        current_daily = (bad_day,) + _uniform_week(CURRENT_START, 2000, 200, 1, 20, 0)[1:]
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 0)
        snapshot = _flat_snapshot(current_daily=current_daily, baseline_daily=baseline_daily)
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(snapshot)
        self.assertEqual(ctx.exception.code, "clicks_exceed_impressions")

    def test_currency_mismatch_rejected(self):
        from app.scale.evaluator import evaluate_scale
        target = ScaleTargetSnapshot(
            target_version_id="target-v1",
            currency="EUR",
            target_cpa=Decimal("25"),
            breakeven_roas=None,
            minimum_economic_spend=Decimal("50"),
            frequency_level_threshold_override=None,
            frequency_growth_threshold_override=None,
            ctr_decline_threshold_override=None,
            cvr_decline_threshold_override=None,
            cpm_increase_threshold_override=None,
            delivery_decline_threshold_override=None,
            cpa_worsening_threshold_override=None,
            roas_worsening_threshold_override=None,
        )
        snapshot = _flat_snapshot(target=target, currency="USD")
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(snapshot)
        self.assertEqual(ctx.exception.code, "currency_mismatch")

    def test_prior_evaluation_without_absence_reason_rejected(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot()
        bad = ScaleEvidenceSnapshot(
            **{**snapshot.__dict__, "prior_streak_absence_reason": None, "prior_streak": None}
        )
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(bad)
        self.assertEqual(ctx.exception.code, "invalid_prior_evaluation")

    def test_naive_freshness_timestamp_rejected(self):
        from app.scale.evaluator import evaluate_scale
        snapshot = _flat_snapshot()
        bad = ScaleEvidenceSnapshot(
            **{**snapshot.__dict__, "last_sync_at": datetime(2026, 9, 6, 1, 0)}
        )
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(bad)
        self.assertEqual(ctx.exception.code, "invalid_freshness_reference")


if __name__ == "__main__":
    unittest.main()
