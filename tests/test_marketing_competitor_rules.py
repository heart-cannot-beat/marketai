"""Issue #273 (C-24): the deterministic competitor rules, without a database.

Same market (the alias table), what the planner reads of a snapshot, and the
three findings spec §7.2 derives from it: crowded and gap angles (advisory)
and reused competitor copy (blocking, rules a and b), with the product's own
page and the allowed names left out. The false-positive check runs every
Acme Charge card of the P0 recordings against an English plan that does not copy.
"""
from __future__ import annotations

import glob
import json
import re
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app import config
from app.marketing import competitor_review as review
from app.marketing.contracts import TestPlan as MarketingTestPlan
from app.marketing.plan_review import with_system_checks
from app.marketing.planner import example_result, planner_plan_schema, system_prompt

P0 = Path(__file__).resolve().parent.parent / "app/competitors/sources/fixtures/scrapecreators_p0"
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
ALIASES = json.dumps({"北美": "US", "North America": "US", "美国": "US"})
UNDERSTANDING = {"product_name": "Brightvolt NACS Adapter", "audience": "EV drivers", "facts": [
    {"id": "f1", "statement": "UL 2252 certified", "source_quote": "UL 2252 certified", "confirmation": "confirmed"}]}


def ad(advertiser, *, role="competitor", titles=(), bodies=(), days=10, page=None):
    return {"advertiser_name": advertiser, "advertiser_page_id": page or f"page-{advertiser}", "role": role,
            "titles": list(titles), "bodies": list(bodies), "days_running": days,
            "library_url": f"https://www.facebook.com/ads/library/?id={advertiser}-{days}"}


def cluster(cluster_id, ads, members, claim="A claim"):
    advertisers = {}
    for ad_id in members:
        entry = ads[ad_id]
        advertisers.setdefault(entry["advertiser_page_id"], {"advertiser_page_id": entry["advertiser_page_id"],
                               "advertiser_name": entry["advertiser_name"], "role": entry["role"]})
    return {"cluster_id": cluster_id, "claim": claim, "ad_ids": list(members), "observation_count": len(members),
            "advertisers": list(advertisers.values()),
            "advertiser_count": sum(1 for a in advertisers.values() if a["role"] != "self"),
            "self_included": any(a["role"] == "self" for a in advertisers.values()),
            "max_days_running": max(ads[m]["days_running"] for m in members),
            "library_urls": [ads[m]["library_url"] for m in members]}


def content(ads, clusters, *, language="en", mock=False, relevant=None):
    relevant = list(relevant if relevant is not None else ads)
    return {"version": "competitor-snapshot/v1", "language": language, "mock": mock, "ads": ads,
            "relevant_ad_ids": relevant, "claim_clusters": clusters, "counts": {"relevant": len(relevant)},
            "unclustered_ad_ids": [], "long_running_ad_ids": [], "long_running_days": 60, "adjacent_ad_ids": []}


def cite(frozen, allowed=("Brightvolt NACS Adapter",)):
    return review.Citation("csnap_test", "US", frozen, allowed=tuple(allowed))


def plan(**combo_fields):
    """The Controlled Mock's English plan, its first combination's copy replaced."""
    document = example_result("plan", {"understanding": UNDERSTANDING, "ad_language": "en-US", "strategy_language": "en-US"})
    document["combinations"][0].update(combo_fields)
    return MarketingTestPlan.model_validate(document)


def kinds(items):
    return [item.kind for item in items]


class SameMarketTests(unittest.TestCase):
    def test_an_alias_table_maps_free_text_after_lower_casing_and_removing_whitespace(self):
        with patch.object(config, "MARKET_COUNTRY_ALIASES_JSON", ALIASES):
            self.assertEqual(review.country_for("北美"), "US")
            self.assertEqual(review.country_for(" north   america "), "US")
            self.assertEqual(review.country_for("NORTHAMERICA"), "US")
            self.assertIsNone(review.country_for("火星"))
            self.assertIsNone(review.country_for(""))

    def test_without_a_table_only_a_two_letter_code_matches(self):
        with patch.object(config, "MARKET_COUNTRY_ALIASES_JSON", "{}"):
            self.assertEqual(review.country_for("us"), "US")
            self.assertEqual(review.country_for(" GB "), "GB")
            self.assertIsNone(review.country_for("北美"))
            self.assertIsNone(review.country_for("USA"))
        for broken in ("not json", "[]", json.dumps({"北美": "USA", "美国": 1})):
            with patch.object(config, "MARKET_COUNTRY_ALIASES_JSON", broken):
                self.assertIsNone(review.country_for("北美"), broken)


