"""Expression distinctness and the plan check before paid generation (J-04, #222)."""
from copy import deepcopy
from unittest.mock import patch

from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.marketing.expression_catalog import CATALOG_VERSION, mechanism_keys, normalize_mechanism
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord, PlanningCallRecord
from app.marketing.planner import example_result, system_prompt
from tests.test_marketing_planning import MarketingPlanningFixtures

DETAILS = ("how_to_tell", "visual_mechanism", "evidence_basis")


def blocking_review(target):
    return {"id": "r_block", "kind": "angle_overlap", "severity": "blocking", "target_ids": [target],
            "finding": "Both angles promise the same reassurance.", "suggestion": "Merge or sharpen one angle.",
            "source": "ai", "resolved": False}


def guiding_question():
    return {"id": "q_offer", "question": "Which offer should this round use?",
            "options": ["Current list price", "Launch discount"], "multiple": False,
            "why_needed": "The offer changes the business goal.", "selected": [], "custom_answer": ""}


class TestExpressionDistinctness(MarketingPlanningFixtures):
    def freeze_body(self):
        return {"start_date": "2025-01-01", "end_date": "2025-01-07", "account_timezone": "UTC",
                "execution_mode": "mock", "protocol": {"min_days": 7, "attribution_lag_days": 8}}

    def test_mock_plan_gives_distinct_expressions_and_a_review_example(self):
        project = self.drafted()
        plan = project["plan"]
        for angle in plan["angles"]:
            mechanisms = [normalize_mechanism(e["visual_mechanism"]) for e in angle["expressions"]]
            self.assertEqual(len(mechanisms), len(set(mechanisms)), angle)
            for expression in angle["expressions"]:
                self.assertTrue(expression["how_to_tell"])
                self.assertTrue(expression["evidence_basis"]["fact_ids"] or expression["evidence_basis"]["gap"])
        self.assertEqual(plan["guiding_questions"], [])
        self.assertTrue([r for r in plan["plan_review"] if r["source"] == "ai"])
        self.assertTrue(all(r["severity"] == "advisory" for r in plan["plan_review"]))
        confirmed = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_reworded_expressions_are_flagged_on_save_and_block_confirmation(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        first, second = plan["angles"][0]["expressions"][:2]
        first.update(name="高功率与认证保障")
        second.update(name="高效快充，安全耐用", visual_mechanism="other: Live-Connection demo ")
        first["visual_mechanism"] = "live_connection_demo"
        saved = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(saved.status_code, 200, saved.text)
        saved = saved.json()
        flagged = [r for r in saved["plan"]["plan_review"] if r["source"] == "system" and r["kind"] == "expression_duplicate"]
        self.assertEqual(len(flagged), 1, flagged)
        self.assertEqual(flagged[0]["kind"], "expression_duplicate")
        self.assertEqual(flagged[0]["severity"], "blocking")
        self.assertEqual(set(flagged[0]["target_ids"]), {plan["angles"][0]["id"], first["id"], second["id"]})
        self.assertIn("高功率与认证保障", flagged[0]["finding"])
        self.assertIn("高效快充，安全耐用", flagged[0]["finding"])

        blocked = self.edit(saved, "confirm_plan", plan=saved["plan"])
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["code"], "expressions_not_distinct")
        self.assertEqual({e["field"] for e in blocked.json()["field_errors"]}, {
            "body.plan.angles.0.expressions.0.visual_mechanism", "body.plan.angles.0.expressions.1.visual_mechanism"})
        self.assertEqual(self.read(saved)["revision"], saved["revision"])

        fixed = deepcopy(saved["plan"])
        fixed["angles"][0]["expressions"][1]["visual_mechanism"] = "route_map"
        # Both expressions changed after their English image briefs were written (#292).
        for combo in fixed["combinations"]:
            if combo["expression_id"] in (first["id"], second["id"]):
                combo["image_brief"]["expression"] = "High power and certified safety"
        confirmed = self.edit(saved, "confirm_plan", plan=fixed)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertFalse([r for r in confirmed.json()["plan"]["plan_review"]
                          if r["source"] == "system" and r["kind"] == "expression_duplicate"])

    def test_ai_draft_with_reworded_expressions_is_kept_but_cannot_be_confirmed(self):
        project = self.understood()
        output = example_result("plan", {"understanding": project["understanding"], "ad_language": "en-US"})
        output["angles"][0]["expressions"][1]["visual_mechanism"] = output["angles"][0]["expressions"][0]["visual_mechanism"]
        # A planner may not resolve its own findings, answer for the user or forge system checks.
        output["plan_review"][0]["resolved"] = True
        output["plan_review"].append({**blocking_review(output["angles"][1]["id"]), "id": "forged", "source": "system"})
        output["guiding_questions"] = [{**guiding_question(), "selected": ["Launch discount"]}]
        with patch("app.marketing.planner.example_result", return_value=output):
            self.execute(self.plan_call(project, "plan"))
        drafted = self.read(project)
        self.assertEqual(drafted["latest_call"]["state"], "succeeded")
        self.assertEqual(drafted["state"], "plan_draft")
        review = drafted["plan"]["plan_review"]
        self.assertFalse(review[0]["resolved"])
        self.assertNotIn("forged", [r["id"] for r in review])
        self.assertEqual([r["kind"] for r in review if r["source"] == "system"],
                         ["expression_duplicate", "evidence_gap", "budget_insufficient"])
        self.assertEqual(drafted["plan"]["guiding_questions"][0]["selected"], [])
        blocked = self.edit(drafted, "confirm_plan", plan=drafted["plan"])
        self.assertEqual(blocked.json()["code"], "expressions_not_distinct", blocked.text)

    def test_expression_details_validate_mechanism_and_evidence(self):
        project = self.drafted()
        cases = [
            ("visual_mechanism", "sparkles", "visual_mechanism_invalid"),
            ("visual_mechanism", "other:", "visual_mechanism_invalid"),
            ("evidence_basis", {"fact_ids": [], "gap": ""}, "evidence_basis_required"),
        ]
        for field, value, code in cases:
            plan = deepcopy(project["plan"])
            plan["angles"][0]["expressions"][0][field] = value
            response = self.edit(project, "save_plan", plan=plan)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn(code, [e["code"] for e in response.json()["field_errors"]], response.text)
        plan = deepcopy(project["plan"])
        plan["angles"][0]["expressions"][0]["evidence_basis"] = {"fact_ids": ["missing_fact"], "gap": ""}
        response = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "plan_evidence_invalid")
        plan = deepcopy(project["plan"])
        plan["angles"][0]["expressions"][0]["visual_mechanism"] = "other:Before and after cable test"
        self.assertEqual(self.edit(project, "save_plan", plan=plan).status_code, 200)

    def test_blocking_review_and_unanswered_questions_gate_confirmation(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        plan["plan_review"].append(blocking_review(plan["angles"][0]["id"]))
        plan["guiding_questions"] = [guiding_question()]
        saved = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(saved.status_code, 200, saved.text)
        saved = saved.json()
        index = next(i for i, r in enumerate(saved["plan"]["plan_review"]) if r["id"] == "r_block")
        blocked = self.edit(saved, "confirm_plan", plan=saved["plan"])
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["code"], "plan_review_unresolved")
        self.assertEqual({e["field"] for e in blocked.json()["field_errors"]}, {
            f"body.plan.plan_review.{index}", "body.plan.guiding_questions.0"})

        invalid = deepcopy(saved["plan"])
        invalid["guiding_questions"][0]["selected"] = ["Something else"]
        self.assertIn("guiding_answer_invalid", [e["code"] for e in self.edit(saved, "save_plan", plan=invalid).json()["field_errors"]])
        invalid["guiding_questions"][0]["selected"] = ["Current list price", "Launch discount"]
        self.assertIn("guiding_answer_invalid", [e["code"] for e in self.edit(saved, "save_plan", plan=invalid).json()["field_errors"]])
        invalid = deepcopy(saved["plan"])
        invalid["guiding_questions"] = [{**guiding_question(), "id": f"q{n}"} for n in range(4)]
        self.assertEqual(self.edit(saved, "save_plan", plan=invalid).status_code, 422)

        answered = deepcopy(saved["plan"])
        answered["guiding_questions"][0]["custom_answer"] = "Use the launch bundle"
        answered = self.edit(saved, "save_plan", plan=answered)
        self.assertEqual(answered.status_code, 200, answered.text)
        answered = answered.json()
        self.assertGreater(answered["revision"], saved["revision"])
        still = self.edit(answered, "confirm_plan", plan=answered["plan"])
        self.assertEqual(still.json()["code"], "plan_review_unresolved")
        resolved = deepcopy(answered["plan"])
        resolved["plan_review"][index]["resolved"] = True
        confirmed = self.edit(answered, "confirm_plan", plan=resolved)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_redraft_freezes_answers_and_catalog_as_planner_input(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        plan["guiding_questions"] = [{**guiding_question(), "selected": ["Launch discount"]}]
        saved = self.edit(project, "save_plan", plan=plan).json()
        call = self.plan_call(saved, "plan")
        with tenant_session(self.context) as session:
            inputs = session.get(PlanningCallRecord, call["id"]).input_json
        self.assertEqual(inputs["guiding_answers"], [{"id": "q_offer", "question": "Which offer should this round use?",
            "selected": ["Launch discount"], "custom_answer": ""}])
        self.assertEqual(inputs["expression_catalog"]["version"], CATALOG_VERSION)
        self.assertEqual({e["visual_mechanism"] for e in inputs["expression_catalog"]["entries"]}, mechanism_keys())
        prompt = system_prompt("plan", inputs)
        for rule in ("visual_mechanism", "other:", "guiding_answers", "plan_review", "expressions_not_distinct"):
            self.assertIn(rule, prompt)

    def test_legacy_plan_opens_marked_for_completion_without_rewriting_history(self):
        project = self.drafted()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            legacy = deepcopy(row.plan_json)
            legacy.pop("plan_review")
            legacy.pop("guiding_questions")
            for angle in legacy["angles"]:
                for expression in angle["expressions"]:
                    for key in DETAILS:
                        expression.pop(key)
            row.plan_json, row.state = legacy, "plan_confirmed"
            revisions = [(r.id, r.content_hash) for r in session.scalars(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == row.id))]
        opened = self.read(project)
        self.assertTrue(all(key not in e for a in opened["plan"]["angles"] for e in a["expressions"] for key in DETAILS))
        blocked = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(opened["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["code"], "expression_details_required")
        incomplete = self.edit(opened, "save_plan", plan=opened["plan"])
        self.assertEqual(incomplete.status_code, 422, incomplete.text)
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingProjectRecord, project["id"]).plan_json, legacy)
            self.assertEqual([(r.id, r.content_hash) for r in session.scalars(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"]))], revisions)
        completed = self.edit(opened, "confirm_plan", plan=project["plan"])
        self.assertEqual(completed.status_code, 200, completed.text)

    def test_drafts_prepared_before_expression_details_keep_their_frozen_intent(self):
        project = self.edit(self.drafted(), "confirm_plan", plan=self.drafted()["plan"])
        project = project.json()
        prepared = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(prepared.status_code, 201, prepared.text)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            legacy = deepcopy(row.plan_json)
            for angle in legacy["angles"]:
                for expression in angle["expressions"]:
                    for key in DETAILS:
                        expression.pop(key)
            row.plan_json = legacy
        preview = self.client.post(f"/api/v1/generation/drafts/{prepared.json()['generation_draft_ids'][0]}/previews",
            headers=self.headers(1))
        self.assertEqual(preview.status_code, 201, preview.text)
        from app.marketing.service import creative_intent
        with tenant_session(self.context) as session:
            intent = creative_intent(session.get(MarketingProjectRecord, project["id"]), project["plan"]["combinations"][0]["id"])
        self.assertEqual(set(intent["expression"]), {"id", "name", "description", "fit_reason", "required_evidence"})

    def test_plan_review_projection_is_opt_in_for_rolling_clients(self):
        project = self.drafted()
        self.assertEqual(project["plan_review_support"], "v1")
        for query in ("", "?include_languages=true"):
            legacy = self.client.get(f"/api/v1/marketing/projects/{project['id']}{query}").json()
            self.assertNotIn("plan_review_support", legacy)
            self.assertNotIn("plan_review", legacy["plan"])
            self.assertNotIn("guiding_questions", legacy["plan"])
            self.assertTrue(all(key not in e for a in legacy["plan"]["angles"] for e in a["expressions"] for key in DETAILS))
        confirmed = self.edit(project, "confirm_plan", plan=project["plan"]).json()
        freeze = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds",
            headers=self.headers(confirmed["revision"]), json=self.freeze_body())
        self.assertEqual(freeze.status_code, 201, freeze.text)
        snapshot = freeze.json()["data"]["snapshot"]["plan"]
        self.assertNotIn("plan_review", snapshot)
        self.assertTrue(all(key not in e for a in snapshot["angles"] for e in a["expressions"] for key in DETAILS))
        opted = self.client.get(f"/api/v1/marketing/rounds/{freeze.json()['id']}?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true")
        with tenant_session(self.context) as session:
            from app.marketing.workflow_models import MarketingRoundRecord
            stored = session.get(MarketingRoundRecord, freeze.json()["id"]).snapshot_json["plan"]
        self.assertIn("visual_mechanism", stored["angles"][0]["expressions"][0])
        self.assertEqual(opted.json()["data"]["snapshot"]["plan"], stored)

    def test_plan_check_commands_do_not_cross_tenants_or_survive_suspension(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        plan["guiding_questions"] = [{**guiding_question(), "selected": ["Launch discount"]}]
        original = self.context
        self.context = self._make_context()
        denied = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(denied.status_code, 404, denied.text)
        self.context = original
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "suspended"
        denied = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(denied.status_code, 403, denied.text)
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "active"
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingProjectRecord, project["id"]).plan_json["guiding_questions"], [])

    def test_removing_a_reviewed_object_keeps_the_finding_without_dangling_targets(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        reviewed = plan["angles"][1]["id"]
        removed = {c["id"] for c in plan["combinations"] if c["angle_id"] == reviewed}
        plan["angles"] = [a for a in plan["angles"] if a["id"] != reviewed]
        plan["combinations"] = [c for c in plan["combinations"] if c["id"] not in removed]
        plan["groups"] = [g for g in plan["groups"] if not set(g["combination_ids"]) & removed]
        plan.update(comparison_kind="business_retest", limited_expression_coverage=True)
        saved = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["plan"]["plan_review"][0]["target_ids"], [])


def test_catalog_is_open_but_normalizes_restated_mechanisms():
    from app.marketing.expression_catalog import valid_mechanism
    assert valid_mechanism("route_map")
    assert valid_mechanism("other:Before and after cable test")
    assert not valid_mechanism("other:   ")
    assert not valid_mechanism("route-map")
    assert not valid_mechanism("other:" + "x" * 81)
    assert normalize_mechanism("other: Route-Map ") == normalize_mechanism("route_map") == "route map"
    assert normalize_mechanism("other:Route map demo") != normalize_mechanism("route_map")


def test_web_mechanism_choices_mirror_the_catalog_and_are_localized():
    import json
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "web"
    source = (root / "components/workspace/MarketingPlanReview.tsx").read_text()
    listed = source.split("export const VISUAL_MECHANISMS = [", 1)[1].split("]", 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", listed)) == mechanism_keys()
    messages = json.loads((root / "locales/en/common.json").read_text())
    assert all(f"marketing-visual-mechanism-{key}" in messages for key in mechanism_keys())
