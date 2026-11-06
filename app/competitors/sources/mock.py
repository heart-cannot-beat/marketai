"""Controlled Mock ``AdLibrarySource``: sanitized P0 recordings, replayed.

The fixtures under ``fixtures/scrapecreators_p0/`` are the ScrapeCreators
responses of the 2026-10-26 P0 run (US, active ads), rewritten by
``scripts/build_competitor_mock_fixtures.py``: public brand pages, ad copy
and landing domains are kept; personal-account advertisers are pseudonyms;
signed media links point at placeholder images (:func:`fetch_mock_media`);
avatars and tracking parameters are gone. P0 recorded no empty page, no
402, no 404 and no ``/ad`` call, so those few answers are synthesized and
say so in their ``provenance``. A page search answers each page with a
placeholder avatar link of its own (never a recorded one), so a Mock
resolve archives avatars the way a real one does (Issue #332).

The mock answers in the provider's own wire format -- it looks a request up
by the exact endpoint and query parameters the real adapter would send
(``query`` case-insensitively) and runs the result through the same
:func:`~app.competitors.sources.scrapecreators.parse_success` and
:func:`~app.competitors.sources.scrapecreators.failure_for_status` -- so
Mock and real responses have one shape:

* a recorded request -> its recording; any other listing request -> the
  provider's empty page (a page or keyword with no active ads);
* ``ad_detail`` of a recorded ad -> that ad in the documented ``/ad`` shape
  (camelCase top level, ``snapshot.body`` a string), unknown ids -> 404;
* every answer is charged its recorded ``credits_charged`` against a
  simulated balance (``credits_remaining``); a call the balance cannot
  cover is a 402 that charges nothing;
* :meth:`MockAdLibrarySource.fail_next` makes chosen calls fail with a
  status (429, 5xx, 401 ...), for retry and partial-scan paths.

Placeholder media links expire like the real signed ones: each answer
re-issues ``oe=`` as "now + the lifetime the link had when P0 captured it"
(about 104 hours), so mock scans keep working after the capture date and
an expired link can still be exercised with an injected clock. Credits are
simulated: nothing here is real cost.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

from app.competitors.sources.base import (
    BILLING_UNIT_CREDIT,
    PLATFORM_META,
    AdLibrarySourceBase,
    SourceDescriptor,
    SourceMethod,
    SourceRequest,
    SourceResponse,
)
from app.competitors.sources.scrapecreators import (
    ENDPOINTS,
    failure_for_status,
    listing_items,
    parse_success,
    reported_credits,
    wire_request,
)

MOCK_PROVIDER = "mock"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "scrapecreators_p0"
#: The simulated balance a mock starts with (the P0 account's starting balance).
DEFAULT_MOCK_CREDITS = 100

MOCK_MEDIA_HOST = "media.mock.invalid"
MOCK_MEDIA_PATH = "/competitor-media/"
#: kind -> (extension, placeholder file under ``FIXTURE_DIR / "media"``).
#: Video files are never archived (spec §1), so they have no placeholder.
MEDIA_KINDS: dict[str, tuple[str, str | None]] = {
    "image": ("png", "placeholder-image.png"),
    "video-poster": ("png", "placeholder-video-poster.png"),
    "avatar": ("png", "placeholder-avatar.png"),
    "video": ("mp4", None),
}

_TEMPLATE_ANY_ENDPOINT = "*"


# ---------------------------------------------------------------------------
# Placeholder media
# ---------------------------------------------------------------------------


def placeholder_media_url(kind: str, ref: str, expires_at: datetime | None) -> str:
    extension, _ = MEDIA_KINDS[kind]
    url = f"https://{MOCK_MEDIA_HOST}{MOCK_MEDIA_PATH}{kind}/{ref}.{extension}"
    if expires_at is not None:
        url += "?" + urlencode({"oe": format(int(expires_at.timestamp()), "X")})
    return url


def _placeholder_expiry(url: str) -> int | None:
    if not url.startswith(f"https://{MOCK_MEDIA_HOST}{MOCK_MEDIA_PATH}"):
        return None
    query = urlsplit(url).query
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == "oe":
            try:
                return int(value, 16)
            except ValueError:
                return None
    return None


@dataclass(frozen=True)
class MockMedia:
    """What downloading a placeholder link answers: 200 + bytes, 403 expired, 404."""

    status: int
    content: bytes | None = None
    content_type: str | None = None


def fetch_mock_media(url: str, *, now: datetime) -> MockMedia:
    """The placeholder behind a mock media link, as of ``now``."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != MOCK_MEDIA_HOST or not parts.path.startswith(MOCK_MEDIA_PATH):
        return MockMedia(404)
    kind = parts.path[len(MOCK_MEDIA_PATH):].split("/", 1)[0]
    _, placeholder = MEDIA_KINDS.get(kind, (None, None))
    if placeholder is None:
        return MockMedia(404)
    expires = _placeholder_expiry(url)
    if expires is not None and now.timestamp() >= expires:
        return MockMedia(403)
    return MockMedia(200, _media_bytes(placeholder), "image/png")


