"""Shared cursor-based pagination for Page[T] responses (spec §2).

Cursors are opaque, base64-encoded tokens that bind a sort position to the
workspace and filter set that produced it, so a cursor minted under one
tenant or query cannot be replayed against another (``InvalidCursorError``).
"""
from __future__ import annotations

import base64
import hashlib
import json

__all__ = [
    "InvalidCursorError",
    "encode_cursor",
    "decode_cursor",
    "decode_offset_cursor",
    "parse_limit",
]


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor is malformed or does not match the
    workspace/filters it is being replayed against."""


def parse_limit(raw: str | None, *, default: int = 50, maximum: int = 100) -> int:
    """Parse and clamp a ``limit`` query parameter to ``[1, maximum]``.

    ``None`` yields ``default``. A non-numeric string raises ``ValueError``.
    """
    if raw is None:
        return default
    val = int(raw)  # raises ValueError on non-numeric
    return max(1, min(val, maximum))


def _filters_hash(filters_hash: str) -> str:
    return hashlib.sha256(filters_hash.encode()).hexdigest()[:16]


def encode_cursor(workspace_id: str, filters_hash: str, sort_tuple: tuple) -> str:
    """Encode an opaque cursor binding ``sort_tuple`` to ``workspace_id`` and
    ``filters_hash``."""
    payload = json.dumps(
        {
            "w": workspace_id,
            "f": _filters_hash(filters_hash),
            "s": list(sort_tuple),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str, workspace_id: str, filters_hash: str) -> tuple:
    """Decode ``cursor`` and validate it was minted for ``workspace_id`` and
    ``filters_hash``. Raises ``InvalidCursorError`` on any mismatch or
    malformed input."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
    except Exception as exc:
        raise InvalidCursorError(f"Malformed cursor: {exc}") from exc
    if not isinstance(payload, dict) or not {"w", "f", "s"} <= payload.keys():
        raise InvalidCursorError("Malformed cursor: not a cursor payload")
    if not isinstance(payload["s"], list):
        raise InvalidCursorError("Malformed cursor: sort position is not a list")
    if payload["w"] != workspace_id:
        raise InvalidCursorError("Cursor workspace mismatch")
    if payload["f"] != _filters_hash(filters_hash):
        raise InvalidCursorError("Cursor filter mismatch")
    return tuple(payload["s"])


def decode_offset_cursor(cursor: str, workspace_id: str, filters_hash: str) -> int:
    """Decode a cursor minted as ``encode_cursor(..., (offset,))`` to its
    offset. Raises ``InvalidCursorError`` as ``decode_cursor`` does, and when
    the sort position is not one non-negative integer."""
    decoded = decode_cursor(cursor, workspace_id, filters_hash)
    if len(decoded) != 1 or type(decoded[0]) is not int or decoded[0] < 0:
        raise InvalidCursorError("Malformed cursor: offset is not a non-negative integer")
    return decoded[0]
