"""``AdLibrarySource``: the provider seam for public ad-library data (spec §12).

Every call is billed as exactly one price-table operation (spec §6,
``MARKETAI_MARKET_DATA_MAX_COSTS_JSON``), so the operation is part of the
request, not something a caller infers afterwards:

==============  ==========================================  ==================
method          price-table operation                       what it fetches
==============  ==========================================  ==================
search_pages    ``page_search``                              page candidates
ads_for_page    ``probe`` (resolve job) / ``page`` (scan)    one page of ads
search_ads      ``keyword_page``                             one page of ads
ad_detail       ``ad_refresh``                               one ad, fresh media
==============  ==========================================  ==================

A source performs one provider request per call and returns the provider's
raw JSON together with what the provider itself reported about the call:
``credits_charged``, ``credits_remaining``, ``cached`` and the measured
latency. It never retries (429/5xx are the caller's decision), never
switches provider and never falls back to Controlled Mock (ADR 0014). A
failure is an :class:`AdLibrarySourceError` -- a ``ProviderDispatchError``,
so the dispatch coordinator's settle/release/uncertain vocabulary applies
unchanged -- carrying the same evidence a success would.

The Workspace's credential is handed over per call as an ephemeral
``memoryview``; a source never stores it, logs it or puts it in a result or
an error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

from app.providers.adapter import ProviderDispatchError


class PriceOperation(StrEnum):
    """Price-table operations (spec §6); one per billed call."""

    PAGE_SEARCH = "page_search"
    PROBE = "probe"
    PAGE = "page"
    KEYWORD_PAGE = "keyword_page"
    AD_REFRESH = "ad_refresh"


class SourceMethod(StrEnum):
    SEARCH_PAGES = "search_pages"
    ADS_FOR_PAGE = "ads_for_page"
    SEARCH_ADS = "search_ads"
    AD_DETAIL = "ad_detail"


class AdStatus(StrEnum):
    """Ad delivery status filter; the first release scans ``ACTIVE`` only."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ALL = "ALL"


#: Which price-table operations each method may be billed as. ``ads_for_page``
#: is the same provider endpoint for a resolve probe and a scan page; the
#: caller says which one it is buying.
METHOD_OPERATIONS: Mapping[SourceMethod, frozenset[PriceOperation]] = {
    SourceMethod.SEARCH_PAGES: frozenset({PriceOperation.PAGE_SEARCH}),
    SourceMethod.ADS_FOR_PAGE: frozenset({PriceOperation.PROBE, PriceOperation.PAGE}),
    SourceMethod.SEARCH_ADS: frozenset({PriceOperation.KEYWORD_PAGE}),
    SourceMethod.AD_DETAIL: frozenset({PriceOperation.AD_REFRESH}),
}

PLATFORM_META = "meta"
BILLING_UNIT_CREDIT = "credit"

#: ISO 3166-1 alpha-2. The Meta Ad Library filters by any of them; a scan is
#: one country at a time, so the provider's ``ALL`` is not offered.
ISO_3166_ALPHA2: frozenset[str] = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP
KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY
MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
NA NC NE NF NG NI NL NO NP NR NU NZ OM
PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ
VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())


@dataclass(frozen=True)
class SourceDescriptor:
    """What an implementation declares about itself."""

    provider: str
    platform: str
    countries: frozenset[str]
    billing_unit: str
    operations: Mapping[SourceMethod, frozenset[PriceOperation]] = field(
        default_factory=lambda: dict(METHOD_OPERATIONS),
    )

    def supports_country(self, country: str) -> bool:
        return country in self.countries


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _numeric_id(name: str, value: Any) -> str:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not text.isdigit():
        raise ValueError(f"{name} must be a numeric Meta id")
    return text


def _country(value: Any) -> str:
    text = _required_text("country", value).upper()
    if len(text) != 2 or not text.isalpha():
        raise ValueError("country must be a two-letter ISO 3166-1 code")
    return text


def _cursor(value: Any) -> str | None:
    if value is None:
        return None
    return _required_text("cursor", value)


