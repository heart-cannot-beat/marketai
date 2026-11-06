"""Tests for the ETag / If-Match revision contract (spec §2)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.platform.etag import check_revision_match, extract_if_match, set_etag


def _mock_request(if_match: str | None = None):
    req = MagicMock()
    req.headers = {}
    if if_match is not None:
        req.headers["if-match"] = if_match
    return req


def test_extract_if_match_missing_raises_428():
    with pytest.raises(HTTPException) as exc_info:
        extract_if_match(_mock_request())
    assert exc_info.value.status_code == 428


def test_extract_if_match_malformed_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        extract_if_match(_mock_request("not-a-number"))
    assert exc_info.value.status_code == 400


def test_extract_if_match_valid():
    assert extract_if_match(_mock_request('"42"')) == 42


def test_extract_if_match_unquoted_integer():
    # Strictly should be quoted, but tolerate bare integer
    assert extract_if_match(_mock_request("42")) == 42


def test_check_revision_match_ok():
    check_revision_match(5, 5)  # no exception


def test_check_revision_match_stale():
    with pytest.raises(HTTPException) as exc_info:
        check_revision_match(6, 5)
    assert exc_info.value.status_code == 409


def test_set_etag():
    resp = MagicMock()
    resp.headers = {}
    set_etag(resp, 7)
    assert resp.headers["ETag"] == '"7"'