@functools.lru_cache(maxsize=None)
def _media_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / "media" / name).read_bytes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _lookup_key(endpoint: str, params: Mapping[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    normalized = {key: value.strip().casefold() if key == "query" else value for key, value in params.items()}
    return endpoint, tuple(sorted(normalized.items()))


@dataclass(frozen=True)
class MockFixture:
    name: str
    status: int
    body: Any
    provenance: Mapping[str, Any]
    endpoint: str
    params: Mapping[str, str] | None = None
    template: str | None = None

    @property
    def captured_at(self) -> datetime | None:
        value = self.provenance.get("captured_at")
        return datetime.fromisoformat(value) if isinstance(value, str) else None


@dataclass(frozen=True)
class MockFixtures:
    """Recorded answers keyed by wire request, plus the synthesized templates."""

    recorded: Mapping[tuple[str, tuple[tuple[str, str], ...]], MockFixture]
    templates: Mapping[tuple[str, str], MockFixture]
    ads: Mapping[str, tuple[MockFixture, Mapping[str, Any]]] = field(default_factory=dict)

    @classmethod
    def from_files(cls, directory: Path) -> MockFixtures:
        recorded, templates, ads = {}, {}, {}
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            request, template = data.get("request"), data.get("template")
            fixture = MockFixture(
                name=path.stem, status=data["status"], body=data["body"], provenance=data["provenance"],
                endpoint=(request or template)["path"], params=request and request["params"],
                template=template and template["use"],
            )
            if fixture.template is not None:
                templates[(fixture.endpoint, fixture.template)] = fixture
                continue
            recorded[_lookup_key(fixture.endpoint, fixture.params or {})] = fixture
            listing = listing_items(fixture.body) if fixture.endpoint != ENDPOINTS[SourceMethod.SEARCH_PAGES] else None
            for item in (listing or ("", []))[1]:
                if isinstance(item, dict) and isinstance(item.get("ad_archive_id"), str):
                    ads.setdefault(item["ad_archive_id"], (fixture, item))
        return cls(recorded=recorded, templates=templates, ads=ads)

    @property
    def countries(self) -> frozenset[str]:
        return frozenset(
            fixture.params["country"] for fixture in self.recorded.values()
            if fixture.params and "country" in fixture.params
        )

    def lookup(self, endpoint: str, params: Mapping[str, str]) -> MockFixture | None:
        return self.recorded.get(_lookup_key(endpoint, params))

    def template(self, endpoint: str, use: str) -> MockFixture:
        fixture = self.templates.get((endpoint, use)) or self.templates.get((_TEMPLATE_ANY_ENDPOINT, use))
        if fixture is None:
            raise LookupError(f"no {use!r} template for {endpoint}")
        return fixture


@functools.lru_cache(maxsize=4)
def load_fixtures(directory: Path = FIXTURE_DIR) -> MockFixtures:
    return MockFixtures.from_files(directory)


def _as_int(value: Any) -> Any:
    return int(value) if isinstance(value, str) and value.isdigit() else value


def _detail_body(ad: Mapping[str, Any]) -> dict[str, Any]:
    """A recorded listing ad in the documented ``/ad`` answer shape (synthesized)."""
    snapshot = copy.deepcopy(ad.get("snapshot") or {})
    if isinstance(snapshot.get("body"), dict):
        snapshot["body"] = snapshot["body"].get("text")
    if isinstance(snapshot.get("display_format"), str):
        snapshot["display_format"] = snapshot["display_format"].lower()
    return {
        "success": True,
        "credits_remaining": 0,
        "credits_charged": 1,
        "adArchiveID": _as_int(ad.get("ad_archive_id")),
        "collationID": ad.get("collation_id"),
        "collationCount": ad.get("collation_count"),
        "pageID": _as_int(ad.get("page_id")),
        "pageName": ad.get("page_name"),
        "pageIsDeleted": ad.get("page_is_deleted"),
        "isActive": ad.get("is_active"),
        "startDate": ad.get("start_date"),
        "endDate": ad.get("end_date"),
        "startDateString": ad.get("start_date_string"),
        "endDateString": ad.get("end_date_string"),
        "publisherPlatform": [p.lower() for p in ad.get("publisher_platform") or [] if isinstance(p, str)],
        "url": ad.get("url"),
        "snapshot": snapshot,
    }


def _reissue_media(node: Any, *, now: datetime, captured_at: datetime | None) -> Any:
    """Placeholder links valid from ``now`` for as long as they were at capture."""
    if captured_at is None:
        return node
    if isinstance(node, dict):
        return {key: _reissue_media(value, now=now, captured_at=captured_at) for key, value in node.items()}
    if isinstance(node, list):
        return [_reissue_media(value, now=now, captured_at=captured_at) for value in node]
    if isinstance(node, str):
        expires = _placeholder_expiry(node)
        if expires is not None:
            lifetime = expires - int(captured_at.timestamp())
            return node.split("?", 1)[0] + "?" + urlencode({"oe": format(int(now.timestamp()) + lifetime, "X")})
    return node


def _with_avatars(body: Any) -> Any:
    """A placeholder avatar link for every page of a page search (the recordings
    carry none). The link has no ``oe``: the same search answers the same body."""
    listing = listing_items(body)
    for item in listing[1] if listing else []:
        if isinstance(item, dict) and item.get("page_id") and not item.get("image_uri"):
            ref = hashlib.sha256(str(item["page_id"]).encode("utf-8")).hexdigest()[:16]
            item["image_uri"] = placeholder_media_url("avatar", ref, None)
    return body


@dataclass
class _Failure:
    status: int
    method: SourceMethod | None
    page_id: str | None
    query: str | None
    remaining: int

    def matches(self, request: SourceRequest) -> bool:
        return (
            (self.method is None or request.method is self.method)
            and (self.page_id is None or request.page_id == self.page_id)
            and (self.query is None or (request.query or "").casefold() == self.query.casefold())
        )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class MockAdLibrarySource(AdLibrarySourceBase):
    """Controlled Mock: deterministic, offline, simulated credits only."""

    def __init__(
        self,
        *,
        fixtures: MockFixtures | None = None,
        credits: int = DEFAULT_MOCK_CREDITS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fixtures = fixtures or load_fixtures()
        self.descriptor = SourceDescriptor(
            provider=MOCK_PROVIDER,
            platform=PLATFORM_META,
            countries=self._fixtures.countries,
            billing_unit=BILLING_UNIT_CREDIT,
        )
        self.credits_remaining = credits
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._failures: list[_Failure] = []
        #: Every request the mock answered or refused, in order.
        self.requests: list[SourceRequest] = []

    def fail_next(
        self,
        status: int,
        *,
        method: SourceMethod | str | None = None,
        page_id: str | None = None,
        query: str | None = None,
        times: int = 1,
    ) -> None:
        """Answer the next ``times`` matching calls with ``status`` instead."""
        if not 400 <= status <= 599:
            raise ValueError("a simulated failure needs a 4xx or 5xx status")
        self._failures.append(_Failure(
            status, SourceMethod(method) if method is not None else None, page_id, query, times,
        ))

    def _take_failure(self, request: SourceRequest) -> int | None:
        for failure in self._failures:
            if failure.remaining > 0 and failure.matches(request):
                failure.remaining -= 1
                return failure.status
        return None

    async def _send(self, request: SourceRequest, *, credential: memoryview | None) -> SourceResponse:
        started = time.monotonic()
        endpoint, params = wire_request(request)
        evidence = {"provider": MOCK_PROVIDER, "request": request, "endpoint": endpoint, "request_params": params}
        self.requests.append(request)

        status = self._take_failure(request)
        if status is not None:
            body = (
                copy.deepcopy(self._fixtures.template(endpoint, "insufficient_credits").body) if status == 402
                else {"success": False, "message": f"Simulated HTTP {status} (Controlled Mock)"}
            )
            raise failure_for_status(status, body, latency_ms=_elapsed_ms(started), **evidence)

        fixture, body = self._answer(request, endpoint, params)
        if not 200 <= fixture.status < 300:
            raise failure_for_status(fixture.status, body, latency_ms=_elapsed_ms(started), **evidence)

        charge = reported_credits(body, "credits_charged") or 0
        if charge > self.credits_remaining:
            insufficient = copy.deepcopy(self._fixtures.template(endpoint, "insufficient_credits").body)
            raise failure_for_status(402, insufficient, latency_ms=_elapsed_ms(started), **evidence)
        self.credits_remaining -= charge
        body["credits_charged"] = charge
        body["credits_remaining"] = self.credits_remaining
        body = _reissue_media(body, now=self._clock(), captured_at=fixture.captured_at)
        if request.method is SourceMethod.SEARCH_PAGES:
            body = _with_avatars(body)
        return parse_success(body, status=fixture.status, latency_ms=_elapsed_ms(started), **evidence)

    def _answer(self, request: SourceRequest, endpoint: str, params: dict[str, str]) -> tuple[MockFixture, Any]:
        if request.method is SourceMethod.AD_DETAIL:
            found = self._fixtures.ads.get(request.ad_archive_id or "")
            if found is None:
                fixture = self._fixtures.template(endpoint, "not_found")
                return fixture, copy.deepcopy(fixture.body)
            fixture, ad = found
            return fixture, _detail_body(ad)
        fixture = self._fixtures.lookup(endpoint, params) or self._fixtures.template(endpoint, "empty")
        return fixture, copy.deepcopy(fixture.body)
