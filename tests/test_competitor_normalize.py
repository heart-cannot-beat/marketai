"""``CompetitorAd`` normalization (#268, spec §5.3) over the sanitized P0 fixtures.

The fixtures hold all 128 distinct P0 ads: DCO 56, VIDEO 48, DPA 13,
IMAGE 9, CAROUSEL 2. Each format is checked field by field; every field
must name the provider field it came from, and nothing missing is guessed.
"""
from __future__ import annotations

import asyncio
import functools
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.competitors.normalize import (
    CompetitorAd,
    MalformedAdError,
    days_running,
    keyword_origin,
    landing_domain,
    media_url_expires_at,
    merge_observations,
    normalize_ad,
    normalize_ads,
    normalize_page_candidates,
    page_origin,
)
from app.competitors.sources.base import SourceMethod, SourceRequest
from app.competitors.sources.mock import MockAdLibrarySource, load_fixtures
from app.competitors.sources.scrapecreators import ENDPOINTS

CAPTURED = datetime(2026, 10, 26, 8, 31, 58, tzinfo=timezone.utc)
ACME = "100000000000010"


def _source() -> MockAdLibrarySource:
    return MockAdLibrarySource(credits=10_000, clock=lambda: CAPTURED)


def _request_for(fixture) -> tuple[SourceRequest, str] | None:
    params = fixture.params or {}
    if fixture.endpoint == ENDPOINTS[SourceMethod.ADS_FOR_PAGE]:
        request = SourceRequest.ads_for_page(
            params["pageId"], country=params["country"], status=params["status"], cursor=params.get("cursor"),
        )
        return request, page_origin(f"cmp_{params['pageId']}")
    if fixture.endpoint == ENDPOINTS[SourceMethod.SEARCH_ADS]:
        request = SourceRequest.search_ads(
            params["query"], country=params["country"], status=params["status"], cursor=params.get("cursor"),
        )
        return request, keyword_origin(params["query"])
    return None


@functools.cache
def _raw_ads() -> dict[str, dict]:
    """Every recorded ad item by ``ad_archive_id`` (first occurrence)."""
    items: dict[str, dict] = {}
    for fixture in load_fixtures().recorded.values():
        for key in ("results", "searchResults"):
            for item in fixture.body.get(key) or []:
                if "snapshot" in item:
                    items.setdefault(item["ad_archive_id"], item)
    return items


@functools.cache
def _observations() -> tuple[CompetitorAd, ...]:
    source, ads = _source(), []
    for fixture in sorted(load_fixtures().recorded.values(), key=lambda f: f.name):
        found = _request_for(fixture)
        if found is None:
            continue
        request, origin = found
        response = asyncio.run(source.call(request))
        ads.extend(normalize_ads(response, origin=origin, observed_at=CAPTURED, raw_ref=f"blob:{fixture.name}"))
    return tuple(merge_observations(ads))


def _ad(ad_archive_id: str) -> CompetitorAd:
    return next(ad for ad in _observations() if ad.ad_archive_id == ad_archive_id)


# ---------------------------------------------------------------------------
# Coverage of the P0 formats
# ---------------------------------------------------------------------------


def test_all_p0_ads_and_display_formats_normalize():
    ads = _observations()

    assert len(ads) == 128
    assert Counter(ad.display_format for ad in ads) == {"DCO": 56, "VIDEO": 48, "DPA": 13, "IMAGE": 9, "CAROUSEL": 2}
    assert all(ad.provider == "mock" and ad.observed_at == CAPTURED for ad in ads)
    assert all(ad.raw_ref.startswith("blob:") and ad.raw_path.startswith(("results[", "searchResults[")) for ad in ads)


def test_the_snapshot_is_card_zero_and_each_variant_follows_it():
    for ad in _observations():
        raw = _raw_ads()[ad.ad_archive_id]
        assert len(ad.cards) == 1 + len(raw["snapshot"]["cards"])
        assert [card.index for card in ad.cards] == list(range(len(ad.cards)))
        for card in ad.cards:
            prefix = "snapshot." if card.index == 0 else f"snapshot.cards[{card.index - 1}]."
            assert all(source.startswith(prefix) for source in card.field_sources.values())
            if card.index == 0:
                assert not any(source.startswith("snapshot.cards") for source in card.field_sources.values())


