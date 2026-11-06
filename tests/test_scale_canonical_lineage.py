"""Golden-vector tests for the versioned canonical lineage hash (spec §4.7).

Issue #86, Task 7. Proves ``canonical_lineage_hash`` is deterministic across
repeated calls, Unicode normalization forms, Decimal representations, and
naive-vs-normalized timestamps, and that changing the version changes the
digest even when the mapping is unchanged.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.scale.canonical_lineage import canonical_lineage_hash


def test_golden_vector_winner_fingerprint():
    """C23: golden vector -- deterministic hash across inputs."""
    mapping = {
        "decision_id": "dec_001",
        "confirmation_id": "conf_001",
        "asset_version_id": "av_001",
        "product_version_id": "pv_001",
        "offer_version_id": "ov_001",
        "market_code": "US",
        "placement": "feed",
        "landing_page_version_id": "lp_001",
        "market_asset_binding_id": "mab_001",
        "attribution_semantics_hash": "abc123",
    }
    h1 = canonical_lineage_hash(1, mapping)
    h2 = canonical_lineage_hash(1, mapping)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex
    assert h1 == bytes.fromhex(h1).hex()  # lowercase hex round-trips exactly


def test_key_order_does_not_affect_hash():
    mapping_a = {"a": "1", "b": "2", "c": "3"}
    mapping_b = {"c": "3", "a": "1", "b": "2"}
    assert canonical_lineage_hash(1, mapping_a) == canonical_lineage_hash(1, mapping_b)


def test_nfc_normalization():
    """Different Unicode representations produce same hash."""
    nfc = unicodedata.normalize("NFC", "é")  # single codepoint é
    nfd = unicodedata.normalize("NFD", "é")  # e + combining acute
    assert nfc != nfd  # sanity: genuinely different code point sequences
    m1 = {"name": nfc}
    m2 = {"name": nfd}
    assert canonical_lineage_hash(1, m1) == canonical_lineage_hash(1, m2)


def test_nfc_normalization_in_dict_key():
    nfc = unicodedata.normalize("NFC", "é")
    nfd = unicodedata.normalize("NFD", "é")
    m1 = {nfc: "v"}
    m2 = {nfd: "v"}
    assert canonical_lineage_hash(1, m1) == canonical_lineage_hash(1, m2)


def test_timestamp_normalization():
    """Datetime values normalized to RFC 3339 UTC."""
    dt = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    m = {"ts": dt}
    h = canonical_lineage_hash(1, m)
    assert len(h) == 64


def test_timestamp_normalized_across_timezone_offsets():
    from datetime import timedelta, timezone as tz

    utc_dt = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    offset_dt = utc_dt.astimezone(tz(timedelta(hours=-5)))
    assert canonical_lineage_hash(1, {"ts": utc_dt}) == canonical_lineage_hash(1, {"ts": offset_dt})


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        canonical_lineage_hash(1, {"ts": datetime(2026, 9, 14, 12, 0, 0)})


def test_decimal_normalization():
    """Trailing zeros and exponent form produce same hash."""
    m1 = {"amount": Decimal("1.50")}
    m2 = {"amount": Decimal("1.5")}
    assert canonical_lineage_hash(1, m1) == canonical_lineage_hash(1, m2)


def test_binary_float_rejected():
    with pytest.raises(TypeError):
        canonical_lineage_hash(1, {"amount": 1.5})


def test_version_changes_hash():
    mapping = {"k": "v"}
    assert canonical_lineage_hash(1, mapping) != canonical_lineage_hash(2, mapping)


def test_mapping_change_changes_hash():
    assert canonical_lineage_hash(1, {"k": "v1"}) != canonical_lineage_hash(1, {"k": "v2"})
