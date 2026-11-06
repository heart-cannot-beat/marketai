"""Shared contract for ``AdLibrarySource`` (#268): Controlled Mock and ScrapeCreators.

Both implementations get the same calls with the same inputs and must give
the same output shape. The real adapter talks HTTP to an in-process httpx
transport that serves exactly what the Controlled Mock answers, so beyond
the shape the two must agree on every value except the provider name and
the measured latency. No test here opens a socket.
"""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

import httpx
import pytest

from app.competitors.normalize import normalize_ads, normalize_page_candidates, page_origin
from app.competitors.sources.base import (
    BILLING_UNIT_CREDIT,
    METHOD_OPERATIONS,
    PLATFORM_META,
    AdLibrarySource,
    AdLibrarySourceError,
    PriceOperation,
    SourceMethod,
    SourceRequest,
    SourceResponse,
)
from app.competitors.sources.mock import MockAdLibrarySource
from app.competitors.sources.scrapecreators import ENDPOINTS, ScrapeCreatorsAdLibrarySource

TEST_KEY = "sc-test-only-not-a-real-key"
CLOCK = datetime(2026, 10, 26, 8, 31, 58, tzinfo=timezone.utc)
ACME = "100000000000010"
BRIGHTVOLT = "100000000000016"
ACME_DCO_AD = "920000000000004"
#: A Acme Charge same-name candidate with no recorded ads: the provider's empty page.
ACME_ISRAEL = "100000000000031"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


_WIRE_METHODS = {path: method for method, path in ENDPOINTS.items()}


def _request_from_wire(request: httpx.Request) -> SourceRequest:
    method, params = _WIRE_METHODS[request.url.path], dict(request.url.params)
    if method is SourceMethod.SEARCH_PAGES:
        return SourceRequest.search_pages(params["query"])
    if method is SourceMethod.ADS_FOR_PAGE:
        return SourceRequest.ads_for_page(
            params["pageId"], country=params["country"], status=params["status"], cursor=params.get("cursor"),
        )
    if method is SourceMethod.SEARCH_ADS:
        return SourceRequest.search_ads(
            params["query"], country=params["country"], status=params["status"], cursor=params.get("cursor"),
        )
    return SourceRequest.ad_detail(params["id"])


class ScrapeCreatorsReplay(httpx.AsyncBaseTransport):
    """Answers over HTTP exactly what the Controlled Mock answers, as ScrapeCreators would."""

    def __init__(self, engine: MockAdLibrarySource) -> None:
        self.engine = engine
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET" or request.headers.get("x-api-key") != TEST_KEY:
            return httpx.Response(401, json={"success": False, "message": "Unauthorized"})
        try:
            answer = await self.engine.call(_request_from_wire(request))
        except AdLibrarySourceError as error:
            return httpx.Response(error.http_status, json=error.raw)
        return httpx.Response(answer.http_status, json=answer.raw)


@dataclass
class Harness:
    source: AdLibrarySource
    #: The mock whose answers are served (the source itself, for the mock).
    engine: MockAdLibrarySource
    #: How many provider requests were made so far.
    sent: Callable[[], int]

    def run(self, method: str, *args, **kwargs):
        call = getattr(self.source, method)
        return asyncio.run(call(*args, credential=memoryview(TEST_KEY.encode()), **kwargs))


def _harness(kind: str, *, credits: int = 100) -> Harness:
    engine = MockAdLibrarySource(credits=credits, clock=lambda: CLOCK)
    if kind == "mock":
        return Harness(engine, engine, lambda: len(engine.requests))
    transport = ScrapeCreatorsReplay(engine)
    return Harness(ScrapeCreatorsAdLibrarySource(transport=transport), engine, lambda: len(transport.requests))


@pytest.fixture(params=["mock", "scrapecreators"])
def kind(request) -> str:
    return request.param


@pytest.fixture
def harness(kind) -> Harness:
    return _harness(kind)


def _ads(response: SourceResponse):
    return normalize_ads(response, origin=page_origin("cmp_1"), observed_at=CLOCK)


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def test_declares_platform_countries_billing_unit_and_operations(harness):
    descriptor = harness.source.descriptor
    assert isinstance(harness.source, AdLibrarySource)
    assert descriptor.platform == PLATFORM_META
    assert descriptor.billing_unit == BILLING_UNIT_CREDIT
    assert descriptor.operations == METHOD_OPERATIONS
    assert descriptor.supports_country("US")
    assert not descriptor.supports_country("ZZ")