def test_every_present_field_names_its_source_and_absent_fields_have_none():
    for ad in _observations():
        for name in ("collation_id", "collation_count", "page_profile_uri", "is_active", "end_date", "landing_domain"):
            assert (getattr(ad, name) is None) == (name not in ad.field_sources), (ad.ad_archive_id, name)
        for card in ad.cards:
            for name in ("title", "body", "cta_text", "cta_type", "link_url", "link_description",
                         "image_ref", "video_poster_ref"):
                assert (getattr(card, name) is None) == (name not in card.field_sources)


def test_dco_top_level_is_card_zero_with_two_variant_cards():
    ad = _ad("920000000000004")

    assert ad.display_format == "DCO" and len(ad.cards) == 3
    top, first, second = ad.cards
    assert top.title == "One Kit. Every Charger." and top.cta_type == "SHOP_NOW"
    assert top.body.startswith("Quit worrying about compatibility.")
    assert top.field_sources["body"] == "snapshot.body.text"
    # The DCO's images live on its variant cards only; card 0 does not borrow one.
    assert top.image_ref is None and "image_ref" not in top.field_sources
    assert first.title == top.title and first.field_sources["title"] == "snapshot.cards[0].title"
    assert first.image_ref.source_field == "snapshot.cards[0].original_image_url"
    assert first.image_ref.expires_at == datetime(2026, 10, 30, 19, 59, 18, tzinfo=timezone.utc)
    assert second.image_ref.url != first.image_ref.url
    assert ad.advertiser_page_id == ACME and ad.advertiser_name == "Acme Charge"
    assert ad.page_categories == ("Shopping", "Cars", "Electric Vehicle Charging Station")
    assert ad.publisher_platforms == ("FACEBOOK", "INSTAGRAM", "MESSENGER", "THREADS")
    assert ad.landing_domain == "acme-charge.example"
    assert ad.field_sources["landing_domain"] == "derived:cards[0].link_url"
    assert ad.library_url == "https://www.facebook.com/ads/library?id=920000000000004"


def test_dco_with_three_variant_cards():
    three = [ad for ad in _observations() if ad.display_format == "DCO" and len(ad.cards) == 4]

    assert len(three) == 24
    # A variant is an image or a video; either way it carries its own media.
    assert all(card.image_ref or card.video_poster_ref for ad in three for card in ad.cards[1:])


def test_video_takes_the_poster_frame_not_an_image():
    ad = _ad("1000000000000241")

    assert ad.display_format == "VIDEO" and len(ad.cards) == 1
    [card] = ad.cards
    assert card.image_ref is None
    assert card.video_poster_ref.source_field == "snapshot.videos[0].video_preview_image_url"
    assert card.video_poster_ref.url.startswith("https://media.mock.invalid/competitor-media/video-poster/")
    assert card.title == "Quick and affordable EV charging gear"
    assert card.link_url == "https://acme-charge.example/collections/ev-chargers"


def test_image_takes_the_snapshot_image():
    ad = _ad("1000000000000265")

    assert ad.display_format == "IMAGE" and ad.advertiser_name == "Brightvolt"
    [card] = ad.cards
    assert card.image_ref.source_field == "snapshot.images[0].original_image_url"
    assert card.link_description.startswith("The first UL2252/CSA-C22.2 certified CCS1 to NACS adapter.")
    assert (card.cta_text, card.cta_type) == ("Order now", "ORDER_NOW")
    assert ad.landing_domain == "brightvolt.example"


def test_dpa_keeps_a_missing_collation_missing():
    ad = _ad("1000000000000253")

    assert ad.display_format == "DPA"
    assert ad.collation_count is None and "collation_count" not in ad.field_sources
    assert [card.title for card in ad.cards[1:3]] == ["V2L (NACS) | Vehicle-To-Load", "J1772 V2L | Vehicle-To-Load"]
    assert ad.cards[1].link_url == "https://brightvolt.example/products/v2l-nacs"
    assert ad.landing_domain == "brightvolt.example"


