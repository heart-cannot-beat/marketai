"""Ads-first drafting (ADR 0018, #190): product understanding, then buying claims, with every fact unchecked."""
import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, new_id
from app.competitors import relevance
from app.entitlements.cost_models import SpendReservationRecord
from app.marketing import ideas, service
from app.marketing.ideas_contracts import Ideas
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord, PlanningCallRecord
from app.marketing.planner import completion_messages, example_result, execute_planning, system_prompt
from app.marketing.planning_schema import openai_format
from tests.test_marketing_ideas import MarketingIdeasFixtures

SOURCE = ("Acme Charge Home\nSchedule charging from the Acme Charge app.\n"
          "IP55 enclosure for outdoor installation.\nLoad balancing keeps the home circuit from tripping.")
PLANNING_ENV = {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test",
                "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}


class IdeasPlanningFixtures(MarketingIdeasFixtures):
    def ideas_project(self, ctx=None):
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": self.product(ctx), "source_text": SOURCE, "market": "United States", "ad_language": "en-US"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def read_ideas(self, project):
        response = self.client.get(f"/api/v1/marketing/ideas/{project['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ideas_call(self, project, kind, mode="mock", quote_hash="", key=None, **extra):
        return self.client.post(f"/api/v1/marketing/ideas/{project['id']}/planning",
            headers=self.headers(project["revision"], key),
            json={"kind": kind, "execution_mode": mode, "quote_hash": quote_hash, **extra})

    def drafted_call(self, project, kind, **kwargs):
        response = self.ideas_call(project, kind, **kwargs)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def understood_ideas(self):
        """An ads-first project with a drafted, entirely unchecked understanding."""
        project = self.ideas_project()
        self.execute(self.drafted_call(project, "understanding"))
        return self.read_ideas(project)

    def with_ideas(self):
        project = self.understood_ideas()
        self.execute(self.drafted_call(project, "ideas"))
        return self.read_ideas(project)

    def example_ideas(self, project):
        return example_result("ideas", {"understanding": project["understanding"], "market": "United States",
                                        "ad_language": "en-US", "strategy_language": "en-US"})

    def call_record(self, call_id):
        with tenant_session(self.context) as session:
            call = session.get(PlanningCallRecord, call_id)
            session.expunge(call)
            return call

    def project_record(self, project_id):
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project_id)
            session.expunge(row)
            return row


class IdeasDraftingTests(IdeasPlanningFixtures):
    def test_claims_are_drafted_while_every_fact_is_unchecked(self):
        project = self.understood_ideas()
        self.assertEqual(project["state"], "understanding_draft")
        self.assertTrue(project["understanding"]["facts"])
        self.assertTrue(all(f["confirmation"] == "unconfirmed" for f in project["understanding"]["facts"]))
        call = self.drafted_call(project, "ideas")
        self.assertEqual((call["kind"], call["state"], call["execution_mode"]), ("ideas", "queued", "mock"))
        self.execute(call)
        current = self.read_ideas(project)
        self.assertEqual(current["state"], "ideas_ready")
        self.assertEqual((current["latest_call"]["kind"], current["latest_call"]["state"]), ("ideas", "succeeded"))
        drafted = Ideas.model_validate(current["ideas"])
        self.assertEqual(len(drafted.angles), 3)
        self.assertEqual([len(a.expressions) for a in drafted.angles], [2, 2, 2])
        self.assertEqual(len(drafted.combinations), 6)
        self.assertEqual((drafted.market, drafted.ad_language), ("United States", "en-US"))
        # Facts stay unchecked: drafting claims never confirms anything.
        self.assertEqual(current["understanding"], project["understanding"])
        self.assertIsNone(self.project_record(project["id"]).plan_json)

    def test_each_step_is_a_revision_and_audited(self):
        project = self.with_ideas()
        with tenant_session(self.context) as session:
            revisions = session.scalars(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"]).order_by(MarketingRevisionRecord.revision)).all()
            self.assertEqual([r.reason for r in revisions],
                             ["created", "planning_requested", "ai_drafted", "planning_requested", "ai_drafted"])
            self.assertEqual(revisions[-1].content_json["ideas"], session.get(MarketingProjectRecord, project["id"]).ideas_json)
            self.assertEqual(Ideas.model_validate(revisions[-1].content_json["ideas"]), Ideas.model_validate(project["ideas"]))
            self.assertEqual(revisions[-1].content_json["state"], "ideas_ready")
            events = session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.resource_id == project["id"])).all()
            self.assertEqual(events.count("marketing.ai_drafted"), 2)

    def test_drafting_claims_again_from_ideas_ready_replaces_them(self):
        project = self.with_ideas()
        self.execute(self.drafted_call(project, "ideas"))
        current = self.read_ideas(project)
        self.assertEqual((current["state"], current["revision"]), ("ideas_ready", project["revision"] + 2))

    def test_drafting_the_understanding_again_drops_the_claims(self):
        project = self.with_ideas()
        self.execute(self.drafted_call(project, "understanding"))
        current = self.read_ideas(project)
        self.assertEqual(current["state"], "understanding_draft")
        self.assertIsNone(current["ideas"])

    def test_claims_need_a_drafted_understanding(self):
        project = self.ideas_project()
        response = self.ideas_call(project, "ideas")
        self.assertEqual((response.status_code, response.json()["code"]), (422, "understanding_required"))
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalars(select(PlanningCallRecord).where(
                PlanningCallRecord.project_id == project["id"])).all(), [])
        self.assertEqual(self.read_ideas(project)["revision"], project["revision"])

    def test_a_pending_call_blocks_another(self):
        project = self.understood_ideas()
        self.drafted_call(project, "ideas")
        again = self.ideas_call(self.read_ideas(project), "ideas")
        self.assertEqual((again.status_code, again.json()["code"]), (409, "planning_in_progress"))

    def test_the_same_key_returns_the_same_call(self):
        project = self.understood_ideas()
        key = new_id("key")
        first = self.drafted_call(project, "ideas", key=key)
        replay = self.drafted_call(project, "ideas", key=key)
        self.assertEqual(first, replay)

    def test_the_revision_is_required_and_checked(self):
        project = self.understood_ideas()
        missing = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/planning",
            headers={"Idempotency-Key": uuid4().hex}, json={"kind": "ideas", "execution_mode": "mock"})
        self.assertEqual(missing.status_code, 428, missing.text)
        stale = self.ideas_call({**project, "revision": project["revision"] - 1}, "ideas")
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "stale_marketing_revision"))

    def test_the_planner_is_asked_for_three_claims_with_two_ways_each(self):
        project = self.understood_ideas()
        call = self.drafted_call(project, "ideas")
        inputs = self.call_record(call["id"]).input_json
        self.assertEqual((inputs["market"], inputs["ad_language"], inputs["strategy_language"]),
                         ("United States", "en-US", "en-US"))
        self.assertEqual(inputs["understanding"], project["understanding"])
        self.assertIn("expression_catalog", inputs)
        prompt = system_prompt("ideas", inputs)
        self.assertIn("exactly 3 angles", prompt)
        self.assertIn("exactly 2 expressions", prompt)
        self.assertIn("exactly 1 combination", prompt)
        schema = json.loads(prompt.rpartition("Schema:\n")[2])
        self.assertEqual(set(schema["properties"]),
                         {"market", "ad_language", "strategy_language", "angles", "combinations", "style_notes"})
        self.assertNotIn("competitor_overlap", schema["$defs"]["Angle"]["properties"])
        self.assertNotIn("executions", schema["$defs"]["CreativeCombination"]["properties"])
        # The OpenAI wire format accepts the ideas schema.
        _, response_format = openai_format(prompt)
        self.assertEqual(response_format["json_schema"]["schema"]["required"],
                         ["market", "ad_language", "strategy_language", "angles", "combinations", "style_notes"])


