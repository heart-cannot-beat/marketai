"""Question-led planning through real HTTP, persistence and dispatch fences."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, new_id, utcnow
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import CredentialProtector
from app.entitlements.models import EntitlementProjectionRecord
from app.main import app
from app.marketing.contracts import TestPlan as MarketingTestPlan
from app.marketing.models import MarketingRevisionRecord, PlanningCallRecord
from app.marketing import plan_review, service
from app.marketing.planner import execute_planning, example_result
from app.platform.http_identity import request_tenant_context


class MarketingPlanningFixtures(LibraryFixtures):
    def setUp(self):
        self.context = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.context
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=True)
        self.addCleanup(self.client.close)

    def headers(self, revision=None, key=None):
        return {"Idempotency-Key": key or new_id("key"), **({"If-Match": f'"{revision}"'} if revision else {})}

    def create(self):
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true", headers=self.headers(),
            json={"source_text": "Alpha 充电器\n65W，多口，面向差旅办公用户。"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def read(self, project):
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def plan_call(self, project, kind="understanding", mode="mock", quote_hash="", key=None):
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(project["revision"], key), json={"kind": kind, "execution_mode": mode, "quote_hash": quote_hash,
                **({"ad_language": "en-US", "strategy_language": "en-US"} if kind == "plan" else {})})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def execute(self, call, complete=None):
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, call["id"])
            job = session.get(JobRecord, row.job_id)
            job.state = "running"
        kwargs = {"complete": complete} if complete else {}
        asyncio.run(execute_planning(self.context, call["id"], **kwargs))

    def edit(self, project, command, **fields):
        return self.client.patch(f"/api/v1/marketing/projects/{project['id']}?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true",
            headers=self.headers(project["revision"]), json={"command": command, **fields})

    def understood(self):
        project = self.create()
        self.execute(self.plan_call(project))
        project = self.read(project)
        understanding = project["understanding"]
        for fact in understanding["facts"]:
            fact["confirmation"] = "confirmed"
        response = self.edit(project, "confirm_understanding", understanding=understanding)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def drafted(self):
        project = self.understood()
        self.execute(self.plan_call(project, "plan"))
        return self.read(project)

    def paid_setup(self):
        with tenant_session(self.context) as session:
            admin = CredentialAdmin(CredentialProtector.local_only())
            credential = admin.create(session, self.owner_ctx(self.context), credential_key="planning",
                                      provider="openai", plaintext=b"tenant-planner-secret")
            admin.bind(session, self.owner_ctx(self.context), binding_scope="generation", provider="openai",
                       credential_version_id=credential.credential_version_id)
            session.merge(EntitlementProjectionRecord(workspace_id=self.context.workspace_id, entitlement_version=1,
                lease_sequence=1, semantic_hash="planning-test", issued_at=utcnow(), valid_until=utcnow() + timedelta(days=1),
                resolved_entitlements_json={"controlled_mock": True, "storage.bytes.max": 10 * 1024 ** 3}))
            session.add(SpendGuardrailRecord(workspace_id=self.context.workspace_id, budget_currency="USD",
                per_operation_limit=Decimal("1"), rolling_24h_limit=Decimal("5"), period_limit=Decimal("5"),
                outstanding_exposure_limit=Decimal("5"), created_at=utcnow(), updated_at=utcnow(), updated_by="owner"))



class TestMarketingPlanning(MarketingPlanningFixtures):
    def test_budget_validation_identifies_the_field_without_changing_saved_plan(self):
        project = self.drafted()
        plan = {**project["plan"], "small_round_budget": "1001", "media_budget": "1000"}
        response = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["field_errors"], [{
            "field": "body.plan.small_round_budget",
            "code": "small_round_budget_exceeds_media_budget",
            "message": "小轮预算不能超过大轮预算",
        }])
        self.assertEqual(self.read(project), project)

    def test_explicit_retry_gets_bounded_rejected_plan_and_validation_feedback(self):
        project=self.understood()
        plan=example_result("plan",{"understanding":project["understanding"]})
        plan["groups"]=[{**plan["groups"][0],"combination_ids":[c["id"] for c in plan["combinations"]]}]
        first=self.plan_call(project,"plan")
        # Controlled external return, with the same single-group mistake as the live response.
        with patch("app.marketing.planner.example_result",return_value=plan):
            self.execute(first)
        current=self.read(project)
        self.assertEqual(current["latest_call"]["state"],"failed")
        self.assertIsNone(current["plan"])
        with tenant_session(self.context) as session:
            row=service.get_project(session,self.context,project["id"])
            self.assertIsNotNone(service.planning_repair_context(session,self.context,row))
            original=row.understanding_json
            row.understanding_json={**original,"audience":"Changed audience"}
            self.assertIsNone(service.planning_repair_context(session,self.context,row))
            row.understanding_json=original
        second=self.plan_call(current,"plan")
        with tenant_session(self.context) as session:
            call=session.get(PlanningCallRecord,second["id"])
            feedback=call.input_json.get("rejected_plan_feedback")
            self.assertIsNotNone(feedback)
            self.assertEqual(feedback["previous_call_id"],first["id"])
            self.assertIn("至少需要两个测试组",feedback["validation_errors"][0]["msg"])
            self.assertEqual(len(feedback["rejected_plan"]["groups"]),1)
            self.assertEqual(call.state,"queued")

    def test_matching_product_quote_does_not_support_invented_certification(self):
        project = self.understood()
        understanding = project["understanding"]
        understanding["facts"][0].update(statement="UL 2252 certified, rated 500 A", source_quote="Alpha 充电器")
        response = self.edit(project, "confirm_understanding", understanding=understanding)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "fact_claim_unsupported")
        self.assertEqual(self.read(project)["revision"], project["revision"])

    def test_paid_understanding_accepts_equivalent_thousands_formatted_evidence(self):
        self.paid_setup()
        source = "Test adapter\nMaximum voltage 1,000V; maximum power 500 kW."
        response = self.client.post("/api/v1/marketing/projects?include_languages=true", headers=self.headers(),
            json={"source_text": source})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        output = example_result("understanding", {"source_text": source})
        output["facts"][0].update(statement="Maximum voltage 1000V, maximum power 500 kW.",
            source_quote="Maximum voltage 1,000V; maximum power 500 kW.")
        with patch.dict("os.environ", {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            call = self.plan_call(project, mode="byok", quote_hash=cap["quote_hash"])
        complete = AsyncMock(return_value=json.dumps(output))
        self.execute(call, complete)
        self.execute(call, complete)
        complete.assert_awaited_once()
        current = self.read(project)
        self.assertEqual(current["latest_call"]["state"], "succeeded")
        self.assertEqual(current["state"], "understanding_draft")
        fact = current["understanding"]["facts"][0]
        self.assertEqual(fact["source_quote"], output["facts"][0]["source_quote"])
        self.assertEqual(fact["confirmation"], "unconfirmed")

    def test_dynamic_expressions_and_explicit_group_membership(self):
        project = self.drafted()
        plan = project["plan"]
        self.assertEqual([len(a["expressions"]) for a in plan["angles"]], [2, 3])
        self.assertEqual([len(g["combination_ids"]) for g in plan["groups"]], [2, 3])
        response = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evidence_status"], "unvalidated")

    def test_shared_expression_keeps_angle_specific_fit_and_distinct_combinations(self):
        from copy import deepcopy
        project = self.drafted()
        plan = project['plan']
        shared = deepcopy(plan['angles'][0]['expressions'][0])
        shared['fit_reason'] = 'Shared presentation, specific rationale for the second proposition.'
        plan['angles'][1]['expressions'].append(shared)
        combination = {**deepcopy(plan['combinations'][0]), 'id': 'shared_second_angle',
            'angle_id': plan['angles'][1]['id'], 'headline': plan['angles'][1]['proposition']}
        plan['combinations'].append(combination)
        plan['groups'][1]['combination_ids'].append(combination['id'])
        confirmed = self.edit(project, 'confirm_plan', plan=plan)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        project = confirmed.json()
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project['revision']), json={'provider': 'mock', 'model': 'mock-v1'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(response.json()['generation_draft_ids']), 6)
        draft = response.json()['generation_draft_ids'][-1]
        preview = self.client.post(f'/api/v1/generation/drafts/{draft}/previews', headers=self.headers(1))
        self.assertEqual(preview.status_code, 201, preview.text)
        from app.generation.models import GenerationDraftRecord
        with tenant_session(self.context) as session:
            frozen = session.get(GenerationDraftRecord, draft).selectors_json['marketing_intent']
            self.assertEqual(frozen['expression']['fit_reason'], shared['fit_reason'])
        self.assertIn(combination['image_brief']['expression'], preview.json()['prompt'])
        # Reusing an identity for a different expression cannot silently merge it.
        plan['angles'][1]['expressions'][-1]['description'] = 'A materially different expression.'
        invalid = self.edit(project, 'save_plan', plan=plan)
        self.assertEqual(invalid.status_code, 422, invalid.text)

    def test_fact_confirmation_does_not_validate_inferences(self):
        project = self.understood()
        self.assertEqual(project["state"], "understanding_confirmed")
        self.assertEqual(project["evidence_status"], "unvalidated")
        self.assertTrue(project["understanding"]["inferences"])
        self.assertNotIn("validated_conclusions", project["understanding"])

    def test_missing_fact_source_and_ai_confirmation_rejected(self):
        project = self.create()
        fake = example_result("understanding", {"source_text": project["source_text"]})
        fake["facts"][0]["source_quote"] = "UL认证通过"
        response = self.edit(project, "save_understanding", understanding=fake)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "fact_source_missing")

    def test_optimistic_lock_and_immutable_history(self):
        project = self.understood()
        response = self.edit(project, "save_understanding", understanding=project["understanding"])
        self.assertEqual(response.status_code, 200, response.text)
        response = self.edit(project, "save_understanding", understanding=project["understanding"])
        self.assertEqual(response.status_code, 409, response.text)
        with tenant_session(self.context) as session:
            revisions = session.scalars(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"])).all()
            self.assertEqual(len(revisions), response.json().get("revision", project["revision"] + 1))
            self.assertEqual(revisions[0].content_json["state"], "input")

    def test_invalid_relationship_and_overclaim_rejected(self):
        project = self.drafted()
        plan = project["plan"]
        plan["conclusion_scope"] = "specific_factor"
        response = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(response.status_code, 422, response.text)
        plan["conclusion_scope"] = "combination"
        plan["combinations"][0]["expression_id"] = plan["angles"][1]["expressions"][0]["id"]
        self.assertEqual(self.edit(project, "save_plan", plan=plan).status_code, 422)

    def test_new_planning_intent_cannot_duplicate_pending_call(self):
        project = self.create()
        key = new_id("same")
        first = self.plan_call(project, key=key)
        replay = self.plan_call(project, key=key)
        self.assertEqual(first, replay)
        current = self.read(project)
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(current["revision"]), json={"kind": "understanding", "execution_mode": "mock"})
        self.assertEqual(response.status_code, 409, response.text)

    def test_cross_workspace_objects_and_project_lists_isolated(self):
        project = self.create()
        self.context = self._make_context()
        self.assertEqual(self.client.get(f"/api/v1/marketing/projects/{project['id']}?include_languages=true").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/marketing/projects").json()["items"], [])

    def test_single_retest_and_budget_validation(self):
        plan = self.drafted()["plan"]
        plan["comparison_kind"] = "business_retest"
        plan["groups"] = plan["groups"][:1]
        ids = set(plan["groups"][0]["combination_ids"])
        plan["combinations"] = [c for c in plan["combinations"] if c["id"] in ids]
        plan["angles"] = plan["angles"][:1]
        self.assertEqual(len(MarketingTestPlan.model_validate(plan).groups), 1)
        plan["small_round_budget"] = "9999"
        with self.assertRaises(ValueError):
            MarketingTestPlan.model_validate(plan)

    def test_confirmed_plan_creates_reusable_drafts_and_freezes_test_intent(self):
        project = self.drafted()
        project = self.edit(project, "confirm_plan", plan=project["plan"]).json()
        path = f"/api/v1/marketing/projects/{project['id']}/generation-drafts"
        body = {"provider": "mock", "model": "mock-v1"}
        first = self.client.post(path, headers=self.headers(project["revision"]), json=body)
        second = self.client.post(path, headers=self.headers(project["revision"]), json=body)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["generation_draft_ids"], second.json()["generation_draft_ids"])
        draft = first.json()["generation_draft_ids"][0]
        result = self.client.post(f"/api/v1/generation/drafts/{draft}/previews", headers=self.headers(1))
        self.assertEqual(result.status_code, 201, result.text)
        self.assertIn(project["plan"]["angles"][0]["proposition"], result.json()["prompt"])
        self.edit(project, "save_plan", plan=project["plan"])
        stale = self.client.post(f"/api/v1/generation/drafts/{draft}/previews", headers=self.headers(1))
        self.assertEqual(stale.status_code, 422, stale.text)

    def test_paid_timeout_keeps_exposure_and_does_not_repeat_call(self):
        self.paid_setup()
        with patch.dict("os.environ", {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            self.assertTrue(cap["available"], cap)
            call = self.plan_call(self.create(), mode="byok", quote_hash=cap["quote_hash"])
        complete = AsyncMock(side_effect=TimeoutError())
        self.execute(call, complete)
        self.execute(call, complete)
        self.assertEqual(complete.await_count, 1)
        self.assertEqual(complete.await_args.args[2], b"tenant-planner-secret")
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.operation_id == call["operation_id"]))
            self.assertEqual(reservation.state, "uncertain")
            self.assertEqual(reservation.outstanding_budget_amount, Decimal("0.10"))

    def test_invalid_ai_json_is_paid_failed_output_not_silent_template(self):
        self.paid_setup()
        project = self.create()
        with patch.dict("os.environ", {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            call = self.plan_call(project, mode="byok", quote_hash=cap["quote_hash"])
        self.execute(call, AsyncMock(return_value="not JSON"))
        current = self.read(project)
        self.assertIsNone(current["understanding"])
        self.assertEqual(current["latest_call"]["state"], "failed")
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.operation_id == call["operation_id"]))
            self.assertEqual(reservation.state, "settled")

    def test_rejected_paid_plan_keeps_diagnostic_without_changing_project_or_resending(self):
        self.paid_setup()
        project = self.understood()
        rejected = example_result("plan", {"understanding": project["understanding"]})
        rejected["small_round_budget"] = "待确认"
        with patch.dict("os.environ", {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            call = self.plan_call(project, "plan", "byok", cap["quote_hash"])
        complete = AsyncMock(return_value=json.dumps(rejected))
        self.execute(call, complete)
        self.execute(call, complete)
        complete.assert_awaited_once()
        current = self.read(project)
        self.assertIsNone(current["plan"])
        self.assertEqual(current["latest_call"]["state"], "failed")
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, call["id"])
            self.assertEqual(row.output_json["rejected_output"], rejected)
            error = row.output_json["validation_errors"][0]
            self.assertEqual(error["loc"], ["small_round_budget"])
            self.assertEqual(error["type"], "decimal_parsing")
            self.assertNotIn("input", error)

    def test_recovered_committed_fence_is_uncertain_without_network_dispatch(self):
        project = self.create()
        call = self.plan_call(project)
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, call["id"])
            from app.platform.operations import transition_operation, OperationState
            transition_operation(session, row.operation_id, OperationState.RUNNING, "test crash", self.context.executor)
            row.state = "running"
        complete = AsyncMock()
        self.execute(call, complete)
        complete.assert_not_awaited()
        self.assertEqual(self.read(project)["latest_call"]["state"], "uncertain")

    def test_generation_intent_is_resolved_from_server_plan_not_client_selectors(self):
        project = self.drafted()
        project = self.edit(project, "confirm_plan", plan=project["plan"]).json()
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts",
            headers=self.headers(project["revision"]), json={"provider": "mock", "model": "mock-v1"})
        draft_id = response.json()["generation_draft_ids"][0]
        self.client.patch(f"/api/v1/generation/drafts/{draft_id}", headers=self.headers(1),
                          json={"selectors": {"prompt": "edited creative", "marketing_intent": {"project_id": "forged"}}})
        response = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(2))
        self.assertEqual(response.status_code, 201, response.text)
        with tenant_session(self.context) as session:
            from app.adapters.persistence.models import ResolutionPreviewRecord
            preview = session.get(ResolutionPreviewRecord, response.json()["id"])
            intent = preview.snapshot_json["recipe"]["selection"]["marketing_intent"]
            self.assertEqual(intent["project_id"], project["id"])
            self.assertEqual(intent["question"], project["plan"]["question"])

    def test_product_source_revision_invalidates_plan_and_keeps_prior_evidence(self):
        project = self.drafted()
        response = self.edit(project, "revise_source", source_text=project["source_text"] + "\n新增信息：旅行携带。")
        self.assertEqual(response.status_code, 200, response.text)
        revised = response.json()
        self.assertEqual(revised["state"], "input")
        self.assertIsNone(revised["plan"])
        with tenant_session(self.context) as session:
            prior = session.scalar(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"], MarketingRevisionRecord.revision == project["revision"]))
            # The revision keeps the server's image-brief record (#292); clients read the plan without it.
            self.assertEqual(plan_review.plan_document(prior.content_json["plan"]), project["plan"])