# ---------------------------------------------------------------------------
# The four calls
# ---------------------------------------------------------------------------


def test_search_pages_returns_raw_candidates_and_reported_billing(harness):
    response = harness.run("search_pages", "Acme Charge")

    assert response.operation is PriceOperation.PAGE_SEARCH
    assert response.endpoint == "/v1/facebook/adLibrary/search/companies"
    assert response.request_params == {"query": "Acme Charge"}
    assert response.http_status == 200
    assert (response.credits_charged, response.credits_remaining, response.cached) == (1, 99, False)
    assert isinstance(response.latency_ms, int) and response.latency_ms >= 0
    assert response.next_cursor is None
    candidates = normalize_page_candidates(response)
    assert len(candidates) == 15
    same_name = [c for c in candidates if c.name and c.name.casefold().startswith("acme charge")]
    assert len(same_name) == 8
    assert candidates[0].page_id == ACME and candidates[0].page_alias == "acmecharge"
    assert any(c.name == "Acme Charge Fuel Systems" and c.likes == 273436 for c in candidates)


def test_brightvolt_ranks_after_the_czech_event_page(harness):
    candidates = normalize_page_candidates(harness.run("search_pages", "Brightvolt"))

    event = next(c for c in candidates if c.category == "Event")
    brightvolt = next(c for c in candidates if c.name == "Brightvolt")
    assert event.rank == 0 and event.name.startswith("Konference")
    assert brightvolt.page_id == BRIGHTVOLT and brightvolt.rank == 4 > event.rank


def test_ads_for_page_follows_the_cursor(harness):
    pages, cursor = [], None
    for _ in range(3):
        response = harness.run("ads_for_page", ACME, country="US", cursor=cursor)
        pages.append(response)
        cursor = response.next_cursor

    assert [len(_ads(page)) for page in pages] == [30, 10, 10]
    assert [page.credits_remaining for page in pages] == [99, 98, 97]
    assert pages[0].request_params == {"pageId": ACME, "country": "US", "status": "ACTIVE"}
    assert pages[1].request_params["cursor"] == pages[0].next_cursor
    assert all(page.next_cursor for page in pages)
    ids = [ad.ad_archive_id for page in pages for ad in _ads(page)]
    assert len(ids) == len(set(ids)) == 50


def test_probe_and_page_are_one_call_billed_as_two_operations(harness):
    probe = harness.run("ads_for_page", ACME, country="US", operation="probe")
    page = harness.run("ads_for_page", ACME, country="US")

    assert (probe.operation, page.operation) == (PriceOperation.PROBE, PriceOperation.PAGE)
    assert probe.endpoint == page.endpoint and probe.request_params == page.request_params


def test_search_ads_pages_keyword_results(harness):
    first = harness.run("search_ads", "NACS adapter", country="US")
    second = harness.run("search_ads", "NACS adapter", country="US", cursor=first.next_cursor)

    assert first.operation is PriceOperation.KEYWORD_PAGE
    assert first.endpoint == "/v1/facebook/adLibrary/search/ads"
    assert "searchResults" in first.raw
    assert (len(_ads(first)), len(_ads(second))) == (30, 10)
    assert {ad.advertiser_name for ad in _ads(first)} >= {"Acme Charge", "Kestrel Power"}


def test_ad_detail_refreshes_one_ad(harness):
    response = harness.run("ad_detail", ACME_DCO_AD)

    assert response.operation is PriceOperation.AD_REFRESH
    assert response.request_params == {"id": ACME_DCO_AD}
    [ad] = _ads(response)
    assert ad.ad_archive_id == ACME_DCO_AD and ad.advertiser_page_id == ACME
    assert all(card.image_ref for card in ad.cards[1:])


