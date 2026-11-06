import unittest
from decimal import Decimal
from datetime import date, datetime, timezone


class TestCanonicalEncoding(unittest.TestCase):
    def test_decimal_removes_trailing_zeros(self):
        from app.scale._canonical import canonical_encode
        self.assertIn('"1.5"', canonical_encode(Decimal("1.500")))

    def test_decimal_negative_zero_becomes_zero(self):
        from app.scale._canonical import canonical_encode
        self.assertIn('"0"', canonical_encode(Decimal("-0")))

    def test_decimal_no_exponent_notation(self):
        from app.scale._canonical import canonical_encode
        result = canonical_encode(Decimal("1E+3"))
        self.assertNotIn("E", result)

    def test_domain_separator(self):
        from app.scale._canonical import canonical_hash
        h1 = canonical_hash({"a": 1}, domain="scale-evidence-snapshot-v1")
        h2 = canonical_hash({"a": 1}, domain="scale-evaluation-result-v1")
        self.assertNotEqual(h1, h2)

    def test_float_rejected(self):
        from app.scale._canonical import canonical_encode
        with self.assertRaises(TypeError):
            canonical_encode(1.5)

    def test_enum_encoded_by_value(self):
        from app.scale._canonical import canonical_encode
        from app.scale.types import MetaMode
        result = canonical_encode(MetaMode.CUSTOMER_SANDBOX)
        self.assertIn('"customer_sandbox"', result)

    def test_frozen_dataclass_round_trip(self):
        from app.scale._contracts import DailyMetricValues
        from app.scale._canonical import canonical_encode
        v = DailyMetricValues(
            impressions=1000, link_clicks=50,
            purchases=Decimal("3"), spend=Decimal("100.00"),
            purchase_value=Decimal("300.00"), delivered=True,
        )
        result = canonical_encode(v)
        self.assertIn('"impressions"', result)

    # --- Additional coverage beyond the brief's minimal set ---

    def test_decimal_no_trailing_dot_for_integral_value(self):
        from app.scale._canonical import canonical_encode
        self.assertIn('"100"', canonical_encode(Decimal("100.00")))
        self.assertNotIn('"100."', canonical_encode(Decimal("100.00")))

    def test_decimal_zero_dot_variants_collapse_to_zero(self):
        from app.scale._canonical import canonical_encode
        for spelling in (Decimal("0"), Decimal("0.00"), Decimal("-0.0")):
            self.assertIn('"0"', canonical_encode(spelling))

    def test_negative_decimal_preserved(self):
        from app.scale._canonical import canonical_encode
        self.assertIn('"-1.25"', canonical_encode(Decimal("-1.250")))

    def test_bool_is_not_encoded_as_int_zero_or_one(self):
        from app.scale._canonical import canonical_encode
        self.assertEqual(canonical_encode(True), "true")
        self.assertEqual(canonical_encode(False), "false")

    def test_none_encoded_as_null(self):
        from app.scale._canonical import canonical_encode
        self.assertEqual(canonical_encode(None), "null")

    def test_date_encoded_as_iso_string(self):
        from app.scale._canonical import canonical_encode
        result = canonical_encode(date(2026, 9, 8))
        self.assertIn('"2026-09-08"', result)

    def test_naive_datetime_rejected(self):
        from app.scale._canonical import canonical_encode
        with self.assertRaises(TypeError):
            canonical_encode(datetime(2026, 9, 8, 12, 0, 0))

    def test_offset_aware_datetime_encoded(self):
        from app.scale._canonical import canonical_encode
        result = canonical_encode(datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc))
        self.assertIn("2026-09-08T12:00:00", result)

    def test_tuple_order_preserved_not_sorted(self):
        from app.scale._canonical import canonical_encode
        result = canonical_encode(("b", "a", "c"))
        self.assertEqual(result, '["b","a","c"]')

    def test_object_keys_are_sorted(self):
        from app.scale._canonical import canonical_encode
        result = canonical_encode({"z": 1, "a": 2})
        self.assertLess(result.index('"a"'), result.index('"z"'))

    def test_set_rejected(self):
        from app.scale._canonical import canonical_encode
        with self.assertRaises(TypeError):
            canonical_encode({1, 2, 3})

    def test_non_frozen_dataclass_rejected(self):
        from dataclasses import dataclass
        from app.scale._canonical import canonical_encode

        @dataclass
        class Mutable:
            x: int

        with self.assertRaises(TypeError):
            canonical_encode(Mutable(x=1))

    def test_repeated_encoding_is_byte_identical(self):
        from app.scale._contracts import DailyMetricValues
        from app.scale._canonical import canonical_encode
        v = DailyMetricValues(
            impressions=1000, link_clicks=50,
            purchases=Decimal("3"), spend=Decimal("100.00"),
            purchase_value=Decimal("300.00"), delivered=True,
        )
        self.assertEqual(canonical_encode(v), canonical_encode(v))

    def test_canonical_hash_is_hex_sha256(self):
        from app.scale._canonical import canonical_hash
        h = canonical_hash({"a": 1}, domain="scale-evidence-snapshot-v1")
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises ValueError if not hex

    def test_canonical_hash_stable_for_same_payload_and_domain(self):
        from app.scale._canonical import canonical_hash
        h1 = canonical_hash({"a": 1}, domain="scale-evidence-snapshot-v1")
        h2 = canonical_hash({"a": 1}, domain="scale-evidence-snapshot-v1")
        self.assertEqual(h1, h2)

    def test_canonical_hash_changes_with_schema_version(self):
        from app.scale._canonical import canonical_hash
        h1 = canonical_hash({"a": 1}, domain="d", schema_version="v1")
        h2 = canonical_hash({"a": 1}, domain="d", schema_version="v2")
        self.assertNotEqual(h1, h2)


