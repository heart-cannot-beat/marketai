"""Taking kept ads to a test, before it starts (ADR 0019 §2, #248): the plan written by template and the quote.

The plan is written from the kept ads and the user's answers without an AI call, and it passes every check a
confirmed plan-first plan passes (``TestPlan`` and ``service.validate_plan(confirming=True)``). The quote says
what the round compares, what each compared object gets, whether that is enough for a verdict, when the result
is due and what the "look together" check costs at most. Reading it writes nothing.

The template tests build the plan from a fictional Acme Charge charger's ideas; the endpoint tests use the ads a
Controlled Mock first batch made for Acme Charge Home (see ``tests.test_marketing_ideas_export``).
"""
import unittest
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import func, select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.marketing import ad_test_plans, ad_tests, service
from app.marketing.ad_tests_contracts import MarketingTestAnswers, MarketingTestQuote
from app.marketing.contracts import TestPlan as MarketingTestPlan
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord
from app.marketing.workflow_contracts import EvaluationProtocol
from tests.test_marketing_ideas_export import IdeasExportFixtures
from tests.test_marketing_tests import MarketingTestFixtures

KeptAd = ad_test_plans.KeptAd


def charger_ideas(strategy_language="en-US"):
    """Acme Charge Travel Charger: two buying claims, two ways of telling each, one ad per way."""
    zh = strategy_language == "zh-CN"

    def expression(eid, mechanism):
        return {"id": eid, "name": f"讲法 {eid}" if zh else f"Telling {eid}",
                "description": "让充电器在路上工作。" if zh else "Show the charger at work.",
                "fit_reason": "出差的人只带一个充电器。" if zh else "Travellers carry one charger.",
                "how_to_tell": "跟着一次出差从办公桌走到登机口。" if zh else "Follow one trip from desk to gate.",
                "visual_mechanism": mechanism, "evidence_basis": {"fact_ids": ["f1"]}}

    def combination(cid, angle_id, expression_id):
        return {"id": cid, "angle_id": angle_id, "expression_id": expression_id,
                "headline": "One charger for the whole trip", "body_copy": "Acme Charge powers laptop and phone together.",
                "visual_brief": "A traveller plugs one charger in at the gate.", "checks": ["Logo is readable"],
                "image_brief": {"proposition": "One charger replaces three", "expression": f"Telling {expression_id}",
                                "hold_constant": ["Same charger"], "checks": ["Logo is readable"]}}

    return {
        "market": "United States", "ad_language": "en-US", "strategy_language": strategy_language,
        "angles": [
            {"id": "a1", "proposition": "一个充电器顶三个" if zh else "One charger replaces three",
             "rationale": "出差的人想少带东西。" if zh else "Travellers pack light.", "fact_ids": ["f1"],
             "expressions": [expression("e1", "scenario_story"), expression("e2", "route_map")]},
            {"id": "a2", "proposition": "笔记本也能满速充" if zh else "Charges a laptop at full speed",
             "rationale": "65W 够笔记本用。" if zh else "65W covers laptops.", "fact_ids": ["f1"], "inference_ids": ["i1"],
             "expressions": [expression("e3", "live_connection_demo"), expression("e4", "qa_objection")]},
        ],
        "combinations": [combination("c1", "a1", "e1"), combination("c2", "a1", "e2"),
                         combination("c3", "a2", "e3"), combination("c4", "a2", "e4")],
        "style_notes": "Clean product photography, soft daylight, neutral background.",
    }


UNDERSTANDING = {
    "product_name": "Acme Charge Travel Charger", "category_need": "Charge several devices on the road.",
    "product_choice_reason": "One 65W charger with three ports.", "audience": "Business travellers",
    "situations": ["Airport gate"], "objections": [],
    "facts": [{"id": "f1", "statement": "65W with three ports.", "source_quote": "65W, three ports",
               "confirmation": "confirmed"}],
    "inferences": [{"id": "i1", "statement": "Travellers want fewer chargers.", "rationale": "A guess.", "fact_ids": ["f1"]}],
    "unknowns": [], "unsupported_claims": [{"id": "u1", "text": "Charges twice as fast as any other charger."}],
}