@dataclass(frozen=True)
class SourceRequest:
    """One billed call, credential-free; build it with the classmethods."""

    method: SourceMethod
    operation: PriceOperation
    query: str | None = None
    page_id: str | None = None
    country: str | None = None
    status: AdStatus | None = None
    cursor: str | None = None
    ad_archive_id: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in METHOD_OPERATIONS[self.method]:
            raise ValueError(f"{self.method} cannot be billed as {self.operation}")

    @classmethod
    def search_pages(cls, query: str) -> SourceRequest:
        return cls(SourceMethod.SEARCH_PAGES, PriceOperation.PAGE_SEARCH, query=_required_text("query", query))

    @classmethod
    def ads_for_page(
        cls,
        page_id: str,
        *,
        country: str,
        status: AdStatus | str = AdStatus.ACTIVE,
        cursor: str | None = None,
        operation: PriceOperation | str = PriceOperation.PAGE,
    ) -> SourceRequest:
        return cls(
            SourceMethod.ADS_FOR_PAGE, PriceOperation(operation),
            page_id=_numeric_id("page_id", page_id), country=_country(country),
            status=AdStatus(status), cursor=_cursor(cursor),
        )

    @classmethod
    def search_ads(
        cls,
        query: str,
        *,
        country: str,
        status: AdStatus | str = AdStatus.ACTIVE,
        cursor: str | None = None,
    ) -> SourceRequest:
        return cls(
            SourceMethod.SEARCH_ADS, PriceOperation.KEYWORD_PAGE,
            query=_required_text("query", query), country=_country(country),
            status=AdStatus(status), cursor=_cursor(cursor),
        )

    @classmethod
    def ad_detail(cls, ad_archive_id: str) -> SourceRequest:
        return cls(
            SourceMethod.AD_DETAIL, PriceOperation.AD_REFRESH,
            ad_archive_id=_numeric_id("ad_archive_id", ad_archive_id),
        )

    def evidence(self) -> dict[str, str]:
        values = {
            "method": self.method, "operation": self.operation, "query": self.query,
            "page_id": self.page_id, "country": self.country, "status": self.status,
            "cursor": self.cursor, "ad_archive_id": self.ad_archive_id,
        }
        return {key: str(value) for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class SourceResponse:
    """A successful call: the provider's raw JSON plus what it reported.

    ``credits_charged`` / ``credits_remaining`` are ``None`` when the
    provider did not report them -- never a guess. ``cached`` is true only
    when the provider said the answer came from its cache. ``raw`` is the
    response body as received; callers store it as the observation's
    ``raw_ref`` blob and must not mutate it.
    """

    provider: str
    request: SourceRequest
    endpoint: str
    request_params: Mapping[str, str]
    http_status: int
    raw: Mapping[str, Any]
    credits_charged: int | None
    credits_remaining: int | None
    cached: bool
    latency_ms: int
    next_cursor: str | None

    @property
    def operation(self) -> PriceOperation:
        return self.request.operation

    def evidence(self) -> dict[str, Any]:
        """Per-call evidence for a ProviderCallAttempt; the raw body is separate."""
        return {
            "provider": self.provider,
            "request": self.request.evidence(),
            "endpoint": self.endpoint,
            "request_params": dict(self.request_params),
            "http_status": self.http_status,
            "credits_charged": self.credits_charged,
            "credits_remaining": self.credits_remaining,
            "cached": self.cached,
            "latency_ms": self.latency_ms,
            "next_cursor": self.next_cursor,
        }


class AdLibrarySourceError(ProviderDispatchError):
    """A failed call, with the evidence of how far it got.

    ``dispatch_certainty`` follows the coordinator: ``not_dispatched`` when
    nothing was billed (the call never left, or the provider refused it),
    ``dispatched`` when the provider reported a charge, ``uncertain`` when
    nobody can tell (timeouts, 5xx without a reported charge).
    """

    def __init__(
        self,
        message: str,
        error_code: str,
        *,
        dispatch_certainty: str,
        retryable: bool,
        provider: str,
        request: SourceRequest,
        endpoint: str | None = None,
        request_params: Mapping[str, str] | None = None,
        http_status: int | None = None,
        raw: Any = None,
        credits_charged: int | None = None,
        credits_remaining: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        super().__init__(dispatch_certainty, message, error_code, retryable=retryable)
        self.provider = provider
        self.request = request
        self.endpoint = endpoint
        self.request_params = dict(request_params or {})
        self.http_status = http_status
        self.raw = raw
        self.credits_charged = credits_charged
        self.credits_remaining = credits_remaining
        self.latency_ms = latency_ms

    def evidence(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "request": self.request.evidence(),
            "endpoint": self.endpoint,
            "request_params": dict(self.request_params),
            "http_status": self.http_status,
            "dispatch_certainty": self.dispatch_certainty,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "credits_charged": self.credits_charged,
            "credits_remaining": self.credits_remaining,
            "latency_ms": self.latency_ms,
        }


@runtime_checkable
class AdLibrarySource(Protocol):
    """A public ad-library provider (spec §12). Implementations: ScrapeCreators, Controlled Mock."""

    descriptor: SourceDescriptor

    async def call(self, request: SourceRequest, *, credential: memoryview | None = None) -> SourceResponse: ...

    async def search_pages(self, query: str, *, credential: memoryview | None = None) -> SourceResponse: ...

    async def ads_for_page(
        self,
        page_id: str,
        *,
        country: str,
        status: AdStatus | str = AdStatus.ACTIVE,
        cursor: str | None = None,
        operation: PriceOperation | str = PriceOperation.PAGE,
        credential: memoryview | None = None,
    ) -> SourceResponse: ...

    async def search_ads(
        self,
        query: str,
        *,
        country: str,
        status: AdStatus | str = AdStatus.ACTIVE,
        cursor: str | None = None,
        credential: memoryview | None = None,
    ) -> SourceResponse: ...

    async def ad_detail(self, ad_archive_id: str, *, credential: memoryview | None = None) -> SourceResponse: ...


class AdLibrarySourceBase:
    """Shared request checks and the four named calls; subclasses implement ``_send``."""

    descriptor: SourceDescriptor

    async def call(self, request: SourceRequest, *, credential: memoryview | None = None) -> SourceResponse:
        self._check_supported(request)
        return await self._send(request, credential=credential)

    async def _send(self, request: SourceRequest, *, credential: memoryview | None) -> SourceResponse:
        raise NotImplementedError

    def _check_supported(self, request: SourceRequest) -> None:
        provider = self.descriptor.provider
        if request.operation not in self.descriptor.operations.get(request.method, frozenset()):
            raise AdLibrarySourceError(
                f"{provider} does not bill {request.method} as {request.operation}", "operation_not_supported",
                dispatch_certainty="not_dispatched", retryable=False, provider=provider, request=request,
            )
        if request.country is not None and not self.descriptor.supports_country(request.country):
            raise AdLibrarySourceError(
                f"{provider} does not cover country {request.country}", "unsupported_country",
                dispatch_certainty="not_dispatched", retryable=False, provider=provider, request=request,
            )

    async def search_pages(self, query: str, *, credential: memoryview | None = None) -> SourceResponse:
        return await self.call(SourceRequest.search_pages(query), credential=credential)

    async def ads_for_page(
        self,
        page_id: str,
        *,
        country: str,
        status: AdStatus | str = AdStatus.ACTIVE,
        cursor: str | None = None,
        operation: PriceOperation | str = PriceOperation.PAGE,
        credential: memoryview | None = None,
    ) -> SourceResponse:
        request = SourceRequest.ads_for_page(
            page_id, country=country, status=status, cursor=cursor, operation=operation,
        )
        return await self.call(request, credential=credential)

    async def search_ads(
        self,
        query: str,
        *,
        country: str,
        status: AdStatus | str = AdStatus.ACTIVE,
        cursor: str | None = None,
        credential: memoryview | None = None,
    ) -> SourceResponse:
        request = SourceRequest.search_ads(query, country=country, status=status, cursor=cursor)
        return await self.call(request, credential=credential)

    async def ad_detail(self, ad_archive_id: str, *, credential: memoryview | None = None) -> SourceResponse:
        return await self.call(SourceRequest.ad_detail(ad_archive_id), credential=credential)