class IdeasRejectionTests(IdeasPlanningFixtures):
    def rejected(self, project, draft):
        call = self.drafted_call(project, "ideas")
        with patch("app.marketing.planner.example_result", return_value=draft):
            self.execute(call)
        current = self.read_ideas(project)
        self.assertEqual((current["latest_call"]["state"], current["latest_call"]["error_code"]),
                         ("failed", "planner_output_invalid"))
        self.assertEqual((current["state"], current["ideas"]), ("understanding_draft", None))
        self.assertEqual(current["revision"], project["revision"] + 1)  # only the request was recorded
        return self.call_record(call["id"])

    def test_two_ways_with_the_same_visual_mechanism_are_rejected(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        first, second = draft["angles"][0]["expressions"]
        second["visual_mechanism"] = first["visual_mechanism"]
        call = self.rejected(project, draft)
        self.assertEqual(call.output_json["validation_errors"][0]["type"], "expressions_not_distinct")
        self.assertEqual(call.output_json["rejected_output"]["angles"][0]["expressions"][1]["visual_mechanism"],
                         first["visual_mechanism"])

    def test_the_same_mechanism_under_different_claims_is_allowed(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["angles"][1]["expressions"][0]["visual_mechanism"] = draft["angles"][0]["expressions"][0]["visual_mechanism"]
        call = self.drafted_call(project, "ideas")
        with patch("app.marketing.planner.example_result", return_value=draft):
            self.execute(call)
        self.assertEqual(self.read_ideas(project)["state"], "ideas_ready")

    def test_a_way_of_telling_without_one_of_its_three_elements_is_rejected(self):
        for element in ("how_to_tell", "visual_mechanism", "evidence_basis"):
            with self.subTest(element=element):
                project = self.understood_ideas()
                draft = self.example_ideas(project)
                draft["angles"][2]["expressions"][1].pop(element)
                call = self.rejected(project, draft)
                self.assertIn(element, [e["loc"][-1] for e in call.output_json["validation_errors"]])

    def test_more_than_the_first_batch_is_rejected(self):
        project = self.understood_ideas()
        too_many = self.example_ideas(project)
        angle = deepcopy(too_many["angles"][0])
        angle["id"] = "a4"
        too_many["angles"].append(angle)
        for n, expression in enumerate(angle["expressions"], 1):
            too_many["combinations"].append({**deepcopy(too_many["combinations"][0]), "id": f"c4_{n}", "angle_id": "a4",
                                             "expression_id": expression["id"]})
        self.assertEqual(self.rejected(project, too_many).output_json["validation_errors"][0]["type"],
                         "ideas_first_batch_too_large")
        project = self.read_ideas(project)
        twice = self.example_ideas(project)
        twice["combinations"].append({**deepcopy(twice["combinations"][0]), "id": "c_extra"})
        self.assertEqual(self.rejected(project, twice).output_json["validation_errors"][0]["type"],
                         "ideas_first_batch_too_large")

    def test_a_claim_citing_an_unknown_fact_is_rejected(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["angles"][0]["expressions"][0]["evidence_basis"]["fact_ids"] = ["f99"]
        self.assertEqual(self.rejected(project, draft).output_json["validation_errors"][0]["type"], "plan_evidence_invalid")

    def test_another_ad_language_is_rejected(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["ad_language"] = "de-DE"
        self.assertEqual(self.rejected(project, draft).output_json["validation_errors"][0]["type"],
                         "planning_language_mismatch")

    def test_copy_outside_the_ad_language_is_rejected(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["combinations"][0]["headline"] = "整晚自动充电"
        self.assertEqual(self.rejected(project, draft).output_json["validation_errors"][0]["type"], "copy_language_mismatch")

    def test_a_combination_without_an_english_image_brief_is_rejected(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["combinations"][0].pop("image_brief")
        self.assertEqual(self.rejected(project, draft).output_json["validation_errors"][0]["type"],
                         "image_brief_language_mismatch")

    def test_the_market_is_the_projects_own(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["market"] = "Northpeak territory"
        call = self.drafted_call(project, "ideas")
        with patch("app.marketing.planner.example_result", return_value=draft):
            self.execute(call)
        self.assertEqual(self.read_ideas(project)["ideas"]["market"], "United States")

    def test_a_retry_carries_the_rejected_draft_and_its_errors(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["angles"][0]["expressions"][1]["visual_mechanism"] = draft["angles"][0]["expressions"][0]["visual_mechanism"]
        first = self.rejected(project, draft)
        retry = self.drafted_call(self.read_ideas(project), "ideas")
        feedback = self.call_record(retry["id"]).input_json["rejected_plan_feedback"]
        self.assertEqual(feedback["policy"], "marketing-explicit-ideas-repair-v1")
        self.assertEqual(feedback["previous_call_id"], first.id)
        self.assertEqual(feedback["validation_errors"][0]["type"], "expressions_not_distinct")
        self.assertEqual(feedback["rejected_plan"]["angles"], draft["angles"])
        # The correction turn is the claims' own, not the test plan's.
        messages = completion_messages("system", json.dumps(self.call_record(retry["id"]).input_json))
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertNotIn("rejected_plan_feedback", messages[1]["content"])
        self.assertNotIn("测试组", messages[-1]["content"])
        self.assertIn("visual_mechanism", messages[-1]["content"])
        self.execute(retry)
        self.assertEqual(self.read_ideas(project)["state"], "ideas_ready")

    def test_a_new_understanding_drops_the_old_rejection(self):
        project = self.understood_ideas()
        draft = self.example_ideas(project)
        draft["angles"][0]["expressions"][1]["visual_mechanism"] = draft["angles"][0]["expressions"][0]["visual_mechanism"]
        self.rejected(project, draft)
        project = self.read_ideas(project)
        self.execute(self.drafted_call(project, "understanding"))
        retry = self.drafted_call(self.read_ideas(project), "ideas")
        self.assertNotIn("rejected_plan_feedback", self.call_record(retry["id"]).input_json)


class IdeasPaidDraftingTests(IdeasPlanningFixtures):
    def paid_call(self, project, kind):
        with patch.dict("os.environ", PLANNING_ENV):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            self.assertTrue(cap["available"], cap)
            return self.drafted_call(project, kind, mode="byok", quote_hash=cap["quote_hash"])

    def test_the_worker_writes_paid_results_back_to_an_ads_first_project(self):
        """#206 left the Worker looking up plan-first projects only; ads-first results must land."""
        self.paid_setup()
        project = self.ideas_project()
        understanding = example_result("understanding", {"source_text": SOURCE})
        complete = AsyncMock(return_value=json.dumps(understanding))
        call = self.paid_call(project, "understanding")
        self.execute(call, complete)
        complete.assert_awaited_once()
        project = self.read_ideas(project)
        self.assertEqual((project["state"], project["latest_call"]["state"]), ("understanding_draft", "succeeded"))
        self.assertEqual(project["understanding"]["facts"][0]["confirmation"], "unconfirmed")

        drafted = self.example_ideas(project)
        complete = AsyncMock(return_value=json.dumps(drafted))
        call = self.paid_call(project, "ideas")
        self.execute(call, complete)
        complete.assert_awaited_once()
        provider, model, _, system, user = complete.await_args.args
        self.assertEqual((provider, model), ("openai", "text-test"))
        self.assertTrue(system.rpartition("Schema:\n")[2].startswith("{"))
        self.assertEqual(json.loads(user)["understanding"], project["understanding"])
        current = self.read_ideas(project)
        self.assertEqual((current["state"], current["latest_call"]["execution_mode"]), ("ideas_ready", "byok"))
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(
                SpendReservationRecord.job_id == self.call_record(call["id"]).job_id))
            self.assertEqual(reservation.state, "settled")

    def test_paid_ai_cannot_check_facts_for_the_user(self):
        self.paid_setup()
        project = self.ideas_project()
        understanding = example_result("understanding", {"source_text": SOURCE})
        understanding["facts"][0]["confirmation"] = "confirmed"
        self.execute(self.paid_call(project, "understanding"), AsyncMock(return_value=json.dumps(understanding)))
        current = self.read_ideas(project)
        self.assertEqual((current["state"], current["understanding"]), ("input", None))
        self.assertEqual(current["latest_call"]["error_code"], "planner_output_invalid")

    def test_a_quote_from_another_configuration_is_refused(self):
        self.paid_setup()
        project = self.ideas_project()
        with patch.dict("os.environ", PLANNING_ENV):
            response = self.ideas_call(project, "understanding", mode="byok", quote_hash="0" * 64)
        self.assertEqual((response.status_code, response.json()["code"]), (422, "planner_unavailable"))


class IdeasBoundaryTests(IdeasPlanningFixtures):
    def test_a_plan_first_project_cannot_use_the_ideas_planning(self):
        old = self.create()
        response = self.ideas_call(old, "understanding")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalars(select(PlanningCallRecord).where(
                PlanningCallRecord.project_id == old["id"])).all(), [])

    def test_an_ads_first_project_cannot_use_the_old_planning(self):
        project = self.understood_ideas()
        for kind, extra in (("understanding", {}), ("plan", {"ad_language": "en-US", "strategy_language": "en-US"})):
            response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
                headers=self.headers(project["revision"]), json={"kind": kind, "execution_mode": "mock", **extra})
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"))
        self.assertEqual(self.read_ideas(project)["revision"], project["revision"])

    def test_the_plan_kind_is_not_accepted_here(self):
        project = self.understood_ideas()
        self.assertEqual(self.ideas_call(project, "plan").status_code, 422)

    def test_another_workspace_can_neither_start_nor_run_nor_read_it(self):
        project = self.understood_ideas()
        call = self.drafted_call(project, "ideas")
        mine = self.context
        self.context = self._make_context()
        try:
            refused = self.ideas_call(project, "ideas")
            self.assertEqual((refused.status_code, refused.json()["code"]), (404, "marketing_project_not_found"))
            self.assertEqual(self.client.get(f"/api/v1/marketing/ideas/{project['id']}").status_code, 404)
            # A Worker holding the other workspace's context cannot claim or apply this call.
            asyncio.run(execute_planning(self.context, call["id"]))
        finally:
            self.context = mine
        record = self.call_record(call["id"])
        self.assertEqual((record.state, record.output_json), ("queued", None))
        with tenant_session(self.context) as session:
            self.assertEqual(len(session.scalars(select(PlanningCallRecord).where(
                PlanningCallRecord.project_id == project["id"])).all()), 2)
        self.assertEqual(self.read_ideas(project)["state"], "understanding_draft")

    def test_the_workers_project_lookup_stays_in_its_workspace(self):
        project = self.understood_ideas()
        other = self._make_context()
        with tenant_session(other) as session:
            with self.assertRaises(service.MarketingError) as caught:
                service.call_project(session, other, project["id"])
            self.assertEqual(caught.exception.status, 404)
        with tenant_session(self.context) as session:
            self.assertEqual(service.call_project(session, self.context, project["id"]).flow, "ads_first")


class CompetitorContextTests(IdeasPlanningFixtures):
    def test_competitor_scans_read_an_unchecked_ads_first_understanding(self):
        """Maintainer decision on #190: a relevance hint only (ADR 0014, ADR 0018), so it reads unchecked facts."""
        project = self.understood_ideas()
        self.assertTrue(all(f["confirmation"] == "unconfirmed" for f in project["understanding"]["facts"]))
        with tenant_session(self.context) as session:
            context = relevance.product_context(session, self.context, project["product_id"])
        understanding = project["understanding"]
        self.assertEqual((context["source"], context["project_id"]), ("product_understanding", project["id"]))
        self.assertEqual((context["product_name"], context["category_need"], context["situations"]),
                         (understanding["product_name"], understanding["category_need"], understanding["situations"]))


class PlanFirstUnchangedTests(IdeasPlanningFixtures):
    def test_plan_first_understanding_keeps_its_fields_and_revision_shape(self):
        project = self.create()
        self.execute(self.plan_call(project))
        current = self.read(project)
        self.assertEqual(current["state"], "understanding_draft")
        self.assertEqual(current["latest_call"]["kind"], "understanding")
        with tenant_session(self.context) as session:
            revision = session.scalar(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"]).order_by(MarketingRevisionRecord.revision.desc()))
            self.assertEqual(set(revision.content_json), {"state", "source_text", "product_id", "understanding", "plan"})
            row = session.get(MarketingProjectRecord, project["id"])
            self.assertEqual((row.flow, row.ideas_json), ("plan_first", None))

    def test_the_plan_repair_prompt_is_unchanged(self):
        feedback = {"policy": "marketing-explicit-plan-repair-v1", "previous_call_id": "pc_1",
                    "validation_errors": [{"type": "x", "loc": [], "msg": "x"}], "rejected_plan": {"groups": []}}
        messages = completion_messages("system", json.dumps({"rejected_plan_feedback": feedback}))
        self.assertIn("测试组是不同的待比较方案", messages[-1]["content"])