def ideas_row(strategy_language="en-US"):
    return SimpleNamespace(id="mp_ideas", revision=3, market="United States", state="ideas_ready",
                           ideas_json=charger_ideas(strategy_language), understanding_json=deepcopy(UNDERSTANDING))


def kept(*combination_ids, mode="byok"):
    """One kept ad per combination id; a repeated id is another version of that ad."""
    angles = {"c1": ("a1", "e1"), "c2": ("a1", "e2"), "c3": ("a2", "e3"), "c4": ("a2", "e4")}
    return [KeptAd(asset_version_id=f"av_{n}", combination_id=cid, angle_id=angles[cid][0], expression_id=angles[cid][1],
                   intent_hash=f"{n:064x}", execution_mode=mode) for n, cid in enumerate(combination_ids, 1)]


ANSWERS = MarketingTestAnswers(target_cost=Decimal("10"), round_budget=Decimal("400"), duration_days=7,
                               landing_page="https://acmecharge.example/travel")


def conditions(**changes):
    return ad_test_plans.Conditions(**{"audience": "Business travellers", "offer": None, "placement": None,
                                        "landing_page": "https://acmecharge.example/travel", "currency": "USD",
                                        "max_small_rounds": 3, "media_budget": None, **changes})


class PlanTemplateTests(unittest.TestCase):
    def plan(self, ads, *, language="en-US", answers=ANSWERS, **changes):
        row = ideas_row(language)
        plan = ad_test_plans.build_plan(row, ads, answers, conditions(**changes))
        self.assertIsInstance(plan, MarketingTestPlan)
        # Every check a confirmed plan passes, unchanged: the stored understanding is the plan-first shape.
        ad_test_plans.check_plan(row, plan)
        return plan

    def test_what_the_round_compares_follows_the_kept_ads(self):
        self.assertEqual(ad_test_plans.comparison_of(kept("c1", "c3")), "claims")
        self.assertEqual(ad_test_plans.comparison_of(kept("c1", "c2", "c3")), "claims")
        self.assertEqual(ad_test_plans.comparison_of(kept("c1", "c2")), "expressions")
        self.assertEqual(ad_test_plans.comparison_of(kept("c1")), "single")
        # Several versions of one ad are still one ad checked against the goal.
        self.assertEqual(ad_test_plans.comparison_of(kept("c1", "c1")), "single")

    def test_two_claims_give_a_plan_that_screens_claims_one_group_each(self):
        for language in ("en-US", "zh-CN"):
            with self.subTest(language=language):
                plan = self.plan(kept("c1", "c3"), language=language)
                self.assertEqual((plan.stage, plan.comparison_kind, plan.conclusion_scope),
                                 ("screen_angles", "whole_combinations", "combination"))
                self.assertEqual([g.combination_ids for g in plan.groups], [["c1"], ["c3"]])
                self.assertEqual(ad_tests.comparison(plan.model_dump(mode="json")), "claims")
                # Each claim is tried with one way of telling it only, so the plan says so (user decision, #248).
                self.assertTrue(plan.limited_expression_coverage)
                full = self.plan(kept("c1", "c2", "c3", "c4"), language=language)
                self.assertEqual(len(plan.limitations), len(full.limitations) + 2)
                self.assertTrue(all(a.proposition in " ".join(plan.limitations) for a in plan.angles))
                self.assertEqual(plan.strategy_language, language)
                # Only the kept ways of telling are in the plan; the combinations are the ideas' own, unchanged.
                self.assertEqual([[e.id for e in a.expressions] for a in plan.angles], [["e1"], ["e3"]])
                ideas = {c["id"]: c for c in charger_ideas(language)["combinations"]}
                self.assertEqual([c.model_dump(mode="json", exclude_unset=True) for c in plan.combinations],
                                 [ideas["c1"], ideas["c3"]])

    def test_claims_with_two_ways_each_are_not_limited(self):
        plan = self.plan(kept("c1", "c2", "c3", "c4"))
        self.assertEqual(plan.stage, "screen_angles")
        self.assertFalse(plan.limited_expression_coverage)
        self.assertEqual([g.combination_ids for g in plan.groups], [["c1", "c2"], ["c3", "c4"]])
        # One claim with two ways and one with a single way: the plan as a whole is limited.
        mixed = self.plan(kept("c1", "c2", "c3"))
        self.assertTrue(mixed.limited_expression_coverage)
        self.assertEqual(len(mixed.limitations), len(plan.limitations) + 1)

    def test_one_claims_ways_of_telling_give_a_plan_that_matches_expressions(self):
        for language in ("en-US", "zh-CN"):
            with self.subTest(language=language):
                plan = self.plan(kept("c1", "c2"), language=language)
                self.assertEqual((plan.stage, plan.comparison_kind), ("match_expressions", "whole_combinations"))
                self.assertEqual([g.combination_ids for g in plan.groups], [["c1"], ["c2"]])
                self.assertFalse(plan.limited_expression_coverage)
                self.assertEqual(ad_tests.comparison(plan.model_dump(mode="json")), "expressions")

    def test_one_ad_gives_a_plan_that_checks_it_against_the_goal(self):
        for language in ("en-US", "zh-CN"):
            for ads in (kept("c3"), kept("c3", "c3")):
                with self.subTest(language=language, ads=len(ads)):
                    plan = self.plan(ads, language=language)
                    # User decision (#248): one ad confirms its combination.
                    self.assertEqual((plan.stage, plan.comparison_kind, plan.conclusion_scope),
                                     ("confirm_combination", "business_retest", "combination"))
                    self.assertEqual([g.combination_ids for g in plan.groups], [["c3"]])
                    self.assertEqual(ad_tests.comparison(plan.model_dump(mode="json")), "single")

    def test_the_answers_and_defaults_are_written_into_the_plan(self):
        plan = self.plan(kept("c1", "c3"))
        self.assertEqual((plan.rules.business_metric, plan.rules.threshold, plan.small_round_budget, plan.duration_days),
                         ("cpa", Decimal("10"), Decimal("400"), 7))
        # The whole test: at most three rounds, three times the first round's budget, in USD.
        self.assertEqual((plan.rules.max_small_rounds, plan.media_budget, plan.currency), (3, Decimal("1200"), "USD"))
        self.assertEqual((plan.landing_page, plan.audience, plan.market, plan.ad_language),
                         ("https://acmecharge.example/travel", "Business travellers", "United States", "en-US"))
        self.assertEqual((plan.plan_review, plan.guiding_questions, plan.executions_per_combination), ([], [], 1))
        self.assertEqual((plan.style_variation, plan.style_notes), ("held", charger_ideas()["style_notes"]))
        self.assertIn("Visual style", plan.held_constant)
        changed = self.plan(kept("c1", "c3"), offer="10% off with code TRIP", placement="Instagram Feed only",
                            currency="EUR", max_small_rounds=2, media_budget=Decimal("900"), audience="Frequent flyers")
        self.assertEqual((changed.offer, changed.placement, changed.currency, changed.rules.max_small_rounds,
                          changed.media_budget, changed.audience),
                         ("10% off with code TRIP", "Instagram Feed only", "EUR", 2, Decimal("900"), "Frequent flyers"))

    def test_a_plan_the_checks_refuse_is_refused_not_loosened(self):
        # A whole test smaller than its first round: TestPlan refuses it, and so does the template.
        with self.assertRaises(ValidationError) as caught:
            ad_test_plans.build_plan(ideas_row(), kept("c1", "c3"), ANSWERS, conditions(media_budget=Decimal("100")))
        self.assertEqual(caught.exception.errors()[0]["type"], "small_round_budget_exceeds_media_budget")
        # Two ways of telling with the same picture mechanism: the confirming check refuses the plan.
        row = ideas_row()
        row.ideas_json["angles"][0]["expressions"][1]["visual_mechanism"] = "scenario_story"
        plan = ad_test_plans.build_plan(row, kept("c1", "c2"), ANSWERS, conditions())
        with self.assertRaises(service.MarketingError) as refused:
            ad_test_plans.check_plan(row, plan)
        self.assertEqual(refused.exception.code, "expressions_not_distinct")

    def test_the_test_understanding_drops_what_only_ideas_have(self):
        understanding = ad_test_plans.understanding(ideas_row())
        self.assertNotIn("unsupported_claims", understanding)
        self.assertEqual(understanding["facts"], UNDERSTANDING["facts"])