def test_a_page_without_ads_is_an_empty_billed_page(harness):
    response = harness.run("ads_for_page", ACME_ISRAEL, country="US")

    assert _ads(response) == [] and response.next_cursor is None
    assert response.credits_charged == 1
    assert normalize_page_candidates(harness.run("search_pages", "no such brand anywhere")) == []


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_running_out_of_credits_is_a_402_that_charges_nothing(kind):
    harness = _harness(kind, credits=1)
    harness.run("search_pages", "Acme Charge")

    with pytest.raises(AdLibrarySourceError) as raised:
        harness.run("ads_for_page", ACME, country="US")

    error = raised.value
    assert (error.error_code, error.http_status) == ("insufficient_credits", 402)
    assert (error.dispatch_certainty, error.retryable) == ("not_dispatched", False)
    assert error.request.operation is PriceOperation.PAGE
    assert harness.engine.credits_remaining == 0


def test_an_unsupported_country_never_reaches_the_provider(harness):
    with pytest.raises(AdLibrarySourceError) as raised:
        harness.run("ads_for_page", ACME, country="ZZ")

    assert (raised.value.error_code, raised.value.dispatch_certainty) == ("unsupported_country", "not_dispatched")
    assert harness.sent() == 0


@pytest.mark.parametrize(
    ("status", "code", "certainty", "retryable"),
    [
        (429, "rate_limited", "not_dispatched", True),
        (503, "http_503", "uncertain", True),
        (401, "credential_rejected", "not_dispatched", False),
        (404, "http_404", "not_dispatched", False),
    ],
)
def test_provider_failures_classify_alike_and_are_never_retried(harness, status, code, certainty, retryable):
    harness.engine.fail_next(status, method="ads_for_page")

    with pytest.raises(AdLibrarySourceError) as raised:
        harness.run("ads_for_page", ACME, country="US")

    error = raised.value
    assert (error.error_code, error.dispatch_certainty, error.retryable) == (code, certainty, retryable)
    assert error.http_status == status and error.credits_charged is None
    assert harness.sent() == 1
    # The caller decides to retry; the next call is a new, billed request.
    assert harness.run("ads_for_page", ACME, country="US").credits_charged == 1


def test_an_unknown_ad_detail_is_a_404(harness):
    with pytest.raises(AdLibrarySourceError) as raised:
        harness.run("ad_detail", "1")

    assert (raised.value.error_code, raised.value.dispatch_certainty) == ("http_404", "not_dispatched")


# ---------------------------------------------------------------------------
# Same input, same output
# ---------------------------------------------------------------------------


def _calls(harness: Harness) -> list[SourceResponse]:
    first = harness.run("ads_for_page", ACME, country="US")
    keyword = harness.run("search_ads", "CCS1 adapter", country="US")
    return [
        harness.run("search_pages", "Brightvolt"),
        first,
        harness.run("ads_for_page", ACME, country="US", cursor=first.next_cursor),
        harness.run("ads_for_page", BRIGHTVOLT, country="US", operation="probe"),
        keyword,
        harness.run("search_ads", "CCS1 adapter", country="US", cursor=keyword.next_cursor),
        harness.run("ad_detail", ACME_DCO_AD),
        harness.run("ads_for_page", ACME_ISRAEL, country="US"),
    ]


def _comparable(response: SourceResponse) -> dict:
    evidence = response.evidence()
    del evidence["provider"], evidence["latency_ms"]
    return {"evidence": evidence, "raw": response.raw}


def test_mock_and_real_adapter_answer_the_same_calls_identically():
    mock, real = _calls(_harness("mock")), _calls(_harness("scrapecreators"))

    assert [r.provider for r in mock] == ["mock"] * len(mock)
    assert [r.provider for r in real] == ["scrapecreators"] * len(real)
    assert [_comparable(r) for r in mock] == [_comparable(r) for r in real]
    for mock_response, real_response in zip(mock, real):
        if mock_response.request.method is SourceMethod.SEARCH_PAGES:
            assert normalize_page_candidates(mock_response) == normalize_page_candidates(real_response)
        else:
            assert _ads(mock_response) == [replace(ad, provider="mock") for ad in _ads(real_response)]


def test_the_credential_is_never_in_a_result_or_an_error(harness):
    responses = _calls(harness)
    harness.engine.fail_next(401)
    with pytest.raises(AdLibrarySourceError) as raised:
        harness.run("search_pages", "Acme Charge")

    texts = [repr(r) + json.dumps(r.evidence(), default=str) + json.dumps(r.raw) for r in responses]
    texts.append(str(raised.value) + repr(raised.value) + json.dumps(raised.value.evidence(), default=str))
    assert not any(TEST_KEY in text for text in texts)
