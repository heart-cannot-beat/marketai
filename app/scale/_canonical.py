"""Versioned canonical serialization and hashing for the Scale evaluator.

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md
§2.6 "Immutable output contracts" and §2.7 "Canonical serialization".

This module accepts only the value shapes the evaluator's frozen value
objects are built from: `None`, `bool`, `int`, `str`, `Enum` (encoded by its
canonical `.value`), `Decimal` (tagged, normalized, non-exponent), `date` and
offset-aware `datetime` (tagged ISO), frozen dataclasses (encoded as objects
keyed by field name), and `tuple`/`list` (encoded as arrays in the given
order — the evaluator's own contracts only ever use `tuple`, never `list`;
`list` and plain `dict` are additionally accepted here so the encoder stays
usable for ad hoc/test payloads). `float`, `set`, non-frozen dataclasses, and
any other object type are rejected with `TypeError`.

`canonical_hash()` prefixes the encoded payload with a caller-supplied domain
separator and this module's canonical-schema version, so distinct payload
families (snapshot hash, compatibility hash, reason signature, statistical
seed, result hash) never collide even on coincidentally identical payloads.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

CANONICAL_SCHEMA_VERSION = "scale-canonical-v1"

_TYPE_KEY = "$type"
_VALUE_KEY = "$value"


def _normalize_decimal(value: Decimal) -> str:
    """Render a Decimal as a fixed-point string: no exponent, no trailing
    zeros, and `-0`/`0.00`/... all collapse to `"0"`."""
    if not value.is_finite():
        raise TypeError(
            f"canonical_encode: Decimal must be finite, got {value!r}"
        )
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text


def _dataclass_fields_canonical(value: Any) -> dict[str, Any]:
    params = getattr(type(value), "__dataclass_params__", None)
    if params is None or not params.frozen:
        raise TypeError(
            "canonical_encode: dataclass instances must be frozen "
            f"(got {type(value)!r})"
        )
    return {
        field.name: _to_canonical(getattr(value, field.name))
        for field in dataclasses.fields(value)
    }


def _to_canonical(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        raise TypeError(
            "canonical_encode: float is not permitted; use Decimal"
        )
    if isinstance(value, Enum):
        return _to_canonical(value.value)
    if isinstance(value, Decimal):
        return {_TYPE_KEY: "decimal", _VALUE_KEY: _normalize_decimal(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(
                "canonical_encode: datetime must be offset-aware"
            )
        return {_TYPE_KEY: "datetime", _VALUE_KEY: value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_KEY: "date", _VALUE_KEY: value.isoformat()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _dataclass_fields_canonical(value)
    if isinstance(value, (tuple, list)):
        return [_to_canonical(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "canonical_encode: mapping keys must be str, "
                    f"got {type(key)!r}"
                )
            out[key] = _to_canonical(item)
        return out
    raise TypeError(f"canonical_encode: unsupported type {type(value)!r}")


def canonical_encode(value: Any) -> str:
    """Encode `value` as a canonical JSON string: sorted object keys, fixed
    separators, UTF-8 without locale-sensitive transforms, no float/NaN/inf."""
    structure = _to_canonical(value)
    return json.dumps(
        structure,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_hash(
    value: Any,
    *,
    domain: str,
    schema_version: str = CANONICAL_SCHEMA_VERSION,
) -> str:
    """SHA-256 hex digest of `value`'s canonical encoding, prefixed with a
    domain separator and the canonical-schema version so distinct payload
    families never collide on coincidentally identical content."""
    payload = canonical_encode(value)
    message = "\x00".join((domain, schema_version, payload))
    return hashlib.sha256(message.encode("utf-8")).hexdigest()