class EstimateTests(unittest.TestCase):
    protocol = EvaluationProtocol()

    def estimate(self, subjects, budget, target="10"):
        return ad_test_plans.estimate(subjects, Decimal(budget), Decimal(target), self.protocol)

    def test_twenty_orders_each_is_enough_for_a_verdict(self):
        result = self.estimate(2, "400")
        self.assertEqual((result["budget"], result["expected_orders"], result["tier"]), ("200.00", 20, "enough"))
        self.assertEqual((result["needed_budget"], result["suggested_subjects"]), ("400.00", None))

    def test_just_under_twenty_shows_a_direction(self):
        result = self.estimate(2, "399.98")
        self.assertEqual((result["budget"], result["expected_orders"], result["tier"]), ("199.99", 19, "direction"))
        # Comparing one would be enough with this money.
        self.assertEqual(result["suggested_subjects"], 1)

    def test_ten_orders_each_still_shows_a_direction(self):
        result = self.estimate(2, "200")
        self.assertEqual((result["expected_orders"], result["tier"]), (10, "direction"))

    def test_under_ten_orders_gives_a_rough_read_only(self):
        result = self.estimate(2, "199.98")
        self.assertEqual((result["expected_orders"], result["tier"]), (9, "rough"))
        self.assertEqual(result["needed_budget"], "400.00")

    def test_the_suggestion_is_how_many_to_compare_for_enough(self):
        result = self.estimate(5, "450")  # 450 buys 20 orders at 10 for two objects, not five
        self.assertEqual((result["tier"], result["suggested_subjects"]), ("rough", 2))
        self.assertIsNone(self.estimate(3, "150")["suggested_subjects"])  # not even one object gets 20 orders

    def test_a_seven_day_test_reads_its_result_on_the_sixteenth_day(self):
        start = date(2026, 12, 1)
        self.assertEqual(ad_test_plans.schedule(start, 7, self.protocol),
                         (start, date(2026, 12, 7), date(2026, 12, 16)))
        self.assertEqual((ad_test_plans.schedule(start, 7, self.protocol)[2] - start).days + 1, 16)


