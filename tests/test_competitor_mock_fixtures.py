"""Controlled Mock fixtures and behaviour for the competitor scan (#268).

Hygiene: no signed CDN link, avatar, API key or tracking parameter in any
fixture; people are pseudonyms. Coverage: every case downstream issues test
against (DCO with 2-3 cards, video, no body, collation, empty page, 402,
cursor pagination, the Acme Charge and Brightvolt candidate lists).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.competitors.normalize import media_url_expires_at, normalize_ads, page_origin
from app.competitors.sources.base import AdLibrarySourceError
from app.competitors.sources.mock import (
    FIXTURE_DIR,
    MEDIA_KINDS,
    MOCK_MEDIA_HOST,
    MockAdLibrarySource,
    fetch_mock_media,
    load_fixtures,
)

CAPTURED = datetime(2026, 10, 26, 8, 31, 58, tzinfo=timezone.utc)
FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json"))
URL = re.compile(r"https?://[^\s\"'<>\\]+")
AVATAR_KEYS = ("page_profile_picture_url", "page_profile_pic_url", "image_uri", "instagram_profile_pic_url")


def _walk(node, key=None):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


def _ad_items():
    for fixture in load_fixtures().recorded.values():
        for key in ("results", "searchResults"):
            for item in fixture.body.get(key) or []:
                if "snapshot" in item:
                    yield fixture, item


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda path: path.stem)
def test_no_signed_link_avatar_key_or_tracking_parameter(path: Path):
    text = path.read_text(encoding="utf-8")

    assert not re.search(r"fbcdn\.net|cdninstagram\.com|fbsbx\.com", text, re.IGNORECASE)
    assert not re.search(r"x-api-key|api[_-]?key|authorization|bearer", text, re.IGNORECASE)
    for key, value in _walk(json.loads(text)):
        if key in AVATAR_KEYS:
            assert value is None, key
    for url in URL.findall(text):
        parts = urlsplit(url)
        if parts.hostname == MOCK_MEDIA_HOST:
            assert parts.path.split("/")[2] in MEDIA_KINDS
            assert set(dict(p.split("=", 1) for p in parts.query.split("&") if p)) <= {"oe"}
        elif parts.path.rstrip("/") == "/ads/library":
            assert re.fullmatch(r"id=\d+", parts.query), url
        else:
            assert not parts.query and not parts.fragment, url


def test_every_fixture_says_where_it_came_from():
    for path in FIXTURE_FILES:
        data = json.loads(path.read_text(encoding="utf-8"))
        provenance = data["provenance"]
        if path.stem.startswith("template--"):
            assert provenance["kind"] == "synthesized" and provenance["reason"]
            assert "request" not in data
        else:
            assert provenance["kind"] == "recorded"
            assert provenance["run"] == "p0-scrapecreators/20261026-163158"
            assert provenance["raw_file"].endswith(".json") and provenance["latency_ms"] > 0
            assert data["status"] == 200


def test_personal_advertisers_and_creators_are_pseudonyms():
    people, creators = set(), set()
    for _, item in _ad_items():
        snapshot = item["snapshot"]
        if snapshot["page_id"] == item["page_id"] and snapshot.get("page_categories") == ["Profile"]:
            people.add((item["page_name"], item["page_id"]))
            assert item["snapshot"]["link_url"].startswith("https://facebook.com/marketplace/item/93")
            assert item["snapshot"]["body"]["text"].startswith("Private Marketplace listing")
        for node in (snapshot, snapshot.get("branded_content") or {}):
            if node and node["page_id"] != item["page_id"]:
                creators.add((node["page_name"], node["page_id"]))
    assert len(people) == 3
    assert all(re.fullmatch(r"Person Example \d+", name) and pid.startswith("9000") for name, pid in people)
    assert len(creators) == 16
    assert all(re.fullmatch(r"Creator Example \d+|creator\.example\.\d+", name) and pid.startswith("950")
               for name, pid in creators)


def test_public_brand_pages_keep_their_names_ids_and_domains():
    advertisers = {(item["page_name"], item["page_id"]) for _, item in _ad_items()}

    for name in ("Acme Charge", "Brightvolt", "Halcyon Motors", "Northpeak", "Kestrel Power", "Lumenline"):
        assert any(advertiser == name for advertiser, _ in advertisers), name
    assert ("Acme Charge", "100000000000010") in advertisers
    assert ("Brightvolt", "100000000000016") in advertisers
    assert ("Halcyon Motors", "100000000010002") in advertisers


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_the_fixtures_cover_every_case_downstream_relies_on():
    items = [item for _, item in _ad_items()]
    variants = lambda fmt: {len(i["snapshot"]["cards"]) for i in items if i["snapshot"]["display_format"] == fmt}

    assert {2, 3} <= variants("DCO")
    assert any(i["snapshot"]["display_format"] == "VIDEO" for i in items)
    assert any(not (i["snapshot"]["body"]["text"] or "").strip() for i in items)
    assert any((i.get("collation_count") or 0) > 1 for i in items)
    paged = [f for f in load_fixtures().recorded.values() if f.params and "cursor" in f.params]
    assert {f.name for f in paged} >= {"company-ads--acme-charge--p2", "company-ads--acme-charge--p3", "search-ads--nacs-adapter--p2"}
    templates = load_fixtures().templates
    assert {use for _, use in templates} == {"empty", "insufficient_credits", "not_found"}
    assert templates[("*", "insufficient_credits")].status == 402


# ---------------------------------------------------------------------------
# Mock behaviour
# ---------------------------------------------------------------------------


def _run(coroutine):
    return asyncio.run(coroutine)


def test_the_mock_covers_only_the_recorded_market():
    source = MockAdLibrarySource()

    assert source.descriptor.provider == "mock"
    assert source.descriptor.countries == frozenset({"US"})
    with pytest.raises(AdLibrarySourceError) as raised:
        _run(source.ads_for_page("100000000000010", country="DE"))
    assert raised.value.error_code == "unsupported_country" and source.requests == []


def test_queries_match_case_insensitively():
    source = MockAdLibrarySource()

    assert _run(source.search_pages("  acme charge ")).raw == _run(MockAdLibrarySource().search_pages("Acme Charge")).raw


def test_credits_are_simulated_and_402_charges_nothing():
    source = MockAdLibrarySource(credits=2)
    _run(source.search_pages("Halcyon Motors"))
    _run(source.search_pages("Halcyon Motors"))

    with pytest.raises(AdLibrarySourceError) as raised:
        _run(source.search_pages("Halcyon Motors"))

    assert raised.value.http_status == 402 and source.credits_remaining == 0
    source.credits_remaining = 5  # topped up: the next call goes through again
    assert _run(source.search_pages("Halcyon Motors")).credits_remaining == 4


def test_simulated_failures_target_matching_calls_only():
    source = MockAdLibrarySource()
    source.fail_next(429, page_id="100000000000016", times=2)

    acme = _run(source.ads_for_page("100000000000010", country="US"))
    for _ in range(2):
        with pytest.raises(AdLibrarySourceError) as raised:
            _run(source.ads_for_page("100000000000016", country="US"))
        assert raised.value.error_code == "rate_limited"
    brightvolt = _run(source.ads_for_page("100000000000016", country="US"))

    assert acme.credits_charged == brightvolt.credits_charged == 1
    assert source.credits_remaining == 98
    with pytest.raises(ValueError):
        source.fail_next(200)


def test_placeholder_links_are_reissued_from_the_mocks_clock():
    later = CAPTURED + timedelta(days=10)
    at_capture = _run(MockAdLibrarySource(clock=lambda: CAPTURED).ad_detail("920000000000004"))
    reissued = _run(MockAdLibrarySource(clock=lambda: later).ad_detail("920000000000004"))

    [original] = normalize_ads(at_capture, origin=page_origin("cmp_1"), observed_at=CAPTURED)
    [fresh] = normalize_ads(reissued, origin=page_origin("cmp_1"), observed_at=later)
    lifetime = original.cards[1].image_ref.expires_at - CAPTURED
    assert timedelta(hours=100) < lifetime < timedelta(hours=110)
    assert fresh.cards[1].image_ref.expires_at - later == lifetime
    assert fresh.cards[1].image_ref.url.split("?")[0] == original.cards[1].image_ref.url.split("?")[0]


def test_placeholder_media_downloads_until_the_link_expires():
    response = _run(MockAdLibrarySource(clock=lambda: CAPTURED).ad_detail("1000000000000241"))
    [ad] = normalize_ads(response, origin=page_origin("cmp_1"), observed_at=CAPTURED)
    poster = ad.cards[0].video_poster_ref
    expires = media_url_expires_at(poster.url)

    fresh = fetch_mock_media(poster.url, now=CAPTURED)
    assert fresh.status == 200 and fresh.content_type == "image/png"
    assert fresh.content.startswith(b"\x89PNG")
    assert fetch_mock_media(poster.url, now=expires).status == 403
    video = response.raw["snapshot"]["videos"][0]["video_hd_url"]
    assert urlsplit(video).hostname == MOCK_MEDIA_HOST
    assert fetch_mock_media(video, now=CAPTURED).status == 404
    assert fetch_mock_media("https://example.com/x.png", now=CAPTURED).status == 404