def test_carousel_blank_copy_is_absent_not_blank():
    ad = _ad("1000000000000371")

    assert ad.display_format == "CAROUSEL" and len(ad.cards) == 6
    assert all(card.body is None and card.link_description is None for card in ad.cards)
    assert ad.cards[0].title == "Skip the wait — order yours now!"
    # Derived from card 0; the variants link to another shop domain.
    assert ad.landing_domain == "brightvolt.example"
    assert landing_domain(ad.cards[1].link_url) == "brightvoltshop.example"


def test_an_ad_without_body_has_no_body_anywhere():
    ad = _ad("1000000000100022")

    assert ad.display_format == "DCO"
    assert all(card.body is None and "body" not in card.field_sources for card in ad.cards)
    assert all(card.title for card in ad.cards)


def test_collated_ads_keep_their_collation():
    collated = [ad for ad in _observations() if (ad.collation_count or 0) > 1]

    assert len(collated) == 21
    assert _ad("1000000000000293").collation_count == 20
    assert _ad("1000000000000293").collation_id == "1000000000000276"


def test_dates_and_days_running():
    ad = _ad("920000000000004")

    assert ad.start_date == datetime(2026, 3, 20, 7, tzinfo=timezone.utc)
    assert ad.end_date == datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
    assert ad.field_sources["start_date"] == "start_date_string"
    assert ad.days_running == 220
    assert ad.field_sources["days_running"] == "derived:observed_at-start_date"
    assert ad.is_active is True


# ---------------------------------------------------------------------------
# Advertiser identity in partnership ads
# ---------------------------------------------------------------------------


def _partnership_ids(creator_in: str) -> list[str]:
    ids = []
    for ad_id, item in _raw_ads().items():
        snapshot, advertiser = item["snapshot"], item["page_id"]
        branded = snapshot.get("branded_content") or {}
        creator_is_snapshot = snapshot["page_id"] != advertiser and branded.get("page_id") == advertiser
        creator_is_branded = snapshot["page_id"] == advertiser and branded and branded["page_id"] != advertiser
        if (creator_in == "snapshot" and creator_is_snapshot) or (creator_in == "branded" and creator_is_branded):
            ids.append(ad_id)
    return ids


def test_a_partnership_ad_takes_the_advertisers_page_not_the_creators():
    ids = _partnership_ids("snapshot")
    assert len(ids) == 16

    for ad in map(_ad, ids):
        raw = _raw_ads()[ad.ad_archive_id]
        assert ad.advertiser_page_id == raw["page_id"] and ad.advertiser_name == raw["page_name"]
        assert ad.field_sources["page_profile_uri"] == "snapshot.branded_content.page_profile_uri"
        assert ad.page_profile_uri == raw["snapshot"]["branded_content"]["page_profile_uri"]
        # The advertiser's categories are not in the answer; the creator's are not borrowed.
        assert ad.page_categories == () and "page_categories" not in ad.field_sources
        assert "Creator Example" not in repr(ad)


def test_a_creator_under_branded_content_is_ignored_too():
    ids = _partnership_ids("branded")
    assert ids

    for ad in map(_ad, ids):
        raw = _raw_ads()[ad.ad_archive_id]
        assert ad.field_sources["page_profile_uri"] == "snapshot.page_profile_uri"
        assert ad.page_profile_uri == raw["snapshot"]["page_profile_uri"]
        assert "creator.example" not in ad.page_profile_uri


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------


def test_an_ad_found_by_page_and_keyword_keeps_both_origins():
    both = [ad for ad in _observations() if len(ad.origins) > 1]
    by_page = [ad for ad in both if page_origin(f"cmp_{ACME}") in ad.origins]

    assert both and by_page
    assert all(len(set(ad.origins)) == len(ad.origins) for ad in both)
    for ad in by_page:
        # Pages are fetched before keywords, so the page origin comes first.
        assert ad.origins[0] == page_origin(f"cmp_{ACME}")
        assert all(origin.startswith("keyword:") for origin in ad.origins[1:])
    # Only 50 of Acme Charge's 222 ads were paged; the keywords found others.
    assert any(ad.advertiser_page_id == ACME and ad.origins[0].startswith("keyword:") for ad in both)


