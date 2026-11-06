"""Paid drafts ask for missing key conditions and the plan check lists declared gaps (#239, after #222)."""
import json
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.adapters.persistence.database import tenant_session
from app.marketing.contracts import TestPlan as MarketingTestPlan
from app.marketing.models import PlanningCallRecord
from app.marketing.plan_review import with_system_checks
from app.marketing.planner import example_result, system_prompt
from tests.test_marketing_planning import MarketingPlanningFixtures

PAID = {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}


def generic_questions():
    """The 2026-10-24 live draft asked these instead of the missing Offer and landing page."""
    return [{"id": f"q{n}", "question": question, "options": ["Option A", "Option B"], "multiple": False,
             "why_needed": "Shapes the campaign.", "selected": [], "custom_answer": ""}
            for n, question in enumerate(("What is the campaign objective?", "Who should we target?",
                                          "Which creative format should we use?"), 1)]


class KeyConditionFixtures(MarketingPlanningFixtures):
    source = "Alpha 充电器\n65W，多口，面向差旅办公用户。"

    def create(self):
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true",
            headers=self.headers(), json={"source_text": self.source})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def live_output(self, project, **fields):
        output = example_result("plan", {"understanding": project["understanding"], "ad_language": "en-US"})
        for angle in output["angles"]:
            for expression in angle["expressions"]:
                expression["evidence_basis"]["gap"] = "No documented test or certificate image supplied."
        output.update({"landing_page": "Official Alpha product page with charger compatibility details",
                       "offer": "None specified; standard pricing assumed", "plan_review": [],
                       "guiding_questions": generic_questions(), **fields})
        return output

    def paid_draft(self, project, output):
        with patch.dict("os.environ", PAID):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            call = self.plan_call(project, "plan", "byok", cap["quote_hash"])
        complete = AsyncMock(return_value=json.dumps(output))
        self.execute(call, complete)
        drafted = self.read(project)
        self.assertEqual(drafted["latest_call"]["state"], "succeeded", drafted["latest_call"])
        return drafted, call, complete


class TestMissingKeyConditions(KeyConditionFixtures):
    def setUp(self):
        super().setUp()
        self.paid_setup()

    def test_paid_draft_without_landing_page_or_offer_asks_instead_of_inventing(self):
        project = self.understood()
        drafted, _, complete = self.paid_draft(project, self.live_output(project))
        prompt = complete.await_args.args[3]
        self.assertIn("Landing page pending confirmation", prompt)
        self.assertIn("Offer pending confirmation", prompt)
        plan = drafted["plan"]
        self.assertEqual((plan["landing_page"], plan["offer"]),
                         ("Landing page pending confirmation", "Offer pending confirmation"))
        # Server questions come first; the AI keeps the remaining slot within the limit of three.
        self.assertEqual([q["id"] for q in plan["guiding_questions"]], ["system-landing-page", "system-offer", "q1"])
        self.assertTrue(all(not q["selected"] and not q["custom_answer"] for q in plan["guiding_questions"]))
        system = [r for r in plan["plan_review"] if r["source"] == "system"]
        self.assertEqual({r["kind"] for r in system}, {"evidence_gap", "budget_insufficient"})
        self.assertTrue(all(r["severity"] == "advisory" for r in system))
        gaps = {t for r in system if r["kind"] == "evidence_gap" for t in r["target_ids"]}
        self.assertTrue({e["id"] for a in plan["angles"] for e in a["expressions"]} <= gaps)

        blocked = self.edit(drafted, "confirm_plan", plan=plan)
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["code"], "plan_review_unresolved")
        self.assertEqual({e["field"] for e in blocked.json()["field_errors"]},
                         {f"body.plan.guiding_questions.{n}" for n in range(3)})
        answered = deepcopy(plan)
        answered["guiding_questions"][0]["custom_answer"] = "https://shop.example.com/alpha"
        answered["guiding_questions"][1]["selected"] = ["Regular price, no promotion"]
        answered["guiding_questions"][2]["selected"] = ["Option A"]
        answered["landing_page"] = "https://shop.example.com/alpha"
        confirmed = self.edit(drafted, "confirm_plan", plan=answered)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_invented_url_is_not_a_landing_page(self):
        project = self.understood()
        drafted, _, _ = self.paid_draft(project, self.live_output(project, landing_page="https://alpha.example.com/charger",
                                                                   guiding_questions=[]))
        self.assertEqual(drafted["plan"]["landing_page"], "Landing page pending confirmation")
        self.assertEqual([q["id"] for q in drafted["plan"]["guiding_questions"]], ["system-landing-page", "system-offer"])

    def test_redraft_keeps_answered_conditions_and_does_not_ask_again(self):
        project = self.understood()
        drafted, _, _ = self.paid_draft(project, self.live_output(project))
        plan = deepcopy(drafted["plan"])
        plan["guiding_questions"][0]["custom_answer"] = "https://shop.example.com/alpha"
        plan["guiding_questions"][1]["selected"] = ["A discount or coupon"]
        plan["guiding_questions"][1]["custom_answer"] = "10% launch coupon"
        saved = self.edit(drafted, "save_plan", plan=plan)
        self.assertEqual(saved.status_code, 200, saved.text)
        redrafted, call, _ = self.paid_draft(saved.json(), self.live_output(
            project, landing_page="https://shop.example.com/alpha/", offer="10% launch coupon", guiding_questions=[]))
        with tenant_session(self.context) as session:
            answers = session.get(PlanningCallRecord, call["id"]).input_json["guiding_answers"]
        self.assertEqual([a["id"] for a in answers], ["system-landing-page", "system-offer"])
        self.assertEqual((redrafted["plan"]["landing_page"], redrafted["plan"]["offer"]),
                         ("https://shop.example.com/alpha/", "10% launch coupon"))
        self.assertEqual(redrafted["plan"]["guiding_questions"], [])

    def test_ai_questions_cannot_take_the_reserved_ids(self):
        project = self.understood()
        forged = [{**generic_questions()[0], "id": "system-offer", "selected": ["Option A"]}]
        drafted, _, _ = self.paid_draft(project, self.live_output(project, guiding_questions=forged))
        questions = drafted["plan"]["guiding_questions"]
        self.assertEqual([q["id"] for q in questions], ["system-landing-page", "system-offer"])
        self.assertIn("Offer", questions[1]["question"])
        self.assertEqual(questions[1]["selected"], [])

    def test_controlled_mock_keeps_its_fixed_example(self):
        plan = self.drafted()["plan"]
        self.assertEqual(plan["guiding_questions"], [])
        self.assertEqual(plan["landing_page"], "Landing page pending confirmation")


