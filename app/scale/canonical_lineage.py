"""Versioned canonical hash for Scale Enrollment lineage (spec §4.7).

``canonical_lineage_hash(version, mapping)`` is the shared primitive behind
every lineage fingerprint in the hardened Scale Enrollment design --
``winner_fingerprint`` (§4.7.1), ``candidate_fingerprint`` (§4.7.2),
``lineage_hash`` (§4.7.3), ``target_fingerprint`` and
``source_manifest_hash`` (§4.3). Each caller picks its own small integer
schema version (bumped whenever that hash's field set changes); the version
is folded into the hashed envelope so two mappings that are otherwise
identical never collide across schema versions.

Extends ``app.platform.canonical``'s evidence-hash rules (NFC-normalized
strings, RFC 3339 UTC timestamps with microsecond precision, normalized
Decimal, no binary floats) to every value in the mapping -- not just the
top-level dict, matching §4.7's "UTF-8 JSON, keys sorted recursively,
separators (',', ':'), NFC-normalized strings" rule.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.platform.canonical import _canonical_decimal_str

__all__ = ["canonical_lineage_hash"]


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _lineage_default(obj: Any) -> Any:
    if isinstance(obj, float):
        raise TypeError(f"Binary float not allowed: {obj!r}")
    if isinstance(obj, Decimal):
        return _canonical_decimal_str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError(f"Naive datetime: {obj!r}")
        utc = obj.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(obj, str):
        return _nfc(obj)
    raise TypeError(f"Unserializable: {type(obj).__name__}")


def _normalize_strings(obj: Any) -> Any:
    """Recursively NFC-normalize every string, including dict keys, and
    reject binary floats up front.

    ``json.dumps``'s ``default`` hook only fires for values it cannot encode
    natively -- plain strings and floats never reach ``_lineage_default``
    (the ``json`` module encodes both itself). Walking the structure first is
    what makes ``test_nfc_normalization`` pass for a top-level string value
    rather than only one nested inside a non-native type, and what makes a
    top-level (not just nested) float actually raise instead of silently
    serializing as a JSON number.
    """
    if isinstance(obj, float):
        raise TypeError(f"Binary float not allowed: {obj!r}")
    if isinstance(obj, str):
        return _nfc(obj)
    if isinstance(obj, dict):
        return {_nfc(k): _normalize_strings(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_strings(i) for i in obj]
    return obj


def canonical_lineage_hash(version: int, mapping: dict) -> str:
    """SHA-256 hex digest of ``mapping`` under a version-prefixed envelope.

    Deterministic and cross-call stable: the same ``(version, mapping)``
    always produces the same digest, regardless of key insertion order,
    Unicode normalization form, Decimal exponent/trailing-zero form, or
    datetime timezone offset (naive datetimes are rejected, matching
    ``app.platform.canonical``).
    """
    normalized = _normalize_strings(mapping)
    envelope = {"v": version, "d": normalized}
    raw = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=_lineage_default,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
