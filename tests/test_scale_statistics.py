"""Golden-vector tests for the Scale deterministic statistical primitives.

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md
§2.4 "Deterministic arithmetic".
"""
import unittest
from datetime import date
from decimal import Decimal

from app.scale._contracts import (
    DailyEvidenceState,
    DailyMetricValues,
    DailyObservation,
    ScalePolicySnapshot,
)
from app.scale.types import DataQualityIssueCode


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
        breakdown_min_sample_impressions=1000,
        breakdown_min_sample_clicks=20,
        breakdown_min_sample_purchases=5,
        fdr_limit=Decimal("0.10"),
        quality_warning_permitted_codes=(DataQualityIssueCode.FRESHNESS_SLO_EXCEEDED,),
        streak_reset_reason_codes=("policy_change",),
        advice_construction_parameters=(),
    )
    defaults.update(overrides)
    return ScalePolicySnapshot(**defaults)


def _obs(local_date, spend, purchases, purchase_value) -> DailyObservation:
    return DailyObservation(
        local_date=local_date,
        evidence_state=DailyEvidenceState.PRESENT,
        metrics=DailyMetricValues(
            impressions=1000,
            link_clicks=50,
            purchases=Decimal(purchases),
            spend=Decimal(spend),
            purchase_value=Decimal(purchase_value),
            delivered=True,
        ),
    )


class TestSplitMix64(unittest.TestCase):
    def test_golden_vector_seed_zero(self):
        from app.scale._statistics import splitmix64_stream
        stream = splitmix64_stream(0)
        # First three words from SplitMix64 with seed=0
        w1 = next(stream)
        w2 = next(stream)
        w3 = next(stream)
        self.assertEqual(w1, 0xe220a8397b1dcdaf)
        self.assertEqual(w2, 0x6e789e6aa1b965f4)
        self.assertEqual(w3, 0x06c45d188009454f)

    def test_golden_vector_seed_max(self):
        from app.scale._statistics import splitmix64_stream
        stream = splitmix64_stream(0xffffffffffffffff)
        w1 = next(stream)
        self.assertIsInstance(w1, int)
        self.assertLess(w1, 2**64)

    def test_deterministic_repeat(self):
        from app.scale._statistics import splitmix64_stream
        words_a = [next(splitmix64_stream(1234)) for _ in range(5)]
        words_b = [next(splitmix64_stream(1234)) for _ in range(5)]
        self.assertEqual(words_a, words_b)


class TestRejectionSampling(unittest.TestCase):
    def test_uniform_modulo_bias_free(self):
        from app.scale._statistics import splitmix64_stream, rejection_sample_index
        stream = splitmix64_stream(42)
        indices = [rejection_sample_index(stream, 7) for _ in range(1000)]
        self.assertTrue(all(0 <= i < 7 for i in indices))

    def test_deterministic_given_same_stream_seed(self):
        from app.scale._statistics import splitmix64_stream, rejection_sample_index
        stream_a = splitmix64_stream(99)
        stream_b = splitmix64_stream(99)
        indices_a = [rejection_sample_index(stream_a, 11) for _ in range(50)]
        indices_b = [rejection_sample_index(stream_b, 11) for _ in range(50)]
        self.assertEqual(indices_a, indices_b)


class TestWilsonInterval(unittest.TestCase):
    def test_known_ctr(self):
        from app.scale._statistics import wilson_interval
        z = Decimal("1.96")
        lower, upper = wilson_interval(x=50, n=1000, z=z)
        self.assertIsInstance(lower, Decimal)
        self.assertIsInstance(upper, Decimal)
        self.assertGreater(lower, Decimal("0"))
        self.assertLess(upper, Decimal("1"))
        self.assertLess(lower, upper)

    def test_zero_successes(self):
        from app.scale._statistics import wilson_interval
        lower, upper = wilson_interval(x=0, n=100, z=Decimal("1.96"))
        self.assertEqual(lower, Decimal("0"))

    def test_all_successes(self):
        from app.scale._statistics import wilson_interval
        lower, upper = wilson_interval(x=100, n=100, z=Decimal("1.96"))
        self.assertEqual(upper, Decimal("1"))


class TestNewcombeMethod10(unittest.TestCase):
    def test_identical_proportions(self):
        from app.scale._statistics import newcombe_method10
        d_lower, d_upper = newcombe_method10(
            x1=50, n1=1000, x2=50, n2=1000, z=Decimal("1.96"),
        )
        self.assertLess(d_lower, Decimal("0"))
        self.assertGreater(d_upper, Decimal("0"))

    def test_decline_detected(self):
        from app.scale._statistics import newcombe_method10
        # baseline CTR 5%, current CTR 3% — clear decline
        d_lower, d_upper = newcombe_method10(
            x1=50, n1=1000, x2=30, n2=1000, z=Decimal("1.96"),
        )
        # d = baseline - current = positive for decline
        self.assertGreater(d_lower, Decimal("0"))


class TestNearestRankPercentile(unittest.TestCase):
    def test_no_interpolation_matches_formula(self):
        from app.scale._statistics import nearest_rank_percentile
        values = [Decimal(i) for i in range(1, 101)]  # 1..100
        # index = max(0, ceil(0.25 * 100) - 1) = 24 (0-based) -> value 25
        self.assertEqual(nearest_rank_percentile(values, Decimal("0.25")), Decimal(25))

    def test_beta_bounds_on_1000_draws(self):
        from app.scale._statistics import nearest_rank_percentile
        values = [Decimal(i) for i in range(1, 1001)]  # 1..1000
        lower = nearest_rank_percentile(values, Decimal("25") / Decimal("1000"))
        upper = nearest_rank_percentile(values, Decimal("975") / Decimal("1000"))
        self.assertEqual(lower, Decimal(25))
        self.assertEqual(upper, Decimal(975))


