"""Tests for cursor-based pagination primitives (spec §2)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from app.platform.pagination import (
    InvalidCursorError,
    decode_cursor,
    decode_keyset_cursor,
    decode_offset_cursor,
    encode_cursor,
    parse_limit,
)


def test_parse_limit_default():
    assert parse_limit(None) == 50


def test_parse_limit_custom():
    assert parse_limit("25") == 25


def test_parse_limit_clamped_to_max():
    assert parse_limit("200") == 100


def test_parse_limit_clamped_to_min():
    assert parse_limit("0") == 1


def test_parse_limit_invalid_string():
    with pytest.raises(ValueError):
        parse_limit("abc")


def test_cursor_round_trip():
    ws = "ws_123"
    fh = "abc"
    sort_tuple = ("2026-01-01T00:00:00", "id_456")
    cursor = encode_cursor(ws, fh, sort_tuple)
    assert isinstance(cursor, str)
    decoded = decode_cursor(cursor, ws, fh)
    assert decoded == sort_tuple


def test_cursor_wrong_workspace():
    cursor = encode_cursor("ws_a", "fh", ("x",))
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "ws_b", "fh")


def test_cursor_wrong_filters():
    cursor = encode_cursor("ws_a", "fh1", ("x",))
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "ws_a", "fh2")


def test_cursor_malformed():
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-a-valid-cursor", "ws", "fh")


def _raw_cursor(payload) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _minted_payload(ws: str = "ws", fh: str = "fh") -> dict:
    return json.loads(base64.urlsafe_b64decode(encode_cursor(ws, fh, (1,))))


@pytest.mark.parametrize("payload", [1, "s", None, [1], ["ws", "fh", [1]]])
def test_cursor_payload_not_an_object(payload):
    with pytest.raises(InvalidCursorError):
        decode_cursor(_raw_cursor(payload), "ws", "fh")


@pytest.mark.parametrize("key", ["w", "f", "s"])
def test_cursor_payload_missing_a_key(key):
    payload = _minted_payload()
    del payload[key]
    with pytest.raises(InvalidCursorError):
        decode_cursor(_raw_cursor(payload), "ws", "fh")


@pytest.mark.parametrize("sort", [5, "x", None, {"0": 1}])
def test_cursor_sort_not_a_list(sort):
    with pytest.raises(InvalidCursorError):
        decode_cursor(_raw_cursor({**_minted_payload(), "s": sort}), "ws", "fh")


def test_cursor_not_ascii():
    with pytest.raises(InvalidCursorError):
        decode_cursor("été", "ws", "fh")


def test_offset_cursor_round_trip():
    assert decode_offset_cursor(encode_cursor("ws", "fh", (0,)), "ws", "fh") == 0
    assert decode_offset_cursor(encode_cursor("ws", "fh", (50,)), "ws", "fh") == 50


@pytest.mark.parametrize("sort", [(), ("x",), ("5",), (-1,), (True,), (1.5,), (None,), (1, 2)])
def test_offset_cursor_not_one_non_negative_integer(sort):
    with pytest.raises(InvalidCursorError):
        decode_offset_cursor(encode_cursor("ws", "fh", sort), "ws", "fh")


def test_offset_cursor_still_checks_workspace_and_filters():
    cursor = encode_cursor("ws_a", "fh", (1,))
    with pytest.raises(InvalidCursorError):
        decode_offset_cursor(cursor, "ws_b", "fh")
    with pytest.raises(InvalidCursorError):
        decode_offset_cursor(cursor, "ws_a", "other")


_STAMP = "2026-01-02T03:04:05.123456+00:00"


def test_keyset_cursor_round_trip():
    cursor = encode_cursor("ws", "fh", (_STAMP, "ver_1"))
    assert decode_keyset_cursor(cursor, "ws", "fh") == (
        datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc), "ver_1",
    )


def test_keyset_cursor_keeps_a_naive_timestamp_naive():
    cursor = encode_cursor("ws", "fh", ("2026-01-02T03:04:05", "v" * 64))
    assert decode_keyset_cursor(cursor, "ws", "fh") == (datetime(2026, 1, 2, 3, 4, 5), "v" * 64)


@pytest.mark.parametrize("sort", [
    (),
    (_STAMP,),
    (_STAMP, "ver_1", "extra"),
    ("yesterday", "ver_1"),
    ("", "ver_1"),
    (1767225600, "ver_1"),
    (None, "ver_1"),
    (_STAMP, 5),
    (_STAMP, None),
    (_STAMP, True),
    (_STAMP, ""),
    (_STAMP, "v" * 65),
    ("ver_1", _STAMP),
])
def test_keyset_cursor_not_an_iso_time_and_an_id(sort):
    with pytest.raises(InvalidCursorError):
        decode_keyset_cursor(encode_cursor("ws", "fh", sort), "ws", "fh")


def test_keyset_cursor_still_checks_workspace_and_filters():
    cursor = encode_cursor("ws_a", "fh", (_STAMP, "ver_1"))
    with pytest.raises(InvalidCursorError):
        decode_keyset_cursor(cursor, "ws_b", "fh")
    with pytest.raises(InvalidCursorError):
        decode_keyset_cursor(cursor, "ws_a", "other")