class TestSuppliedKeyConditions(KeyConditionFixtures):
    source = "Alpha 充电器\n65W，多口。Price: $49 with free shipping. Buy at https://shop.example.com/alpha"

    def test_conditions_the_source_supplies_are_kept_without_questions(self):
        self.paid_setup()
        project = self.understood()
        drafted, _, _ = self.paid_draft(project, self.live_output(
            project, landing_page="Our store: https://shop.example.com/alpha", offer="$49, free shipping", guiding_questions=[]))
        plan = drafted["plan"]
        self.assertEqual((plan["landing_page"], plan["offer"]), ("Our store: https://shop.example.com/alpha", "$49, free shipping"))
        self.assertEqual(plan["guiding_questions"], [])


class TestDeclaredGapsAndBudget(KeyConditionFixtures):
    def test_declared_gaps_and_thin_budget_are_listed_on_every_save(self):
        project = self.drafted()
        plan = project["plan"]
        system = [r for r in plan["plan_review"] if r["source"] == "system"]
        # The mock declares gaps only under its second angle and splits 100 USD across two groups.
        self.assertEqual([(r["id"], r["kind"]) for r in system],
                         [("system-evidence-gap-1", "evidence_gap"), ("system-budget-insufficient", "budget_insufficient")])
        angle = plan["angles"][1]
        self.assertEqual(system[0]["target_ids"], [angle["id"], *(e["id"] for e in angle["expressions"])])
        self.assertIn("Confirm documented answers", system[0]["finding"])
        self.assertIn("50 USD", system[1]["finding"])
        self.assertIn("600 USD", system[1]["finding"])
        self.assertEqual(system[1]["target_ids"], [g["id"] for g in plan["groups"]])

        fixed = deepcopy(plan)
        for expression in fixed["angles"][1]["expressions"]:
            expression["evidence_basis"] = {"fact_ids": expression["evidence_basis"]["fact_ids"], "gap": ""}
        fixed.update(media_budget="1200", small_round_budget="1200")
        saved = self.edit(project, "save_plan", plan=fixed)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertFalse([r for r in saved.json()["plan"]["plan_review"] if r["source"] == "system"])

    def test_budget_floor_follows_the_metric(self):
        plan = MarketingTestPlan.model_validate(example_result("plan", {"understanding": {
            "audience": "Commuters", "facts": [{"id": "f1"}]}, "strategy_language": "zh-CN", "ad_language": "zh-CN"}))
        kinds = lambda p: [r.kind for r in with_system_checks(p).plan_review if r.source == "system"]
        self.assertIn("budget_insufficient", kinds(plan))
        finding = next(r.finding for r in with_system_checks(plan).plan_review if r.kind == "budget_insufficient" and r.source == "system")
        self.assertIn("每组约 50 USD", finding)
        ctr = plan.model_copy(update={"rules": plan.rules.model_copy(update={"business_metric": "ctr", "threshold": Decimal("0.01")})})
        self.assertNotIn("budget_insufficient", kinds(ctr))
        tiny = ctr.model_copy(update={"small_round_budget": Decimal("30")})
        self.assertIn("budget_insufficient", kinds(tiny))

    def test_prompt_states_key_condition_and_self_check_rules(self):
        for language, pending in (("en-US", "Offer pending confirmation"), ("zh-CN", "待确认销售方案")):
            prompt = system_prompt("plan", {"strategy_language": language})
            for rule in (pending, "landing_page", "source=system", "guiding_questions", "budget_insufficient"):
                self.assertIn(rule, prompt)