class PlannerInputTests(unittest.TestCase):
    def test_the_planner_reads_counts_and_claims_but_no_copy_or_media(self):
        ads = {"a1": ad("Acme Charge", titles=["One Kit. Every Charger."], bodies=["Quit worrying about compatibility."]),
               "a2": ad("Northpeak", titles=["Safe charging"], days=90)}
        frozen = content(ads, [cluster("c1", ads, ["a1", "a2"], claim="x" * 500)], language="zh-CN")
        frozen.update(long_running_ad_ids=["a2"], unclustered_ad_ids=[], adjacent_ad_ids=[])
        row = type("Row", (), {"id": "csnap_1", "market": "US", "summary_json": frozen})()
        read = review.planner_input(row)
        self.assertEqual((read["snapshot_id"], read["country"], read["language"]), ("csnap_1", "US", "zh-CN"))
        [only] = read["claim_clusters"]
        self.assertEqual(len(only["claim"]), review.MAX_CLAIM_CHARS)
        self.assertEqual((only["advertiser_count"], only["max_days_running"]), (2, 90))
        self.assertEqual(read["long_running"]["cluster_ids"], ["c1"])
        text = json.dumps(read, ensure_ascii=False)
        for copy in ("One Bundle", "Stop worrying", "Safe charging", "media", "titles", "bodies"):
            self.assertNotIn(copy, text)

    def test_without_a_snapshot_the_prompt_and_schema_name_no_competitor(self):
        inputs = {"strategy_language": "en-US", "ad_language": "en-US"}
        self.assertNotIn("competitor", system_prompt("plan", inputs))
        self.assertNotIn("competitor", json.dumps(planner_plan_schema()))
        with_snapshot = system_prompt("plan", {**inputs, "competitor_snapshot": {"claim_clusters": []}})
        self.assertIn("competitor_overlap", with_snapshot)
        self.assertIn("「Competitor observation」", with_snapshot)
        self.assertIn("用en-US转述竞品主张", with_snapshot)
        schema = planner_plan_schema(competitor=True)
        self.assertIn("competitor_overlap", schema["$defs"]["Angle"]["properties"])
        # The server, not the model, writes the citation and every competitor finding.
        self.assertNotIn("competitor_snapshot_id", schema["properties"])
        self.assertNotIn("observations", schema["$defs"]["ReviewItem"]["properties"])
        self.assertFalse(set(schema["$defs"]["ReviewItem"]["properties"]["kind"]["enum"]) & set(review.COMPETITOR_KINDS))


