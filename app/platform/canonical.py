"""Canonical JSON serialization and evidence hashing (§5.3).

Deterministic, cross-dialect serializer for immutable evidence snapshots.
Rejects binary floats, naive datetimes, non-finite Decimals, and non-string
dict keys. Normalizes Decimals (no exponent, no trailing zeros, no negative
zero) and datetimes (UTC with microsecond precision, Z suffix).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "_canonical_decimal_str",
    "_canonical_json",
    "_canonical_bytes",
    "_normalize_legacy_numerics",
    "_sha256",
    "canonical_hash",
]


def _canonical_decimal_str(d: Decimal) -> str:
    if not d.is_finite():
        raise ValueError(f"Non-finite Decimal: {d!r}")
    if d == 0:
        return "0"
    sign, digits, exponent = d.as_tuple()
    num_digits = len(digits)
    if exponent >= 0:
        int_str = "".join(str(dig) for dig in digits) + "0" * exponent
    else:
        abs_exp = -exponent
        if abs_exp >= num_digits:
            int_str = "0"
            frac_digits = "0" * (abs_exp - num_digits) + "".join(str(dig) for dig in digits)
        else:
            int_str = "".join(str(dig) for dig in digits[:num_digits - abs_exp])
            frac_digits = "".join(str(dig) for dig in digits[num_digits - abs_exp:])
        frac_digits = frac_digits.rstrip("0")
        if frac_digits:
            result = f"{int_str}.{frac_digits}"
            return f"-{result}" if sign else result
    return f"-{int_str}" if sign else int_str


def _evidence_default(obj: Any) -> Any:
    if isinstance(obj, float):
        raise TypeError(f"Binary float not allowed in evidence: {obj!r}")
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError(f"Non-finite Decimal: {obj!r}")
        return _canonical_decimal_str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError(f"Naive datetime not allowed: {obj!r}")
        utc = obj.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"Unserializable type: {type(obj).__name__}")


class _StrictEncoder(json.JSONEncoder):
    def encode(self, o: Any) -> str:
        if isinstance(o, dict):
            for key in o:
                if not isinstance(key, str):
                    raise TypeError(f"Non-string dict key: {type(key).__name__}")
        return super().encode(o)

    def default(self, o: Any) -> Any:
        return _evidence_default(o)

    def iterencode(self, o: Any, _one_shot: bool = False) -> Any:
        if isinstance(o, float):
            raise TypeError(f"Binary float not allowed in evidence: {o!r}")
        if isinstance(o, dict):
            for key in o:
                if not isinstance(key, str):
                    raise TypeError(f"Non-string dict key: {type(key).__name__}")
        return super().iterencode(o, _one_shot)


_encoder = _StrictEncoder(
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)


def _check_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise TypeError(f"Binary float not allowed in evidence: {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"Non-string dict key: {type(k).__name__}")
            _check_floats(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _check_floats(item)


def _canonical_json(value: Any) -> str:
    _check_floats(value)
    return _encoder.encode(value)


def _canonical_bytes(value: Any) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_legacy_numerics(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _normalize_legacy_numerics(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_legacy_numerics(item) for item in obj]
    return obj


def canonical_hash(value: Any) -> str:
    """Backward-compatible canonical hash for non-evidence uses.

    Uses ``default=str`` so it accepts types the strict encoder rejects.
    Existing call sites (judge, library, generation, store) use this.
    """
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