class QuoteHashTests(unittest.TestCase):
    def digest(self, ads, revision=3, answers=ANSWERS):
        return ad_test_plans.quote_hash(revision, ads, answers, {"start_date": "2026-12-01"}, {"maximum_cost": "0.10"},
                                        EvaluationProtocol(), {"plan": 1})

    def test_the_hash_covers_every_kept_ad_and_what_it_was_made_from(self):
        ads = kept("c1", "c3")
        self.assertEqual(self.digest(ads), self.digest(list(reversed(ads))))  # the order kept does not matter
        changed = [ads[0], KeptAd(**{**ads[1].__dict__, "intent_hash": "f" * 64})]
        self.assertNotEqual(self.digest(ads), self.digest(changed))
        self.assertNotEqual(self.digest(ads), self.digest(ads[:1]))
        self.assertNotEqual(self.digest(ads), self.digest(ads, revision=4))
        self.assertNotEqual(self.digest(ads), self.digest(ads, answers=ANSWERS.model_copy(update={"duration_days": 8})))


class TestQuoteFixtures(IdeasExportFixtures, MarketingTestFixtures):
    def exportable(self, mode="mock"):
        """Acme Charge Home ideas whose six ads are all exportable and none kept yet."""
        project = self.checked_ads(mode)
        self.verify_everything(project)
        return self.read_ideas(project)

    def keep(self, project, *combination_ids, kept=True):
        ads = self.ads(project)
        for combination_id in combination_ids:
            response = self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{ads[combination_id]['asset_version_id']}/kept",
                                       headers=self.headers(), json={"kept": kept})
            self.assertEqual(response.status_code, 200, response.text)

    def quote_of(self, project, expect=200, **params):
        query = {"target_cost": "10", "round_budget": "400", "duration_days": 7, **params}
        response = self.client.get(f"/api/v1/marketing/ideas/{project['id']}/test-quote", params=query)
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            MarketingTestQuote.model_validate(response.json())
        return response.json()