class CopyReuseTests(unittest.TestCase):
    def setUp(self):
        self.ads = {
            "acme": ad("Acme Charge", titles=["Tired of Compatibility Headaches? Not Anymore.", "Fast Charge"],
                          bodies=["Acme Charge offers reliable and convenient solutions for all your charging needs. Shop now."]),
            "zh": ad("某品牌", titles=["一套转接头覆盖所有充电桩"], bodies=["告别充电焦虑，一套转接头让你在任何充电桩都能放心充电"]),
            "self": ad("Brightvolt", role="self", titles=["Enjoy Charging Without Limits."],
                       bodies=["Charge your EV anywhere with the adapter built for every road trip you take"]),
            "adjacent": ad("Other", titles=["Charge Smarter Every Day Now"]),
        }
        self.frozen = content(self.ads, [], relevant=["acme", "zh", "self"])

    def hits(self, **fields):
        return review.copy_reuse(plan(**fields), cite(self.frozen))

    def test_rule_a_a_whole_headline_of_three_words_is_reuse_whatever_the_case_and_punctuation(self):
        [hit] = self.hits(headline="tired of compatibility headaches — not anymore!")
        self.assertEqual((hit.fields, hit.ad_ids), (["headline"], ["acme"]))
        self.assertEqual(self.hits(headline="Fast charge"), [])  # two words
        self.assertEqual(self.hits(headline="Tired of compatibility headaches? Not yet."), [])  # not the whole title
        self.assertEqual(self.hits(headline="Charge Smarter Every Day Now"), [])  # an adjacent observation

    def test_rule_a_counts_six_cjk_characters(self):
        [hit] = self.hits(headline="一套转接头覆盖所有充电桩")
        self.assertEqual(hit.ad_ids, ["zh"])
        short = deepcopy(self.frozen)
        short["ads"]["zh"]["titles"] = ["转接头全覆盖"]  # six
        self.assertEqual(len(review.copy_reuse(plan(headline="转接头全覆盖"), cite(short))), 1)
        short["ads"]["zh"]["titles"] = ["转接头覆盖"]  # five
        self.assertEqual(review.copy_reuse(plan(headline="转接头覆盖"), cite(short)), [])

    def test_rule_b_eight_consecutive_words_of_a_title_or_body(self):
        [hit] = self.hits(body_copy="We offer reliable and convenient solutions for all your charging needs, at home.")
        self.assertEqual((hit.fields, hit.ad_ids), (["body_copy"], ["acme"]))
        # Seven words in a row are not enough.
        self.assertEqual(self.hits(body_copy="Reliable and convenient solutions for all your needs, every day."), [])
        [hit] = self.hits(body_copy="告别充电焦虑，一套转接头让你在任何地方都能充电")  # 12 identical CJK characters
        self.assertEqual(hit.ad_ids, ["zh"])
        self.assertEqual(self.hits(body_copy="一套转接头让你在任何地方安心"), [])  # 10

    def test_the_products_own_page_is_never_compared(self):
        self.assertEqual(self.hits(headline="Enjoy Charging Without Limits."), [])
        self.assertEqual(self.hits(body_copy="Charge your EV anywhere with the adapter built for every road trip you take"), [])

    def test_names_certifications_and_calls_to_action_do_not_count(self):
        frozen = deepcopy(self.frozen)
        frozen["ads"]["acme"]["titles"].append("Brightvolt Shop Now")
        frozen["ads"]["acme"]["titles"].append("UL 2252 Learn More")
        self.assertEqual(review.copy_reuse(plan(headline="Brightvolt — shop now"), cite(frozen)), [])
        self.assertEqual(review.copy_reuse(plan(headline="UL 2252: learn more"), cite(frozen)), [])
        # Allowed words are left out of a run, so they neither count nor break it: 8 counted words remain.
        frozen["ads"]["acme"]["bodies"] = ["the brightvolt adapter lets every driver charge at more public stations"]
        [hit] = review.copy_reuse(plan(body_copy="The Brightvolt adapter lets every driver charge at more public stations."),
                                  cite(frozen))
        self.assertEqual(hit.fields, ["body_copy"])
        frozen["ads"]["acme"]["bodies"] = ["the brightvolt adapter lets every driver charge at stations"]
        self.assertEqual(review.copy_reuse(plan(body_copy="The Brightvolt adapter lets every driver charge at stations."),
                                           cite(frozen)), [])  # seven counted words

    def test_the_p0_acme_copy_does_not_flag_an_english_plan_that_does_not_copy(self):
        ads = {}
        for path in sorted(glob.glob(str(P0 / "company-ads--acme-charge--p*.json"))):
            for n, raw in enumerate(json.loads(Path(path).read_text())["body"]["results"]):
                snap = raw.get("snapshot") or {}
                body = snap.get("body")
                cards = [{"title": snap.get("title"), "body": body.get("text") if isinstance(body, dict) else body},
                         *(snap.get("cards") or [])]
                ads[f"{Path(path).stem}-{n}"] = ad("Acme Charge", titles=[c["title"] for c in cards if c.get("title")],
                                                   bodies=[c["body"] for c in cards if c.get("body")])
        self.assertGreater(len(ads), 40)
        honest = example_result("plan", {"understanding": UNDERSTANDING, "ad_language": "en-US", "strategy_language": "en-US"})
        copy = [("One adapter for CCS1 cars at Halcyon Motors stations", "Plug a CCS1 car into the Halcyon Motors network with a compact NACS adapter rated to UL 2252."),
                ("Road trips with more places to charge", "Find more charging stops on long drives; check your car's compatibility list before you buy."),
                ("Charge safely in rain or snow", "A sealed housing and a firm latch keep the connection steady through winter weather."),
                ("Know what fits before you buy", "Our guide lists the models and chargers this adapter supports, and those it does not."),
                ("Fewer detours, more charging", "Use the fast chargers already on your route instead of planning around one network.")]
        for combo, (headline, body) in zip(honest["combinations"], copy):
            combo.update(headline=headline, body_copy=body)
        frozen = content(ads, [])
        document = MarketingTestPlan.model_validate(honest)
        self.assertEqual(review.copy_reuse(document, cite(frozen)), [])
        # ... while the same corpus flags a headline lifted from it.
        lifted = deepcopy(honest)
        lifted["combinations"][0]["headline"] = "Unlock Halcyon Supercharging for Non-Halcyon EVs"
        self.assertEqual(len(review.copy_reuse(MarketingTestPlan.model_validate(lifted), cite(frozen))), 1)


