"""ScrapeCreators' Meta Ad Library API as an ``AdLibrarySource`` (spec §6, §12).

==============  ===========================================  =============================
method          endpoint (``GET``, header ``x-api-key``)     query parameters
==============  ===========================================  =============================
search_pages    ``/v1/facebook/adLibrary/search/companies``  ``query``
ads_for_page    ``/v1/facebook/adLibrary/company/ads``       ``pageId country status cursor``
search_ads      ``/v1/facebook/adLibrary/search/ads``        ``query country status cursor``
ad_detail       ``/v1/facebook/adLibrary/ad``                ``id``
==============  ===========================================  =============================

The parameters are exactly the ones the 2026-10-26 P0 run sent; ordering is
left at the provider's default (impressions) and no ``cache_max_age`` is
sent, so every answer is a live, billed scrape.

Every body reports ``credits_charged`` and ``credits_remaining``; a cache hit
would add ``cached: true``. Listing pages carry their ads under ``results``
(``company/ads``) or ``searchResults`` (``search/ads``, and the page
candidates of ``search/companies``) and the next page's token under
``cursor``. ``/ad`` answers with a single ad whose top-level keys are
camelCase (``adArchiveID``, ``pageID``, ``startDateString`` ...).

Outcomes:

* success -- a 2xx JSON object of the expected shape;
* ``not_dispatched`` -- the call never left (no credential, connection
  refused), or the provider refused it with a 4xx and reported no charge:
  402 ``insufficient_credits``, 401/403 ``credential_rejected``, 429
  ``rate_limited`` (``retryable``), any other ``http_<status>``;
* ``uncertain`` -- nobody can tell whether a credit went: timeouts, a
  dropped connection, 5xx (``retryable``: the caller decides) or a 2xx
  answer that is not usable, each unless the body reported the charge;
* ``dispatched`` -- a failure whose body reported ``credits_charged > 0``.

The adapter performs one HTTP request per call: no retries, no provider
switch, no Mock fallback.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.competitors.sources.base import (
    BILLING_UNIT_CREDIT,
    ISO_3166_ALPHA2,
    PLATFORM_META,
    AdLibrarySourceBase,
    AdLibrarySourceError,
    SourceDescriptor,
    SourceMethod,
    SourceRequest,
    SourceResponse,
)

PROVIDER = "scrapecreators"
DEFAULT_BASE_URL = "https://api.scrapecreators.com"
DEFAULT_TIMEOUT_SECONDS = 60.0

ENDPOINTS: dict[SourceMethod, str] = {
    SourceMethod.SEARCH_PAGES: "/v1/facebook/adLibrary/search/companies",
    SourceMethod.ADS_FOR_PAGE: "/v1/facebook/adLibrary/company/ads",
    SourceMethod.SEARCH_ADS: "/v1/facebook/adLibrary/search/ads",
    SourceMethod.AD_DETAIL: "/v1/facebook/adLibrary/ad",
}

#: Keys under which a listing body carries its items, in lookup order.
LISTING_KEYS = ("results", "searchResults")


# ---------------------------------------------------------------------------
# Wire format (shared with the Controlled Mock, which replays recordings of it)
# ---------------------------------------------------------------------------


def wire_request(request: SourceRequest) -> tuple[str, dict[str, str]]:
    """The endpoint and query parameters this request is sent as."""
    if request.method is SourceMethod.SEARCH_PAGES:
        params = {"query": request.query}
    elif request.method is SourceMethod.ADS_FOR_PAGE:
        params = {"pageId": request.page_id, "country": request.country, "status": request.status}
    elif request.method is SourceMethod.SEARCH_ADS:
        params = {"query": request.query, "country": request.country, "status": request.status}
    else:
        params = {"id": request.ad_archive_id}
    if request.cursor is not None:
        params["cursor"] = request.cursor
    return ENDPOINTS[request.method], {key: str(value) for key, value in params.items()}


def listing_items(body: Any) -> tuple[str, list[Any]] | None:
    """``(key, items)`` of a listing body, or ``None`` when it has none."""
    if isinstance(body, dict):
        for key in LISTING_KEYS:
            if isinstance(body.get(key), list):
                return key, body[key]
    return None


def is_ad_detail(body: Any) -> bool:
    return isinstance(body, dict) and ("adArchiveID" in body or "ad_archive_id" in body)


def reported_credits(body: Any, key: str) -> int | None:
    value = body.get(key) if isinstance(body, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return None
    return int(value)


def next_cursor(body: Any) -> str | None:
    value = body.get("cursor") if isinstance(body, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _certainty(credits_charged: int | None, unknown: str) -> str:
    if credits_charged is None:
        return unknown
    return "dispatched" if credits_charged > 0 else "not_dispatched"


def failure_for_status(
    status: int,
    body: Any,
    *,
    provider: str,
    request: SourceRequest,
    endpoint: str,
    request_params: dict[str, str],
    latency_ms: int,
) -> AdLibrarySourceError:
    """Classify a non-2xx answer. Provider prose stays in ``raw``, not the message."""
    if status == 402:
        code, retryable = "insufficient_credits", False
    elif status in (401, 403):
        code, retryable = "credential_rejected", False
    elif status == 429:
        code, retryable = "rate_limited", True
    else:
        code, retryable = f"http_{status}", status >= 500
    charged = reported_credits(body, "credits_charged")
    return AdLibrarySourceError(
        f"{provider} answered {status} ({code})", code,
        dispatch_certainty=_certainty(charged, "uncertain" if status >= 500 else "not_dispatched"),
        retryable=retryable,
        provider=provider, request=request, endpoint=endpoint, request_params=request_params,
        http_status=status, raw=body, credits_charged=charged,
        credits_remaining=reported_credits(body, "credits_remaining"), latency_ms=latency_ms,
    )


def _has_expected_shape(request: SourceRequest, body: dict[str, Any]) -> bool:
    if request.method is SourceMethod.AD_DETAIL:
        return is_ad_detail(body)
    listing = listing_items(body)
    if request.method is SourceMethod.SEARCH_PAGES:
        return listing is not None and listing[0] == "searchResults"
    return listing is not None


def parse_success(
    body: Any,
    *,
    provider: str,
    request: SourceRequest,
    endpoint: str,
    request_params: dict[str, str],
    status: int,
    latency_ms: int,
) -> SourceResponse:
    """A 2xx answer as a :class:`SourceResponse`, or the error it really is."""
    charged = reported_credits(body, "credits_charged")
    remaining = reported_credits(body, "credits_remaining")
    failure = None
    if not isinstance(body, dict):
        failure = "invalid_response", "answered 2xx without a JSON object"
    elif body.get("success") is False:
        failure = "provider_reported_failure", "answered 2xx with success=false"
    elif not _has_expected_shape(request, body):
        failure = "invalid_response", f"answered 2xx without the {request.method} shape"
    if failure is not None:
        code, detail = failure
        raise AdLibrarySourceError(
            f"{provider} {detail}", code, dispatch_certainty=_certainty(charged, "uncertain"), retryable=False,
            provider=provider, request=request, endpoint=endpoint, request_params=request_params,
            http_status=status, raw=body, credits_charged=charged, credits_remaining=remaining,
            latency_ms=latency_ms,
        )
    return SourceResponse(
        provider=provider, request=request, endpoint=endpoint, request_params=request_params,
        http_status=status, raw=body, credits_charged=charged, credits_remaining=remaining,
        cached=body.get("cached") is True, latency_ms=latency_ms, next_cursor=next_cursor(body),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ScrapeCreatorsAdLibrarySource(AdLibrarySourceBase):
    descriptor = SourceDescriptor(
        provider=PROVIDER,
        platform=PLATFORM_META,
        countries=ISO_3166_ALPHA2,
        billing_unit=BILLING_UNIT_CREDIT,
    )

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS
        self._transport = transport

    async def _send(self, request: SourceRequest, *, credential: memoryview | None) -> SourceResponse:
        endpoint, params = wire_request(request)
        evidence = {"provider": PROVIDER, "request": request, "endpoint": endpoint, "request_params": params}
        key = _api_key(credential)
        if key is None:
            raise AdLibrarySourceError(
                "no usable market_data credential was supplied for the call", "credential_unavailable",
                dispatch_certainty="not_dispatched", retryable=False, **evidence,
            )
        headers = {"x-api-key": key, "accept": "application/json"}
        key = ""
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                try:
                    response = await client.get(self._base_url + endpoint, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    # Not chained: the httpx request object carries the key header.
                    raise self._transport_failure(exc, _elapsed_ms(started), evidence) from None
        finally:
            headers["x-api-key"] = ""
        latency_ms = _elapsed_ms(started)
        body = _json_body(response)
        if not 200 <= response.status_code < 300:
            raise failure_for_status(response.status_code, body, latency_ms=latency_ms, **evidence)
        return parse_success(body, status=response.status_code, latency_ms=latency_ms, **evidence)

    def _transport_failure(self, exc: httpx.HTTPError, latency_ms: int, evidence: dict[str, Any]) -> AdLibrarySourceError:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            # The connection never opened: this request was never sent.
            return AdLibrarySourceError(
                f"{PROVIDER} unreachable: {type(exc).__name__}", "provider_unreachable",
                dispatch_certainty="not_dispatched", retryable=True, latency_ms=latency_ms, **evidence,
            )
        if isinstance(exc, httpx.TimeoutException):
            return AdLibrarySourceError(
                f"{PROVIDER} request timed out after {self._timeout:g}s", "provider_timeout",
                dispatch_certainty="uncertain", retryable=False, latency_ms=latency_ms, **evidence,
            )
        return AdLibrarySourceError(
            f"{PROVIDER} request failed in transit: {type(exc).__name__}", "provider_connection_lost",
            dispatch_certainty="uncertain", retryable=False, latency_ms=latency_ms, **evidence,
        )


def _api_key(credential: memoryview | None) -> str | None:
    if credential is None or len(credential) == 0:
        return None
    try:
        key = bytes(credential).decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return key or None


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
