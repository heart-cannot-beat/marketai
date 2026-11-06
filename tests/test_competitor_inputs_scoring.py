"""Issue #269 -- competitor inputs (spec §4.1) and candidate scoring (§4.2), pure functions.

The P0 recordings of the Controlled Mock are the scoring fixtures: the
Acme Charge search returns eight Acme Charge* pages, and the Brightvolt search ranks a
Czech event page first.
"""
from __future__ import annotations

import asyncio
import unittest

from app.competitors import scoring
from app.competitors.inputs import (
    CompetitorInputError,
    normalize_name,
    normalize_website,
    parse_input,
    parse_page_link,
)
from app.competitors.resolve_worker import _probe_summary, _search_candidates
from app.competitors.sources.mock import MockAdLibrarySource

ACME = "100000000000010"
BRIGHTVOLT = "100000000000016"


def _refused(fn, value) -> CompetitorInputError:
    with unittest.TestCase().assertRaises(CompetitorInputError) as caught:
        fn(value)
    return caught.exception


class InputTests(unittest.TestCase):
    def test_an_ad_library_link_yields_the_page_id(self):
        parsed = parse_input(
            "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US"
            f"&view_all_page_id={ACME}&search_type=page"
        )
        self.assertEqual((parsed.kind, parsed.page_id, parsed.name), ("ad_library", ACME, None))
        self.assertTrue(parsed.is_link)

    def test_page_links_with_an_id_yield_it(self):
        for link in (
            f"https://www.facebook.com/profile.php?id={ACME}",
            f"facebook.com/people/Acme-Charge/{ACME}/",
            f"https://m.facebook.com/Acme-Charge-EV-{ACME}",
            f"https://web.facebook.com/{ACME}",
            f"https://www.fb.com/pages/Acme-Charge/{ACME}",
        ):
            parsed = parse_input(link)
            self.assertEqual((parsed.kind, parsed.page_id, parsed.page_alias), ("facebook_page", ACME, None), link)

    def test_an_alias_link_keeps_the_alias_without_an_id(self):
        parsed = parse_input("https://www.facebook.com/acmecharge/about")
        self.assertEqual((parsed.kind, parsed.page_id, parsed.page_alias), ("facebook_page", None, "acmecharge"))

    def test_a_facebook_link_that_names_no_page_is_refused(self):
        for link in (
            "https://www.facebook.com/ads/library/?id=123456789",
            "https://www.facebook.com/groups/123456789",
            "https://www.facebook.com/share/p/abc/",
            "https://www.facebook.com/",
        ):
            self.assertEqual(_refused(parse_input, link).code, "competitor_link_unrecognized", link)

    def test_a_website_alone_asks_for_a_name(self):
        for value in ("acme-charge.example", "https://www.acme-charge.example/collections"):
            error = _refused(parse_input, value)
            self.assertEqual(error.code, "competitor_name_required")
            self.assertEqual(error.params, {"website_domain": "acme-charge.example"})

    def test_a_name_is_kept_with_its_spaces_collapsed(self):
        parsed = parse_input("  Acme   Charge ")
        self.assertEqual((parsed.kind, parsed.name), ("name", "Acme Charge"))
        self.assertEqual(normalize_name("Acme Charge"), normalize_name("acme-charge!"))
        self.assertEqual(_refused(parse_input, "  ").code, "competitor_input_required")
        self.assertEqual(_refused(parse_input, "!!!").code, "competitor_input_required")
        too_long = _refused(parse_input, "x" * 201)
        self.assertEqual((too_long.code, too_long.params), ("competitor_name_too_long", {"limit": 200}))

    def test_websites_normalize_to_their_registrable_domain(self):
        self.assertEqual(normalize_website("https://www.acme-charge.example/x?y=1"), "acme-charge.example")
        self.assertEqual(normalize_website("shop.brightvolt.co.uk"), "brightvolt.co.uk")
        self.assertIsNone(normalize_website("  "))
        for value in ("not a site", "https://facebook.com/acmecharge", "instagram.com/x"):
            self.assertEqual(_refused(normalize_website, value).code, "competitor_website_invalid", value)

    def test_a_confirmation_link_must_carry_a_page_id(self):
        self.assertEqual(parse_page_link(f"https://facebook.com/profile.php?id={BRIGHTVOLT}").page_id, BRIGHTVOLT)
        error = _refused(parse_page_link, "https://www.facebook.com/brightvolt")
        self.assertEqual((error.code, error.params), ("competitor_link_needs_page_id", {"page_alias": "brightvolt"}))
        self.assertEqual(_refused(parse_page_link, "Acme Charge").code, "competitor_link_unrecognized")


def _search(query: str) -> list[dict]:
    return _search_candidates(asyncio.run(MockAdLibrarySource().search_pages(query)))


def _probes(page_ids, country="US") -> dict:
    source = MockAdLibrarySource()
    return {
        page_id: _probe_summary(
            asyncio.run(source.ads_for_page(page_id, country=country, operation="probe")), country,
        )
        for page_id in page_ids
    }