class TestScaleContracts(unittest.TestCase):
    def test_all_contract_dataclasses_are_frozen(self):
        import dataclasses
        from app.scale import _contracts

        checked = 0
        for name in dir(_contracts):
            obj = getattr(_contracts, name)
            if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                params = obj.__dataclass_params__
                self.assertTrue(params.frozen, f"{name} must be frozen")
                checked += 1
        self.assertGreater(checked, 15)

    def test_invalid_scale_snapshot_carries_stable_code(self):
        from app.scale._contracts import InvalidScaleSnapshot
        err = InvalidScaleSnapshot("currency_mismatch", "expected USD got EUR")
        self.assertEqual(err.code, "currency_mismatch")
        self.assertIn("currency_mismatch", str(err))
        self.assertIsInstance(err, ValueError)

    def test_invalid_scale_snapshot_without_detail(self):
        from app.scale._contracts import InvalidScaleSnapshot
        err = InvalidScaleSnapshot("negative_metric")
        self.assertEqual(str(err), "negative_metric")

    def test_daily_observation_frozen_and_immutable(self):
        from app.scale._contracts import DailyObservation
        from app.scale.types import DailyEvidenceState
        obs = DailyObservation(
            local_date=date(2026, 9, 1),
            evidence_state=DailyEvidenceState.MISSING,
            metrics=None,
        )
        with self.assertRaises(Exception):
            obs.local_date = date(2026, 9, 2)  # type: ignore[misc]

    def test_scale_evaluation_hashes_deterministically(self):
        from app.scale._contracts import (
            ScaleEvaluation,
            StreakCompatibility,
            StreakState,
        )
        from app.scale._canonical import canonical_hash
        from app.scale.types import EvidenceMaturity, DataQualityState

        compat = StreakCompatibility(
            policy_version_id="pol_1",
            target_version_id=None,
            metric_contract_set_hash="a" * 64,
            configuration_baseline_hash="b" * 64,
            data_continuity_generation="gen1",
            canonical_hash="c" * 64,
        )
        streak_state = StreakState(
            compatibility=compat,
            healthy_evaluations=0,
            counters=(),
            resets=(),
        )
        evaluation = ScaleEvaluation(
            snapshot_canonical_hash="d" * 64,
            algorithm_version="beta-v1",
            maturity=EvidenceMaturity.WARMING,
            data_quality_state=DataQualityState.PASS,
            health=None,
            health_reason="",
            signals=(),
            advice_candidates=(),
            streak_state=streak_state,
            result_hash="",
        )
        h1 = canonical_hash(evaluation, domain="scale-evaluation-result-v1")
        h2 = canonical_hash(evaluation, domain="scale-evaluation-result-v1")
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