def test_merge_keeps_the_first_observation_and_unions_origins():
    first = normalize_ad({"ad_archive_id": "7", "page_name": "first"}, provider="mock",
                         origin=page_origin("cmp_1"), observed_at=CAPTURED)
    second = normalize_ad({"ad_archive_id": "7", "page_name": "second"}, provider="mock",
                          origin=keyword_origin(" NACS adapter "), observed_at=CAPTURED)
    other = normalize_ad({"ad_archive_id": "8"}, provider="mock", origin=page_origin("cmp_1"), observed_at=CAPTURED)

    merged = merge_observations([first, second, other, first])

    assert [ad.ad_archive_id for ad in merged] == ["7", "8"]
    assert merged[0].advertiser_name == "first"
    assert merged[0].origins == ("page:cmp_1", "keyword:NACS adapter")


def test_origins_need_a_value():
    with pytest.raises(ValueError):
        page_origin("")
    with pytest.raises(ValueError):
        keyword_origin("  ")


# ---------------------------------------------------------------------------
# The /ad detail shape
# ---------------------------------------------------------------------------


def _without_provenance(ad: CompetitorAd) -> dict:
    data = {name: getattr(ad, name) for name in ad.__dataclass_fields__}
    for name in ("field_sources", "raw_path", "raw_ref"):
        data.pop(name)
    data["cards"] = [
        {name: getattr(card, name) for name in card.__dataclass_fields__ if name != "field_sources"}
        for card in ad.cards
    ]
    return data


def test_an_ad_detail_normalizes_to_the_same_observation_as_its_listing():
    response = asyncio.run(_source().ad_detail("920000000000004"))
    [detail] = normalize_ads(response, origin=page_origin(f"cmp_{ACME}"), observed_at=CAPTURED)
    listing = _ad("920000000000004")

    assert _without_provenance(detail) == _without_provenance(listing) | {"origins": (page_origin(f"cmp_{ACME}"),)}
    assert detail.raw_path == ""
    assert detail.field_sources["ad_archive_id"] == "adArchiveID"
    assert detail.field_sources["start_date"] == "startDateString"
    assert detail.field_sources["publisher_platforms"] == "publisherPlatform"
    assert detail.cards[0].field_sources["body"] == "snapshot.body"


def test_the_documented_detail_shape_with_a_sponsor_page():
    detail = {
        "adArchiveID": 702369045530963,
        "collationID": None,
        "collationCount": None,
        "pageID": 166258896830103,
        "pageName": "Sponsor Brand",
        "isActive": False,
        "startDate": 1747897200,
        "endDate": 1747983600,
        "publisherPlatform": ["facebook", "instagram", "audience_network"],
        "url": "https://www.facebook.com/ads/library?id=702369045530963",
        "snapshot": {
            "page_id": 598631456674530,
            "page_name": "Partner Page",
            "page_profile_uri": "https://www.facebook.com/61575286107814/",
            "page_categories": {"197251360301109": "Marina"},
            "branded_content": {
                "page_id": 166258896830103,
                "page_profile_uri": "https://www.facebook.com/sponsorbrand/",
                "page_categories": {"197251360301109": "Marina"},
            },
            "display_format": "video",
            "title": None,
            "body": "Reflections ripple.",
            "cta_type": "LEARN_MORE",
            "link_url": "https://www.example.co.uk/attractions/",
            "cards": [],
            "images": [],
            "videos": [{"video_preview_image_url": "https://media.example/poster.jpg?oe=6843812F"}],
        },
    }

    ad = normalize_ad(detail, provider="scrapecreators", origin=page_origin("cmp_9"), observed_at=CAPTURED)

    assert (ad.ad_archive_id, ad.advertiser_page_id, ad.advertiser_name) == (
        "702369045530963", "166258896830103", "Sponsor Brand",
    )
    assert ad.collation_id is None and ad.collation_count is None
    assert ad.page_profile_uri == "https://www.facebook.com/sponsorbrand/"
    assert ad.page_categories == ("Marina",)
    assert ad.field_sources["page_categories"] == "snapshot.branded_content.page_categories"
    assert ad.start_date == datetime(2025, 5, 22, 7, tzinfo=timezone.utc)
    assert ad.field_sources["start_date"] == "startDate"
    assert ad.display_format == "VIDEO"
    assert ad.publisher_platforms == ("FACEBOOK", "INSTAGRAM", "AUDIENCE_NETWORK")
    assert ad.is_active is False
    [card] = ad.cards
    assert card.title is None and card.body == "Reflections ripple."
    assert card.video_poster_ref.expires_at == datetime.fromtimestamp(0x6843812F, timezone.utc)
    assert ad.landing_domain == "example.co.uk"