class CrowdedAndGapTests(unittest.TestCase):
    def setUp(self):
        ads = {"l": ad("Acme Charge", days=200), "t": ad("Northpeak", days=40), "u": ad("Corvane", days=12),
               "l2": ad("Acme Charge", days=30), "s": ad("Brightvolt", role="self", days=500), "r": ad("Roadside", days=5)}
        self.ads = ads
        self.frozen = content(ads, [cluster("c1", ads, ["l", "t", "s"]), cluster("c2", ads, ["u", "l2"]),
                                    cluster("c3", ads, ["s"]), cluster("c4", ads, ["r"])])

    def items(self, overlaps, language="en-US", frozen=None):
        document = plan()
        document = document.model_copy(update={"strategy_language": language, "angles": [
            angle.model_copy(update={"competitor_overlap": overlap}) for angle, overlap in zip(document.angles, overlaps)]})
        return review.review_items(document, cite(frozen or self.frozen))

    def test_merged_clusters_with_three_advertisers_besides_the_own_page_are_crowded(self):
        [crowded, gap] = self.items([["c1", "c2"], []])
        self.assertEqual((crowded.kind, crowded.severity, crowded.source), ("competitor_crowded", "advisory", "system"))
        self.assertEqual(crowded.target_ids, ["a1"])
        # Acme Charge runs in both clusters and counts once; the own page's 500-day ad is not a competitor's.
        self.assertIn("3 advertisers other than your own page", crowded.finding)
        self.assertIn("(4 observations, longest running 200 days, US)", crowded.finding)
        # One observation per advertiser, longest running first; never the own page.
        self.assertEqual([o.ad_id for o in crowded.observations], ["l", "t", "u"])
        self.assertEqual(crowded.observations[0].library_url, self.ads["l"]["library_url"])
        self.assertEqual((gap.kind, gap.severity, gap.target_ids), ("competitor_gap", "advisory", ["a2"]))
        self.assertIn("This may be white space, or others may have tried it without success.", gap.finding)

    def test_the_own_page_and_a_repeated_advertiser_count_once_or_not_at_all(self):
        # c1 alone: Acme Charge and Northpeak (the own page left out) -> not crowded; c3 holds only the own page.
        self.assertEqual(kinds(self.items([["c1"], ["c3", "c4"]])), [])
        # c2 alone holds two advertisers; with c4 a third one.
        self.assertEqual(kinds(self.items([["c2"], ["c2", "c4"]])), ["competitor_crowded"])

    def test_an_unknown_cluster_id_is_not_a_citation(self):
        self.assertEqual(kinds(self.items([["c9"], ["c4"]])), ["competitor_gap"])
        document = plan().model_copy(update={"angles": [a.model_copy(update={"competitor_overlap": ["c9", "c1", "c1"]})
                                                         for a in plan().angles]})
        cited = review.with_citation(document, cite(self.frozen))
        self.assertEqual([a.competitor_overlap for a in cited.angles], [["c1"], ["c1"]])
        self.assertTrue(all(a.rationale.endswith(" (Competitor observation: c1)") for a in cited.angles))
        self.assertEqual(cited.competitor_snapshot_id, "csnap_test")

    def test_an_english_plan_reads_a_chinese_snapshot_in_english(self):
        frozen = deepcopy(self.frozen)
        frozen.update(language="zh-CN", mock=True)
        for item in frozen["claim_clusters"]:
            item["claim"] = "一套转接头覆盖所有充电桩（受控 Mock）"
        items = self.items([["c1", "c2"], []], frozen=frozen)
        self.assertEqual(kinds(items), ["competitor_crowded", "competitor_gap"])
        for item in items:
            self.assertIsNone(CJK.search(item.finding + item.suggestion), item.finding)
            self.assertTrue(item.finding.endswith("(The competitor data is a Controlled Mock simulation.)"))
        zh = self.items([["c1", "c2"], []], language="zh-CN")
        self.assertIn("可能是空白，也可能别人试过无效。", zh[1].finding)

    def test_system_checks_put_competitor_findings_last_and_drop_forged_ones(self):
        document = plan(headline="Tired of Compatibility Headaches? Not Anymore.")
        forged = {"id": "r9", "kind": "competitor_crowded", "severity": "advisory", "target_ids": [], "finding": "x",
                  "suggestion": "y", "source": "ai", "observations": [{"ad_id": "l"}]}
        document = MarketingTestPlan.model_validate({**document.model_dump(mode="json"),
                                                     "plan_review": [*document.model_dump(mode="json")["plan_review"], forged]})
        self.frozen["ads"]["l"]["titles"] = ["Tired of Compatibility Headaches? Not Anymore."]
        checked = with_system_checks(review.with_citation(document, cite(self.frozen)), citation=cite(self.frozen))
        found = [(r.source, r.kind) for r in checked.plan_review]
        self.assertNotIn(("ai", "competitor_crowded"), found)
        competitor = [i for i, (_, kind) in enumerate(found) if kind in review.COMPETITOR_KINDS]
        self.assertEqual(competitor, list(range(len(found) - len(competitor), len(found))))
        self.assertEqual(found[competitor[0]], ("system", "competitor_copy_reuse"))
        # Without a citation the checks are exactly what they were.
        self.assertEqual(with_system_checks(plan()), with_system_checks(plan(), citation=None))
        self.assertFalse({r.kind for r in with_system_checks(plan()).plan_review} & set(review.COMPETITOR_KINDS))
