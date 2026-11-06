"""Tests for the canonical JSON evidence encoder (§5.3.1–§5.3.5)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from app.platform.canonical import (
    _canonical_decimal_str,
    _canonical_json,
    _canonical_bytes,
    _normalize_legacy_numerics,
    _sha256,
)


# ── §5.3.1 Canonical JSON encoder ──────────────────────────────────────


class TestCanonicalJson:
    def test_ce01_key_sorting(self):
        assert _canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_ce02_decimal_trailing_zeros(self):
        assert _canonical_json({"a": Decimal("1.230000")}) == '{"a":"1.23"}'

    def test_ce03_decimal_no_exponent(self):
        assert _canonical_json({"a": Decimal("1000")}) == '{"a":"1000"}'

    def test_ce04_decimal_negative_zero(self):
        assert _canonical_json({"a": Decimal("-0")}) == '{"a":"0"}'

    def test_ce05_decimal_small(self):
        assert _canonical_json({"a": Decimal("0.001")}) == '{"a":"0.001"}'

    def test_ce06_float_rejected(self):
        with pytest.raises(TypeError):
            _canonical_json(1.5)

    def test_ce07_nested_float_rejected(self):
        with pytest.raises(TypeError):
            _canonical_json({"a": [1.5]})

    def test_ce08_nonstring_key_rejected(self):
        with pytest.raises(TypeError):
            _canonical_json({1: "v"})

    def test_ce09_string_key_accepted(self):
        assert _canonical_json({"a": "v"}) == '{"a":"v"}'

    def test_ce10_datetime_utc_microseconds(self):
        dt = datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
        assert _canonical_json({"a": dt}) == '{"a":"2024-01-01T12:00:00.123456Z"}'

    def test_ce11_datetime_offset_normalized_to_utc(self):
        tz_plus2 = timezone(timedelta(hours=2))
        dt = datetime(2024, 1, 1, 14, 0, 0, 0, tzinfo=tz_plus2)
        assert _canonical_json({"a": dt}) == '{"a":"2024-01-01T12:00:00.000000Z"}'

    def test_ce12_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            _canonical_json({"a": datetime(2024, 1, 1, 12, 0, 0)})

    def test_ce13_tuple_to_list(self):
        assert _canonical_json({"a": (1, 2, 3)}) == '{"a":[1,2,3]}'

    def test_ce14_decimal_infinity_rejected(self):
        with pytest.raises(ValueError):
            _canonical_json({"a": Decimal("inf")})

    def test_ce15_decimal_nan_rejected(self):
        with pytest.raises(ValueError):
            _canonical_json({"a": Decimal("NaN")})

    def test_ce16_unserializable_rejected(self):
        with pytest.raises(TypeError):
            _canonical_json({"a": object()})


# ── §5.3.2 _canonical_decimal_str property-style tests ─────────────────


class TestCanonicalDecimalStr:
    @pytest.mark.parametrize("d", [
        Decimal("0"), Decimal("1"), Decimal("-1"), Decimal("1000"),
        Decimal("0.001"), Decimal("1.230000"), Decimal("-0"),
        Decimal("99999999999999999999"), Decimal("0.000001"),
        Decimal("1E+10"), Decimal("1.0E-5"),
    ])
    def test_cdp1_no_exponent_notation(self, d):
        result = _canonical_decimal_str(d)
        assert "E" not in result and "e" not in result

    @pytest.mark.parametrize("d", [
        Decimal("0"), Decimal("1"), Decimal("-1"), Decimal("1000"),
        Decimal("0.001"), Decimal("1.230000"), Decimal("-0"),
        Decimal("99999999999999999999"), Decimal("0.000001"),
    ])
    def test_cdp2_roundtrip(self, d):
        result = _canonical_decimal_str(d)
        assert Decimal(result) == d

    @pytest.mark.parametrize("d", [
        Decimal("-0"), Decimal("-0.0"), Decimal("-0.00"),
    ])
    def test_cdp3_no_negative_zero(self, d):
        result = _canonical_decimal_str(d)
        assert not result.startswith("-")
        assert result == "0"

    @pytest.mark.parametrize("d,expected", [
        (Decimal("1.230000"), "1.23"),
        (Decimal("1.00"), "1"),
        (Decimal("0.10"), "0.1"),
    ])
    def test_cdp4_no_trailing_fractional_zeros(self, d, expected):
        assert _canonical_decimal_str(d) == expected


# ── §5.3.3 _normalize_legacy_numerics ──────────────────────────────────


class TestNormalizeLegacyNumerics:
    def test_nl01_recursive_float_to_decimal(self):
        result = _normalize_legacy_numerics({"a": 1.5, "b": [2.0, {"c": 3.5}]})
        assert result == {"a": Decimal("1.5"), "b": [Decimal("2.0"), {"c": Decimal("3.5")}]}
        assert isinstance(result["a"], Decimal)
        assert isinstance(result["b"][0], Decimal)
        assert isinstance(result["b"][1]["c"], Decimal)

    def test_nl02_non_floats_unchanged(self):
        original = {"a": "text", "b": 42, "c": None}
        assert _normalize_legacy_numerics(original) == original

    def test_nl03_large_float_no_overflow(self):
        result = _normalize_legacy_numerics({"a": 1e308})
        assert isinstance(result["a"], Decimal)


# ── §5.3.4 Hash determinism ────────────────────────────────────────────


class TestHashDeterminism:
    def test_hd01_cross_dialect_hash_from_vector(self):
        vector = {
            "z_string": "hello",
            "a_int": 42,
            "b_decimal": Decimal("1.23"),
            "c_none": None,
            "d_list": [1, 2, 3],
            "e_nested": {"x": Decimal("0.001")},
            "f_datetime": datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc),
        }
        h1 = _sha256(_canonical_bytes(vector))
        h2 = _sha256(_canonical_bytes(vector))
        assert h1 == h2
        assert len(h1) == 64

    def test_hd08_duplicate_group_hash_deterministic(self):
        inputs = ("dimension_key_1", "concept_key_1")
        h1 = _sha256(_canonical_bytes(list(inputs)))
        h2 = _sha256(_canonical_bytes(list(inputs)))
        assert h1 == h2


# ── §5.3.5 Eligibility-context hash sensitivity ───────────────────────


class TestEligibilityContextHashSensitivity:
    @staticmethod
    def _ech(frozen_hash, proj_head, candidates, recheck_hash):
        return _sha256(_canonical_bytes([frozen_hash, proj_head, sorted(candidates), recheck_hash]))

    def test_ech01_frozen_hash_change(self):
        h1 = self._ech("aaa", "bbb", [("c1", "h1")], "ddd")
        h2 = self._ech("AAA", "bbb", [("c1", "h1")], "ddd")
        assert h1 != h2

    def test_ech02_proj_head_change(self):
        h1 = self._ech("aaa", "bbb", [("c1", "h1")], "ddd")
        h2 = self._ech("aaa", "BBB", [("c1", "h1")], "ddd")
        assert h1 != h2

    def test_ech03_recheck_change(self):
        h1 = self._ech("aaa", "bbb", [("c1", "h1")], "ddd")
        h2 = self._ech("aaa", "bbb", [("c1", "h1")], "DDD")
        assert h1 != h2

    def test_ech04_add_candidate(self):
        h1 = self._ech("aaa", "bbb", [("c1", "h1")], "ddd")
        h2 = self._ech("aaa", "bbb", [("c1", "h1"), ("c2", "h2")], "ddd")
        assert h1 != h2

    def test_ech05_remove_candidate(self):
        h1 = self._ech("aaa", "bbb", [("c1", "h1"), ("c2", "h2")], "ddd")
        h2 = self._ech("aaa", "bbb", [("c1", "h1")], "ddd")
        assert h1 != h2

    def test_ech06_swap_candidate_hashes(self):
        h1 = self._ech("aaa", "bbb", [("c1", "h1"), ("c2", "h2")], "ddd")
        h2 = self._ech("aaa", "bbb", [("c1", "h2"), ("c2", "h1")], "ddd")
        assert h1 != h2

    def test_ech07_ordering_invariance(self):
        h1 = self._ech("aaa", "bbb", [("c2", "h2"), ("c1", "h1")], "ddd")
        h2 = self._ech("aaa", "bbb", [("c1", "h1"), ("c2", "h2")], "ddd")
        assert h1 == h2
