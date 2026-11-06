"""Ad-library responses -> ``CompetitorAd`` observations (spec §5.3).

Every normalized field records the provider field it came from
(``field_sources``, paths relative to the ad item, e.g.
``snapshot.cards[1].title``); the item's place in the raw response is
``raw_path`` and the stored response itself is ``raw_ref``. Two fields are
derived and say so (``derived:...``): ``landing_domain`` from the first
card's ``link_url`` and ``days_running`` from the scan time and
``start_date``. A field the provider left out, or sent blank, stays empty:
nothing is guessed, and nothing is copied from one card into another.

Cards: the snapshot's own creative is card 0 and ``snapshot.cards[i]`` is
card ``i + 1`` -- for every ``display_format``, so a DCO's top level is its
card 0 even when its images live only on the variant cards.

Both ScrapeCreators shapes are read: listing items (``company/ads``,
``search/ads``: snake_case, ``snapshot.body.text``) and the ``/ad`` detail
(camelCase top level, ``snapshot.body`` as a string, ``page_categories`` as
an ``{id: name}`` object). Formats and platforms are upper-cased, ids are
strings.

``origins`` are the paths an observation was found by: ``page:<competitor
id>`` or ``keyword:<keyword>``; :func:`merge_observations` folds the same ad
found by several paths (or pages) into one observation with every origin.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

from app.competitors.sources.base import SourceMethod, SourceResponse
from app.competitors.sources.scrapecreators import listing_items

ORIGIN_PAGE_PREFIX = "page:"
ORIGIN_KEYWORD_PREFIX = "keyword:"

#: Second-level labels under a two-letter ccTLD that are not registrable
#: themselves (``co.uk``, ``com.au``, ``co.il`` ...). An approximation of the
#: Public Suffix List, which the project does not ship.
_CC_SECOND_LEVEL = frozenset({"ac", "co", "com", "edu", "gob", "gov", "ne", "net", "or", "org"})

_OE_HEX = re.compile(r"[0-9A-Fa-f]{6,10}")


class MalformedAdError(ValueError):
    """An ad item that cannot become an observation (no ``ad_archive_id``)."""


def page_origin(competitor_id: str) -> str:
    if not competitor_id:
        raise ValueError("competitor_id is required")
    return ORIGIN_PAGE_PREFIX + competitor_id


def keyword_origin(keyword: str) -> str:
    if not keyword or not keyword.strip():
        raise ValueError("keyword is required")
    return ORIGIN_KEYWORD_PREFIX + keyword.strip()


@dataclass(frozen=True)
class MediaRef:
    """A provider media link as received; signed links expire at ``expires_at``."""

    url: str
    source_field: str
    expires_at: datetime | None


@dataclass(frozen=True)
class AdCard:
    index: int
    title: str | None = None
    body: str | None = None
    cta_text: str | None = None
    cta_type: str | None = None
    link_url: str | None = None
    link_description: str | None = None
    image_ref: MediaRef | None = None
    video_poster_ref: MediaRef | None = None
    field_sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CompetitorAd:
    """One observation of a public ad: what was seen, never what works (ADR 0014)."""

    provider: str
    ad_archive_id: str
    observed_at: datetime
    collation_id: str | None = None
    collation_count: int | None = None
    advertiser_page_id: str | None = None
    advertiser_name: str | None = None
    page_profile_uri: str | None = None
    page_categories: tuple[str, ...] = ()
    is_active: bool | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    days_running: int | None = None
    display_format: str | None = None
    publisher_platforms: tuple[str, ...] = ()
    cards: tuple[AdCard, ...] = ()
    landing_domain: str | None = None
    library_url: str | None = None
    origins: tuple[str, ...] = ()
    raw_ref: str | None = None
    raw_path: str = ""
    field_sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageCandidate:
    """A page the provider returned for a name search, in the provider's order.

    The avatar (``image_uri``) is deliberately not kept: it is a signed link
    that expires, and the confirmation card does not store avatars (§4.4).
    """

    rank: int
    page_id: str
    name: str | None = None
    page_alias: str | None = None
    category: str | None = None
    likes: int | None = None
    ig_username: str | None = None
    ig_followers: int | None = None
    verification: str | None = None
    page_is_deleted: bool | None = None
    country: str | None = None
    raw_path: str = ""
    field_sources: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def media_url_expires_at(url: str | None) -> datetime | None:
    """Expiry of a signed Meta CDN link: ``oe=`` is a hex unix timestamp."""
    if not isinstance(url, str):
        return None
    values = parse_qs(urlsplit(url).query).get("oe")
    if not values or not _OE_HEX.fullmatch(values[0]):
        return None
    return datetime.fromtimestamp(int(values[0], 16), timezone.utc)


def landing_domain(url: str | None) -> str | None:
    """The registrable domain of a landing URL (``www.halcyon-motors.example`` -> ``halcyon-motors.example``)."""
    if not isinstance(url, str):
        return None
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if len(labels[-1]) == 2 and labels[-2] in _CC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def days_running(start_date: datetime | None, observed_at: datetime) -> int | None:
    """Whole days between the ad's start and the scan; an ad started today is 0."""
    if start_date is None:
        return None
    return max(0, math.floor((observed_at - start_date).total_seconds() / 86400))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _numeric_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _datetime_text(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _datetime_epoch(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc)


def _media_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


class _Fields:
    """Collects normalized values with the provider path each came from."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.sources: dict[str, str] = {}

    def take(self, name: str, node: Any, keys: Iterable[str], convert, prefix: str = "") -> Any:
        """The first of ``keys`` in ``node`` that converts to a value."""
        if not isinstance(node, dict):
            return None
        for key in keys:
            if key not in node:
                continue
            value = convert(node[key])
            if value is not None and value != ():
                self.values[name] = value
                self.sources[name] = f"{prefix}{key}"
                return value
        return None

    def derive(self, name: str, value: Any, source: str) -> None:
        if value is not None:
            self.values[name] = value
            self.sources[name] = source


def _upper_text(value: Any) -> str | None:
    text = _text(value)
    return text.strip().upper() if text else None


def _platforms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip().upper() for item in value if _text(item))


def _categories(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if _text(item))


def _body_text(node: dict[str, Any], prefix: str) -> tuple[str | None, str | None]:
    body = node.get("body")
    if isinstance(body, dict):
        text = _text(body.get("text"))
        return (text, f"{prefix}body.text") if text else (None, None)
    text = _text(body)
    return (text, f"{prefix}body") if text else (None, None)


def _media(node: dict[str, Any], key: str, list_key: str, prefix: str) -> MediaRef | None:
    """``node[key]`` (a variant card) or ``node[list_key][0][key]`` (the snapshot)."""
    url = _media_url(node.get(key))
    if url:
        return MediaRef(url, f"{prefix}{key}", media_url_expires_at(url))
    items = node.get(list_key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        url = _media_url(items[0].get(key))
        if url:
            return MediaRef(url, f"{prefix}{list_key}[0].{key}", media_url_expires_at(url))
    return None


def _card(index: int, node: dict[str, Any], prefix: str) -> AdCard:
    fields = _Fields()
    for name in ("title", "cta_text", "cta_type", "link_url", "link_description"):
        fields.take(name, node, (name,), _text, prefix)
    body, body_source = _body_text(node, prefix)
    fields.derive("body", body, body_source or "")
    for name, key, list_key in (
        ("image_ref", "original_image_url", "images"),
        ("video_poster_ref", "video_preview_image_url", "videos"),
    ):
        ref = _media(node, key, list_key, prefix)
        if ref is not None:
            fields.derive(name, ref, ref.source_field)
    return AdCard(index=index, field_sources=fields.sources, **fields.values)


def _advertiser_page(advertiser_page_id: str | None, snapshot: Any) -> tuple[dict[str, Any], str] | None:
    """The snapshot layer describing the advertiser's own page.

    In a partnership ad the snapshot's page is the creator's and the
    advertiser sits under ``branded_content`` (or the other way round); a
    page field is only taken from the layer whose ``page_id`` is the
    advertiser's, never from the creator's.
    """
    if advertiser_page_id is None or not isinstance(snapshot, dict):
        return None
    for node, prefix in ((snapshot, "snapshot."), (snapshot.get("branded_content"), "snapshot.branded_content.")):
        if isinstance(node, dict) and _numeric_id(node.get("page_id")) == advertiser_page_id:
            return node, prefix
    return None


def _cards(snapshot: Any) -> tuple[AdCard, ...]:
    if not isinstance(snapshot, dict):
        return ()
    cards = [_card(0, snapshot, "snapshot.")]
    variants = snapshot.get("cards")
    if isinstance(variants, list):
        cards.extend(
            _card(i + 1, variant, f"snapshot.cards[{i}].")
            for i, variant in enumerate(variants)
            if isinstance(variant, dict)
        )
    return tuple(cards)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_ad(
    ad: Mapping[str, Any],
    *,
    provider: str,
    origin: str,
    observed_at: datetime,
    raw_ref: str | None = None,
    raw_path: str = "",
) -> CompetitorAd:
    """One provider ad item (listing item or ``/ad`` body) as an observation."""
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    if not isinstance(ad, dict):
        raise MalformedAdError(f"ad item at {raw_path or '<root>'} is not an object")
    fields = _Fields()
    if fields.take("ad_archive_id", ad, ("ad_archive_id", "adArchiveID"), _numeric_id) is None:
        raise MalformedAdError(f"ad item at {raw_path or '<root>'} has no ad_archive_id")
    fields.take("collation_id", ad, ("collation_id", "collationID"), _numeric_id)
    fields.take("collation_count", ad, ("collation_count", "collationCount"), _int)
    fields.take("advertiser_page_id", ad, ("page_id", "pageID"), _numeric_id)
    fields.take("advertiser_name", ad, ("page_name", "pageName"), _text)
    fields.take("is_active", ad, ("is_active", "isActive"), _bool)
    for name, text_keys, epoch_keys in (
        ("start_date", ("start_date_string", "startDateString"), ("start_date", "startDate")),
        ("end_date", ("end_date_string", "endDateString"), ("end_date", "endDate")),
    ):
        if fields.take(name, ad, text_keys, _datetime_text) is None:
            fields.take(name, ad, epoch_keys, _datetime_epoch)
    fields.take("publisher_platforms", ad, ("publisher_platform", "publisherPlatform"), _platforms)
    fields.take("library_url", ad, ("url",), _text)

    snapshot = ad.get("snapshot")
    page = _advertiser_page(fields.values.get("advertiser_page_id"), snapshot)
    if page is not None:
        node, prefix = page
        fields.take("page_profile_uri", node, ("page_profile_uri",), _text, prefix)
        fields.take("page_categories", node, ("page_categories",), _categories, prefix)
    fields.take("display_format", snapshot, ("display_format",), _upper_text, "snapshot.")

    cards = _cards(snapshot)
    for card in cards:
        if card.link_url:
            fields.derive("landing_domain", landing_domain(card.link_url), f"derived:cards[{card.index}].link_url")
            break
    fields.derive(
        "days_running", days_running(fields.values.get("start_date"), observed_at),
        "derived:observed_at-start_date",
    )
    return CompetitorAd(
        provider=provider, observed_at=observed_at, cards=cards, origins=(origin,),
        raw_ref=raw_ref, raw_path=raw_path, field_sources=fields.sources, **fields.values,
    )


def normalize_ads(
    response: SourceResponse,
    *,
    origin: str,
    observed_at: datetime,
    raw_ref: str | None = None,
) -> list[CompetitorAd]:
    """Every ad in one source response, in provider order."""
    common = {"provider": response.provider, "origin": origin, "observed_at": observed_at, "raw_ref": raw_ref}
    if response.request.method is SourceMethod.AD_DETAIL:
        return [normalize_ad(response.raw, **common)]
    if response.request.method is SourceMethod.SEARCH_PAGES:
        raise ValueError("search_pages returns page candidates, not ads; use normalize_page_candidates")
    listing = listing_items(response.raw)
    if listing is None:
        return []
    key, items = listing
    return [normalize_ad(item, raw_path=f"{key}[{i}]", **common) for i, item in enumerate(items)]


def merge_observations(ads: Iterable[CompetitorAd]) -> list[CompetitorAd]:
    """One observation per ``ad_archive_id``: the first one seen, with every origin."""
    merged: dict[str, CompetitorAd] = {}
    for ad in ads:
        kept = merged.get(ad.ad_archive_id)
        if kept is None:
            merged[ad.ad_archive_id] = ad
            continue
        origins = kept.origins + tuple(origin for origin in ad.origins if origin not in kept.origins)
        if origins != kept.origins:
            merged[ad.ad_archive_id] = dataclasses.replace(kept, origins=origins)
    return list(merged.values())


def normalize_page_candidates(response: SourceResponse) -> list[PageCandidate]:
    """The page candidates of a ``search_pages`` response, in provider order."""
    if response.request.method is not SourceMethod.SEARCH_PAGES:
        raise ValueError("only search_pages returns page candidates")
    listing = listing_items(response.raw)
    if listing is None:
        return []
    key, items = listing
    candidates = []
    for i, item in enumerate(items):
        fields = _Fields()
        if fields.take("page_id", item, ("page_id",), _numeric_id) is None:
            continue
        for name, convert in (
            ("name", _text), ("page_alias", _text), ("category", _text), ("likes", _int),
            ("ig_username", _text), ("ig_followers", _int), ("verification", _text),
            ("page_is_deleted", _bool), ("country", _text),
        ):
            fields.take(name, item, (name,), convert)
        candidates.append(PageCandidate(rank=len(candidates), raw_path=f"{key}[{i}]", field_sources=fields.sources, **fields.values))
    return candidates