class ScoringTests(unittest.TestCase):
    def test_categories_are_filtered_coarsely(self):
        self.assertEqual(scoring.category_class("Shopping & Retail"), "commercial")
        self.assertEqual(scoring.category_class("Automotive Parts Store"), "commercial")
        for category in ("Event", "Elementary School", "Fan page", "Political Organization", "Just For Fun"):
            self.assertEqual(scoring.category_class(category), "excluded", category)
        self.assertEqual(scoring.category_class("Electrician"), "other")
        self.assertEqual(scoring.category_class(None), "other")

    def test_acme_eight_namesakes_the_true_page_ranks_first(self):
        candidates = _search("Acme Charge")
        namesakes = [item for item in candidates if (item["page_name"] or "").startswith("Acme Charge")]
        self.assertEqual(len(namesakes), 8)
        scored = scoring.score(candidates, query="Acme Charge")
        # Likes never outrank the name: the 273k-like Acme Charge Fuel Systems is not an exact match.
        fuel = next(item for item in scored if item.candidate["page_name"] == "Acme Charge Fuel Systems")
        self.assertEqual(fuel.name_match, "partial")
        plan = scoring.probe_plan(scored)
        self.assertEqual(plan[0], ACME)
        self.assertEqual(len(plan), 3)
        ranked = scoring.rank(scored, probes=_probes(plan), website_domain="acme-charge.example")
        self.assertEqual((ranked[0]["page_id"], ranked[0]["page_alias"]), (ACME, "acmecharge"))
        self.assertTrue(ranked[0]["domain_hit"])
        self.assertEqual(ranked[0]["probe"]["landing_domains"], ["acme-charge.example"])
        self.assertEqual(ranked[0]["probe"]["total_active_ads"], 222)
        self.assertEqual([item["domain_hit"] for item in ranked[1:3]], [False, False])
        self.assertEqual(scoring.automatic_match(ranked, by_alias=False)["page_id"], ACME)

    def test_brightvolt_ranks_above_the_czech_event_page(self):
        candidates = _search("Brightvolt")
        self.assertEqual(candidates[0]["category"], "Event")
        self.assertEqual(candidates[4]["page_id"], BRIGHTVOLT)
        scored = scoring.score(candidates, query="Brightvolt")
        event = next(item for item in scored if item.candidate["provider_rank"] == 0)
        self.assertEqual(event.excluded_reason, "category")
        plan = scoring.probe_plan(scored)
        self.assertEqual(plan[0], BRIGHTVOLT)
        ranked = scoring.rank(scored, probes=_probes(plan), website_domain="brightvolt.example")
        self.assertEqual(ranked[0]["page_id"], BRIGHTVOLT)
        self.assertEqual(ranked[0]["probe"]["landing_domains"], ["brightvolt.example"])
        self.assertTrue(ranked[0]["domain_hit"])
        self.assertEqual(ranked[-1]["excluded_reason"], "category")

    def test_without_a_website_nothing_is_confirmed_automatically(self):
        scored = scoring.score(_search("Acme Charge"), query="Acme Charge")
        ranked = scoring.rank(scored, probes=_probes(scoring.probe_plan(scored)), website_domain=None)
        self.assertEqual(ranked[0]["page_id"], ACME)
        self.assertIsNone(ranked[0]["domain_hit"])
        self.assertIsNone(scoring.automatic_match(ranked, by_alias=False))

    def test_a_pasted_alias_ranks_its_page_first_and_matches_alone(self):
        scored = scoring.score(_search("Acme Charge"), query="acmecharge", alias="acmecharge")
        ranked = scoring.rank(scored, probes={}, website_domain=None)
        self.assertTrue(ranked[0]["alias_match"])
        self.assertEqual(scoring.automatic_match(ranked, by_alias=True)["page_id"], ACME)

    def test_deleted_pages_are_excluded_and_two_matches_are_ambiguous(self):
        cards = [
            {"page_id": "1", "page_name": "Acme", "category": "Brand", "likes": 5, "provider_rank": 0,
             "page_is_deleted": True},
            {"page_id": "2", "page_name": "Acme", "category": "Brand", "likes": 1, "provider_rank": 1},
            {"page_id": "3", "page_name": "ACME", "category": "Brand", "likes": 9, "provider_rank": 2},
        ]
        scored = scoring.score(cards, query="acme")
        self.assertEqual(scored[-1].excluded_reason, "deleted")
        self.assertEqual(scoring.probe_plan(scored), ["3", "2"])  # likes break the tie
        probes = {page: {"status": "succeeded", "landing_domains": ["acme.com"]} for page in ("2", "3")}
        ranked = scoring.rank(scored, probes=probes, website_domain="acme.com")
        self.assertIsNone(scoring.automatic_match(ranked, by_alias=False))


if __name__ == "__main__":
    unittest.main()