# ---------------------------------------------------------------------------
# Nothing guessed, nothing malformed
# ---------------------------------------------------------------------------


def test_an_ad_with_only_an_id_stays_empty():
    ad = normalize_ad({"ad_archive_id": "42"}, provider="mock", origin=page_origin("cmp_1"), observed_at=CAPTURED)

    assert ad.field_sources == {"ad_archive_id": "ad_archive_id"}
    assert ad.cards == () and ad.page_categories == () and ad.publisher_platforms == ()
    assert ad.days_running is None and ad.landing_domain is None and ad.is_active is None


@pytest.mark.parametrize("item", [{"page_name": "no id"}, {"ad_archive_id": "not-a-number"}, ["not", "an", "ad"]])
def test_an_ad_without_an_archive_id_is_rejected(item):
    with pytest.raises(MalformedAdError):
        normalize_ad(item, provider="mock", origin=page_origin("cmp_1"), observed_at=CAPTURED)


def test_observed_at_must_carry_a_timezone():
    with pytest.raises(ValueError):
        normalize_ad({"ad_archive_id": "1"}, provider="mock", origin=page_origin("cmp_1"),
                     observed_at=datetime(2026, 10, 26))


def test_page_search_answers_are_candidates_not_ads():
    candidates = asyncio.run(_source().search_pages("Halcyon Motors"))
    page = asyncio.run(_source().ads_for_page(ACME, country="US"))

    with pytest.raises(ValueError):
        normalize_ads(candidates, origin=page_origin("cmp_1"), observed_at=CAPTURED)
    with pytest.raises(ValueError):
        normalize_page_candidates(page)
    halcyon = normalize_page_candidates(candidates)
    assert [c.name for c in halcyon[:4]] == ["Northpeak", "Yarrow Accessories for Halcyon Motors", "Jordan’s Halcyon Motors Garage", "Halcyon Motors"]
    assert halcyon[3].verification == "BLUE_VERIFIED" and halcyon[3].page_alias == "halcyonmotors"
    assert all("image_uri" not in c.field_sources for c in halcyon)


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://scontent.example/v/x.jpg?_nc_cat=100&oe=6AE4F716", datetime(2026, 10, 30, 19, 59, 18, tzinfo=timezone.utc)),
        ("https://media.mock.invalid/competitor-media/image/abc.png?oe=6ae4f716", datetime(2026, 10, 30, 19, 59, 18, tzinfo=timezone.utc)),
        ("https://scontent.example/v/x.jpg?_nc_cat=100", None),
        ("https://scontent.example/v/x.jpg?oe=not-hex", None),
        ("https://scontent.example/v/x.jpg?oe=12", None),
        (None, None),
    ],
)
def test_media_link_expiry_is_the_hex_oe_timestamp(url, expected):
    assert media_url_expires_at(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme-charge.example/products/x", "acme-charge.example"),
        ("https://www.halcyon-motors.example/", "halcyon-motors.example"),
        ("https://m.bazaar-wholesale.example/lp/group", "bazaar-wholesale.example"),
        ("http://parts.kinsei-usa.example/", "kinsei-usa.example"),
        ("https://acme-charge.co.il/", "acme-charge.co.il"),
        ("https://shop.brand.com.au/x", "brand.com.au"),
        ("https://springfield.onelink.example/sp01/chargepass", "onelink.example"),
        ("https://fb.com/canvas_doc/1000000000000358", "fb.com"),
        ("https://192.0.2.10/x", "192.0.2.10"),
        ("acme-charge.example", None),
        ("", None),
        (None, None),
    ],
)
def test_landing_domain_is_the_registrable_domain(url, expected):
    assert landing_domain(url) == expected


def test_days_running_counts_whole_days_and_never_goes_negative():
    start = datetime(2026, 10, 1, 7, tzinfo=timezone.utc)

    assert days_running(start, start + timedelta(days=30, hours=23)) == 30
    assert days_running(start, start - timedelta(hours=2)) == 0
    assert days_running(None, start) is None