class TestQuoteTests(TestQuoteFixtures):
    def test_kept_ads_of_two_claims_are_quoted_as_a_claims_test(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1", "c2_2")
        quote = self.quote_of(project)
        ads = self.ads(project)
        self.assertEqual((quote["available"], quote["unavailable_reason"], quote["plan_error"]), (True, None, None))
        self.assertEqual((quote["comparison"], quote["execution_mode"], quote["ideas_revision"]),
                         ("claims", "mock", self.read_ideas(project)["revision"]))
        self.assertEqual([(s["kind"], s["angle_id"], s["combination_ids"]) for s in quote["subjects"]],
                         [("claim", "a1", ["c1_1"]), ("claim", "a2", ["c2_1", "c2_2"])])
        self.assertEqual([s["asset_version_ids"] for s in quote["subjects"]],
                         [[ads["c1_1"]["asset_version_id"]], [ads["c2_1"]["asset_version_id"], ads["c2_2"]["asset_version_id"]]])
        # 400 split over two claims, 10 a order: twenty orders each, enough for a verdict.
        self.assertEqual([(s["budget"], s["expected_orders"]) for s in quote["subjects"]], [("200.00", 20), ("200.00", 20)])
        self.assertEqual((quote["tier"], quote["min_purchases"], quote["needed_budget"], quote["suggested_subjects"]),
                         ("enough", 20, "400.00", None))
        self.assertEqual((quote["target_cost"], quote["round_budget"], quote["currency"], quote["duration_days"]),
                         ("10.00", "400.00", "USD", 7))
        # The landing page defaults to the product link the user pasted.
        self.assertEqual((quote["landing_page"], quote["landing_page_source"]),
                         ("https://acmecharge.example/products/home", "product_link"))
        # Controlled Mock ads are checked by the controlled example: one look-together check per ad, free.
        self.assertEqual(quote["checks"], {"available": True, "unavailable_reason": None, "quantity": 3,
                                           "maximum_cost": "0", "currency": "USD"})
        self.assertTrue(quote["question"])
        self.assertRegex(quote["quote_hash"], r"^[0-9a-f]{64}$")

    def test_one_claims_two_ways_and_a_single_ad_are_quoted_too(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c1_2")
        quote = self.quote_of(project)
        self.assertEqual((quote["available"], quote["comparison"]), (True, "expressions"))
        self.assertEqual([(s["kind"], s["expression_id"]) for s in quote["subjects"]],
                         [("expression", "e1_1"), ("expression", "e1_2")])
        self.keep(project, "c1_2", kept=False)
        quote = self.quote_of(project)
        self.assertEqual((quote["available"], quote["comparison"]), (True, "single"))
        self.assertEqual([(s["kind"], s["combination_ids"], s["budget"], s["expected_orders"]) for s in quote["subjects"]],
                         [("ad", ["c1_1"], "400.00", 40)])

    def test_the_three_tiers_and_the_day_of_the_result(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        self.assertEqual(self.quote_of(project, round_budget="399.98")["tier"], "direction")
        self.assertEqual(self.quote_of(project, round_budget="200")["tier"], "direction")
        rough = self.quote_of(project, round_budget="199.98")
        self.assertEqual((rough["tier"], rough["needed_budget"], rough["available"]), ("rough", "400.00", True))
        start = date.today() + timedelta(days=30)
        quote = self.quote_of(project, start_date=start.isoformat(), account_timezone="America/New_York")
        self.assertEqual((quote["start_date"], quote["end_date"], quote["conclusion_date"], quote["attribution_lag_days"]),
                         (start.isoformat(), (start + timedelta(days=6)).isoformat(),
                          (start + timedelta(days=15)).isoformat(), 8))
        self.assertEqual(quote["account_timezone"], "America/New_York")
        # Without a start date the test starts tomorrow in the ad account's time zone.
        self.assertGreater(date.fromisoformat(self.quote_of(project)["start_date"]), date.today() - timedelta(days=1))

    def test_the_hash_changes_with_the_kept_ads_and_stays_for_the_same_quote(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        first = self.quote_of(project)["quote_hash"]
        self.assertEqual(self.quote_of(project)["quote_hash"], first)
        self.keep(project, "c2_2")
        self.assertNotEqual(self.quote_of(project)["quote_hash"], first)
        self.keep(project, "c2_2", kept=False)
        self.assertEqual(self.quote_of(project)["quote_hash"], first)
        self.assertNotEqual(self.quote_of(project, round_budget="500")["quote_hash"], first)
        self.assertNotEqual(self.quote_of(project, placement="Instagram Feed only")["quote_hash"], first)

    def test_ads_that_may_not_be_exported_are_named(self):
        project = self.checked_ads()  # checked by the AI, nothing confirmed by the user: every ad is a draft
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        ads = self.ads(project)
        self.assertEqual((quote["available"], quote["unavailable_reason"]), (False, "ads_not_exportable"))
        self.assertEqual(sorted(item["asset_version_id"] for item in quote["not_exportable"]),
                         sorted(ads[cid]["asset_version_id"] for cid in ("c1_1", "c2_1")))
        self.assertTrue(all(item["status"] == "draft" and item["reasons"] for item in quote["not_exportable"]))

    def test_a_kept_ad_of_a_claim_taken_out_since_is_named_not_compared(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c3_1")
        # The ideas without the third claim, as after taking it out (the first batch is still open here).
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            ideas_json = deepcopy(row.ideas_json)
            ideas_json["angles"] = [a for a in ideas_json["angles"] if a["id"] != "a3"]
            ideas_json["combinations"] = [c for c in ideas_json["combinations"] if c["angle_id"] != "a3"]
            row.ideas_json = ideas_json
        quote = self.quote_of(project)
        self.assertEqual((quote["available"], quote["unavailable_reason"], quote["comparison"], quote["subjects"]),
                         (False, "ads_not_exportable", "claims", []))
        self.assertEqual([(item["asset_version_id"], item["status"]) for item in quote["not_exportable"]],
                         [(self.ads(project)["c3_1"]["asset_version_id"], "needs_redo")])

    def test_without_a_product_link_the_landing_page_is_asked_for(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        # The product was read from a page; one read from pasted text or pictures only has no link.
        with patch.object(ad_tests, "product_link", return_value=None):
            quote = self.quote_of(project)
            answered = self.quote_of(project, landing_page="https://acmecharge.example/go")
        self.assertEqual((quote["available"], quote["unavailable_reason"], quote["landing_page"]),
                         (False, "landing_page_required", None))
        self.assertEqual((answered["available"], answered["landing_page"], answered["landing_page_source"]),
                         (True, "https://acmecharge.example/go", "answer"))
        self.assertNotEqual(answered["quote_hash"], quote["quote_hash"])

    def test_nothing_kept_is_not_a_test(self):
        project = self.exportable()
        quote = self.quote_of(project)
        self.assertEqual((quote["available"], quote["unavailable_reason"], quote["comparison"], quote["subjects"]),
                         (False, "no_kept_ads", None, []))

    def test_answers_outside_the_rules_are_refused(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        for params in ({"duration_days": 6}, {"target_cost": "0"}, {"round_budget": "-1"},
                       {"account_timezone": "Mars/Base"}, {"landing_page": "acmecharge.example"},
                       {"currency": "BTC"}, {"something": "else"}):
            with self.subTest(params=params):
                self.quote_of(project, expect=422, **params)
        # A whole test smaller than its first round is a plan the checks refuse: the quote says why.
        quote = self.quote_of(project, media_budget="100")
        self.assertEqual((quote["available"], quote["unavailable_reason"], quote["plan_error"]),
                         (False, "plan_invalid", "small_round_budget_exceeds_media_budget"))

    def test_quoting_writes_nothing(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        with tenant_session(self.context) as session:
            before = (session.get(MarketingProjectRecord, project["id"]).revision,
                      session.scalar(select(func.count()).select_from(MarketingRevisionRecord)),
                      session.scalar(select(func.count()).select_from(MarketingProjectRecord)))
        self.quote_of(project)
        with tenant_session(self.context) as session:
            after = (session.get(MarketingProjectRecord, project["id"]).revision,
                     session.scalar(select(func.count()).select_from(MarketingRevisionRecord)),
                     session.scalar(select(func.count()).select_from(MarketingProjectRecord)))
            self.assertEqual(after, before)
            # This Workspace's test projects: other test modules sharing the database make their own elsewhere.
            self.assertEqual(session.scalars(select(MarketingProjectRecord).where(
                MarketingProjectRecord.workspace_id == self.context.workspace_id,
                MarketingProjectRecord.flow == ad_tests.FLOW)).all(), [])


class TestQuoteAccessTests(TestQuoteFixtures):
    def test_another_workspace_and_other_flows_are_not_found(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        plan_first, _, _ = self.confirmed_plan()
        test_id, _ = self.make_test(source=project["id"], freeze=False)
        for project_id in (plan_first, test_id, "mp_missing"):
            response = self.client.get(f"/api/v1/marketing/ideas/{project_id}/test-quote",
                                       params={"target_cost": "10", "round_budget": "400", "duration_days": 7})
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), project_id)
        other = self._make_context()
        self.use(other)
        theirs = self.client.get(f"/api/v1/marketing/ideas/{project['id']}/test-quote",
                                 params={"target_cost": "10", "round_budget": "400", "duration_days": 7})
        missing = self.client.get(f"/api/v1/marketing/ideas/{new_id('mp')}/test-quote",
                                  params={"target_cost": "10", "round_budget": "400", "duration_days": 7})
        self.assertEqual((theirs.status_code, theirs.json()["code"], theirs.json()["detail"]),
                         (missing.status_code, missing.json()["code"], missing.json()["detail"]))
        self.assertEqual(theirs.status_code, 404)
        self.use(self.context)
        self.assertTrue(self.quote_of(project)["available"])

    def test_a_suspended_workspace_is_refused(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        self.assertEqual(self.quote_of(project, expect=403)["code"], "workspace_inactive")
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.assertTrue(self.quote_of(project)["available"])


class PaidCheckQuoteTests(TestQuoteFixtures):
    def test_the_button_maximum_is_one_look_together_check_per_ad(self):
        project = self.exportable("byok")
        self.keep(project, "c1_1", "c2_1", "c3_1")
        quote = self.quote_of(project)
        self.assertEqual((quote["available"], quote["execution_mode"]), (True, "byok"))
        # QC_ENV prices one check at 0.05 USD.
        self.assertEqual(quote["checks"], {"available": True, "unavailable_reason": None, "quantity": 3,
                                           "maximum_cost": "0.15", "currency": "USD"})
        with self.no_key():
            refused = self.quote_of(project)
        self.assertEqual((refused["available"], refused["unavailable_reason"], refused["checks"]["available"]),
                         (False, "checks_unavailable", False))
        self.assertTrue(refused["checks"]["unavailable_reason"])
        self.assertNotEqual(refused["quote_hash"], quote["quote_hash"])


if __name__ == "__main__":
    unittest.main()
