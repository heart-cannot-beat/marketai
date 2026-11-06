"""ScrapeCreators ``AdLibrarySource`` adapter (#268): wire format and failure classes.

Every test answers through ``httpx.MockTransport``; no request leaves the
process, and no real key is read or used.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.competitors.sources.base import AdLibrarySourceError, AdStatus, PriceOperation, SourceRequest
from app.competitors.sources.scrapecreators import (
    DEFAULT_BASE_URL,
    ScrapeCreatorsAdLibrarySource,
    wire_request,
)

TEST_KEY = "sc-test-only-not-a-real-key"
LISTING = {"success": True, "credits_charged": 1, "credits_remaining": 41, "results": [], "cursor": None}


def _source(handler) -> tuple[ScrapeCreatorsAdLibrarySource, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return ScrapeCreatorsAdLibrarySource(transport=httpx.MockTransport(record)), seen


def _page(source, credential=TEST_KEY.encode(), **kwargs):
    view = memoryview(credential) if credential is not None else None
    return asyncio.run(source.ads_for_page("100000000000010", country="US", credential=view, **kwargs))


def _error(source, **kwargs) -> AdLibrarySourceError:
    with pytest.raises(AdLibrarySourceError) as raised:
        _page(source, **kwargs)
    return raised.value


@pytest.mark.parametrize(
    ("request_", "endpoint", "params"),
    [
        (SourceRequest.search_pages("Acme Charge"), "/v1/facebook/adLibrary/search/companies", {"query": "Acme Charge"}),
        (
            SourceRequest.ads_for_page("100000000000010", country="us"),
            "/v1/facebook/adLibrary/company/ads",
            {"pageId": "100000000000010", "country": "US", "status": "ACTIVE"},
        ),
        (
            SourceRequest.ads_for_page("100000000000010", country="US", cursor="c2", operation="probe"),
            "/v1/facebook/adLibrary/company/ads",
            {"pageId": "100000000000010", "country": "US", "status": "ACTIVE", "cursor": "c2"},
        ),
        (
            SourceRequest.search_ads("NACS adapter", country="US", status=AdStatus.ALL),
            "/v1/facebook/adLibrary/search/ads",
            {"query": "NACS adapter", "country": "US", "status": "ALL"},
        ),
        (SourceRequest.ad_detail("920000000000004"), "/v1/facebook/adLibrary/ad", {"id": "920000000000004"}),
    ],
)
def test_wire_request_is_what_the_p0_run_sent(request_, endpoint, params):
    assert wire_request(request_) == (endpoint, params)


def test_requests_are_validated_before_they_exist():
    with pytest.raises(ValueError):
        SourceRequest.search_pages("  ")
    with pytest.raises(ValueError):
        SourceRequest.ads_for_page("acmecharge", country="US")
    with pytest.raises(ValueError):
        SourceRequest.ads_for_page("100000000000010", country="USA")
    with pytest.raises(ValueError):
        SourceRequest.ads_for_page("100000000000010", country="US", operation=PriceOperation.KEYWORD_PAGE)


def test_one_get_with_the_key_header_and_the_reported_billing():
    source, seen = _source(lambda request: httpx.Response(200, json=LISTING))

    response = _page(source)

    [request] = seen
    assert request.method == "GET"
    assert str(request.url).startswith(DEFAULT_BASE_URL + "/v1/facebook/adLibrary/company/ads?")
    assert dict(request.url.params) == {"pageId": "100000000000010", "country": "US", "status": "ACTIVE"}
    assert request.headers["x-api-key"] == TEST_KEY
    assert (response.credits_charged, response.credits_remaining, response.cached) == (1, 41, False)
    assert response.raw == LISTING and response.next_cursor is None
    assert response.provider == "scrapecreators" and response.operation is PriceOperation.PAGE


def test_a_cache_hit_is_reported_as_such():
    body = dict(LISTING, cached=True, cached_at="2026-10-26T08:00:00Z", credits_charged=0)
    source, _ = _source(lambda request: httpx.Response(200, json=body))

    response = _page(source)

    assert (response.cached, response.credits_charged) == (True, 0)


def test_unreported_credits_stay_unknown():
    source, _ = _source(lambda request: httpx.Response(200, json={"success": True, "results": []}))

    response = _page(source)

    assert (response.credits_charged, response.credits_remaining, response.cached) == (None, None, False)


@pytest.mark.parametrize("credential", [None, b"", b"   ", b"\xff\xfe"])
def test_no_usable_credential_sends_nothing(credential):
    source, seen = _source(lambda request: httpx.Response(200, json=LISTING))

    error = _error(source, credential=credential)

    assert (error.error_code, error.dispatch_certainty, error.retryable) == (
        "credential_unavailable", "not_dispatched", False,
    )
    assert seen == []


@pytest.mark.parametrize(
    ("status", "body", "code", "certainty", "retryable"),
    [
        (402, {"success": False, "message": "out of credits"}, "insufficient_credits", "not_dispatched", False),
        (401, {"success": False}, "credential_rejected", "not_dispatched", False),
        (403, {"success": False}, "credential_rejected", "not_dispatched", False),
        (429, {"success": False}, "rate_limited", "not_dispatched", True),
        (400, {"success": False}, "http_400", "not_dispatched", False),
        (404, None, "http_404", "not_dispatched", False),
        (500, None, "http_500", "uncertain", True),
        (503, {"success": False, "credits_charged": 0}, "http_503", "not_dispatched", True),
        (502, {"success": False, "credits_charged": 1, "credits_remaining": 9}, "http_502", "dispatched", True),
    ],
)
def test_status_failures_are_classified_and_never_retried(status, body, code, certainty, retryable):
    source, seen = _source(
        lambda request: httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="<html>"),
    )

    error = _error(source)

    assert (error.error_code, error.dispatch_certainty, error.retryable) == (code, certainty, retryable)
    assert error.http_status == status and error.raw == body
    assert error.request_params == {"pageId": "100000000000010", "country": "US", "status": "ACTIVE"}
    assert error.latency_ms is not None and len(seen) == 1
    assert "out of credits" not in str(error)


@pytest.mark.parametrize(
    ("exception", "code", "certainty", "retryable"),
    [
        (httpx.ConnectError("refused"), "provider_unreachable", "not_dispatched", True),
        (httpx.ConnectTimeout("slow dns"), "provider_unreachable", "not_dispatched", True),
        (httpx.ReadTimeout("read"), "provider_timeout", "uncertain", False),
        (httpx.RemoteProtocolError("dropped"), "provider_connection_lost", "uncertain", False),
    ],
)
def test_transport_failures_are_classified_without_the_request_attached(exception, code, certainty, retryable):
    def fail(request):
        raise exception

    source, seen = _source(fail)

    error = _error(source)

    assert (error.error_code, error.dispatch_certainty, error.retryable) == (code, certainty, retryable)
    assert error.http_status is None and len(seen) == 1
    # The httpx exception holds the request (and its key header); it is not chained.
    assert error.__cause__ is None and error.__suppress_context__


@pytest.mark.parametrize(
    ("body", "code", "certainty"),
    [
        ("not json", "invalid_response", "uncertain"),
        (["a", "list"], "invalid_response", "uncertain"),
        ({"success": True, "credits_charged": 1}, "invalid_response", "dispatched"),
        ({"success": False, "credits_charged": 1}, "provider_reported_failure", "dispatched"),
        ({"success": False}, "provider_reported_failure", "uncertain"),
    ],
)
def test_a_2xx_without_a_usable_answer_is_an_error(body, code, certainty):
    source, _ = _source(
        lambda request: httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body),
    )

    error = _error(source)

    assert (error.error_code, error.dispatch_certainty, error.retryable) == (code, certainty, False)


def test_page_search_needs_its_search_results():
    source, _ = _source(lambda request: httpx.Response(200, json={"success": True, "results": []}))

    with pytest.raises(AdLibrarySourceError) as raised:
        asyncio.run(source.search_pages("Acme Charge", credential=memoryview(TEST_KEY.encode())))

    assert raised.value.error_code == "invalid_response"


def test_the_key_never_leaves_in_a_response_or_an_error():
    echo = lambda request: httpx.Response(401, json={"success": False, "message": "bad key"})
    source, _ = _source(echo)
    error = _error(source)
    ok_source, _ = _source(lambda request: httpx.Response(200, json=LISTING))
    response = _page(ok_source)

    for text in (
        str(error), repr(error), json.dumps(error.evidence(), default=str),
        repr(response), json.dumps(response.evidence(), default=str),
    ):
        assert TEST_KEY not in text


def test_the_base_url_is_injectable():
    seen: list[httpx.Request] = []
    source = ScrapeCreatorsAdLibrarySource(
        base_url="https://sandbox.example.test/", timeout=5,
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json=LISTING)),
    )

    _page(source)

    assert str(seen[0].url).startswith("https://sandbox.example.test/v1/facebook/adLibrary/company/ads?")