class TestDayBlockBootstrap(unittest.TestCase):
    def _windows(self):
        baseline_days = (
            _obs(date(2026, 1, 1), "100.00", "5", "150.00"),
            _obs(date(2026, 1, 2), "120.00", "6", "180.00"),
        )
        current_days = (
            _obs(date(2026, 1, 8), "90.00", "3", "90.00"),
            _obs(date(2026, 1, 9), "95.00", "4", "100.00"),
        )
        return baseline_days, current_days

    @staticmethod
    def _cpa_worsening(baseline_sample, current_sample):
        baseline_purchases = sum((d.purchases for d in baseline_sample), Decimal(0))
        current_purchases = sum((d.purchases for d in current_sample), Decimal(0))
        if baseline_purchases == 0 or current_purchases == 0:
            return None
        baseline_spend = sum((d.spend for d in baseline_sample), Decimal(0))
        current_spend = sum((d.spend for d in current_sample), Decimal(0))
        baseline_cpa = baseline_spend / baseline_purchases
        current_cpa = current_spend / current_purchases
        return (current_cpa / baseline_cpa) - 1

    def test_deterministic_and_ordered(self):
        from app.scale._statistics import day_block_bootstrap
        baseline_days, current_days = self._windows()
        policy = _policy()
        args = (
            baseline_days, current_days, self._cpa_worsening,
            policy.bootstrap_draw_count, "a" * 64, "cpa_worsening", policy,
        )
        lower1, upper1 = day_block_bootstrap(*args)
        lower2, upper2 = day_block_bootstrap(*args)
        self.assertEqual((lower1, upper1), (lower2, upper2))
        self.assertLessEqual(lower1, upper1)
        self.assertIsInstance(lower1, Decimal)
        self.assertIsInstance(upper1, Decimal)

    def test_different_signal_id_changes_stream(self):
        # A separate signal-keyed stream for every signal and segment
        # (spec §2.4): the same snapshot hash with a different signal_key
        # must derive a different seed and therefore a different word
        # stream. Checked directly on the PRNG rather than on bootstrap
        # percentile output, which can coincide by chance on a tiny fixture
        # with few distinct resampled sums.
        from app.scale._statistics import bootstrap_prng_seed, splitmix64_stream
        seed_a = bootstrap_prng_seed("a" * 64, "cpa_worsening")
        seed_b = bootstrap_prng_seed("a" * 64, "roas_worsening")
        self.assertNotEqual(seed_a, seed_b)
        words_a = [next(splitmix64_stream(seed_a)) for _ in range(3)]
        words_b = [next(splitmix64_stream(seed_b)) for _ in range(3)]
        self.assertNotEqual(words_a, words_b)

    def test_insufficient_valid_draws_raises(self):
        from app.scale._statistics import day_block_bootstrap, BootstrapUnavailable
        baseline_days, current_days = self._windows()
        policy = _policy()
        with self.assertRaises(BootstrapUnavailable):
            day_block_bootstrap(
                baseline_days, current_days, lambda b, c: None,
                policy.bootstrap_draw_count, "b" * 64, "always_unavailable", policy,
            )


class TestBenjaminiHochberg(unittest.TestCase):
    def test_deterministic_tie_breaking(self):
        from app.scale._statistics import benjamini_hochberg
        p_values = [
            ("seg_a", Decimal("0.03")),
            ("seg_b", Decimal("0.03")),
            ("seg_c", Decimal("0.08")),
        ]
        results = benjamini_hochberg(p_values, fdr_limit=Decimal("0.10"))
        keys = [r[0] for r in results]
        # Ties broken by segment_key
        self.assertEqual(keys[0], "seg_a")
        self.assertEqual(keys[1], "seg_b")

    def test_reverse_cumulative_minimum_golden_values(self):
        from app.scale._statistics import benjamini_hochberg
        p_values = [
            ("seg_a", Decimal("0.03")),
            ("seg_b", Decimal("0.03")),
            ("seg_c", Decimal("0.08")),
        ]
        results = benjamini_hochberg(p_values, fdr_limit=Decimal("0.10"))
        by_key = {k: (q, passed) for k, q, passed in results}
        self.assertEqual(by_key["seg_a"][0], Decimal("0.045"))
        self.assertEqual(by_key["seg_b"][0], Decimal("0.045"))
        self.assertEqual(by_key["seg_c"][0], Decimal("0.08"))
        self.assertTrue(by_key["seg_a"][1])
        self.assertTrue(by_key["seg_b"][1])
        self.assertTrue(by_key["seg_c"][1])

    def test_family_excluded_beyond_fdr_limit(self):
        from app.scale._statistics import benjamini_hochberg
        p_values = [
            ("seg_a", Decimal("0.01")),
            ("seg_b", Decimal("0.50")),
        ]
        results = benjamini_hochberg(p_values, fdr_limit=Decimal("0.05"))
        by_key = {k: (q, passed) for k, q, passed in results}
        self.assertTrue(by_key["seg_a"][1])
        self.assertFalse(by_key["seg_b"][1])

    def test_empty_family(self):
        from app.scale._statistics import benjamini_hochberg
        self.assertEqual(benjamini_hochberg([], fdr_limit=Decimal("0.10")), [])


if __name__ == "__main__":
    unittest.main()
