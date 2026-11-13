"""QC correction and evidence-bound interpretation through native HTTP seams."""
import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, JobRecord
from app.generation.models import RunItemRecord, GenerationRecipeSnapshotRecord
from app.generation.paid_execution import execute_run_item
from app.marketing.models import PlanningCallRecord
from app.marketing.planner import execute_planning
from app.marketing.explanation import validate_output
from app.providers.adapter import ProviderCallResult
from tests.support.library_fixtures import png_bytes
from tests.test_marketing_workflow import MarketingWorkflowFixtures


class TestMarketingFollowup(MarketingWorkflowFixtures):
    def revoke_creative(self, row):
        from app.generation.review import create_review
        from app.marketing import workflow
        asset_id = row['evaluation']['qualified_asset_version_ids'][0]
        with tenant_session(self.context) as session:
            prior = workflow.active_quality(session, self.context, asset_id)
            create_review(session, self.context, asset_id, 'rejected',
                'Isolated fixture: product geometry needs correction', self.context.initiator_id, prior[0].id)
        return asset_id

    def test_revoked_approval_hides_current_explanation_and_blocks_paid_admission(self):
        import json
        from app.marketing.explanation import example
        from app.marketing.workflow import Evaluation
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            row = self.paid_evaluated()
            original = row['evaluation']
            self.assertTrue(original['current'])
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            row = self.command(row, 'explanation', {'execution_mode': 'byok', 'quote_hash': cap['quote_hash']}, 202)
            with tenant_session(self.context) as session:
                call = session.get(PlanningCallRecord, row['explanation']['id'])
                output = json.dumps(example(call.input_json))
            self.execute({'id': row['explanation']['id']}, complete=AsyncMock(return_value=output))
            before = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
            self.assertIsNotNone(before['explanation']['data'])
            revoked = self.revoke_creative(before)
            row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
            self.assertFalse(row['evaluation']['current'])
            self.assertFalse(row['explanation']['current'])
            self.assertIsNone(row['explanation']['data'])
            response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/explanation", headers=self.headers(row['revision']),
                json={'execution_mode': 'byok', 'quote_hash': cap['quote_hash']})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['code'], 'evaluation_evidence_changed')
            refreshed = self.command(row, 'evaluate')
            self.assertTrue(refreshed['evaluation']['current'])
            self.assertNotEqual(refreshed['evaluation']['id'], original['id'])
            self.assertNotIn(revoked, refreshed['evaluation']['qualified_asset_version_ids'])
            with tenant_session(self.context) as session:
                self.assertIn(revoked, session.get(Evaluation, original['id']).result_json['qualified_asset_version_ids'])
                from app.marketing.workflow import active_quality
                from app.generation.review import create_review
                prior = active_quality(session, self.context, revoked)
                create_review(session, self.context, revoked, 'approved', 'Fixture corrected review, same immutable image', self.context.initiator_id, prior[0].id)
            restored = self.command(refreshed, 'evaluate')
            self.assertTrue(restored['evaluation']['current'])
            self.assertNotIn(restored['evaluation']['id'], (original['id'], refreshed['evaluation']['id']))
            self.assertIn(revoked, restored['evaluation']['qualified_asset_version_ids'])

    def test_revoked_approval_before_explanation_dispatch_releases_reservation(self):
        from app.entitlements.cost_models import SpendReservationRecord
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            row = self.paid_evaluated()
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            row = self.command(row, 'explanation', {'execution_mode': 'byok', 'quote_hash': cap['quote_hash']}, 202)
            call_id = row['explanation']['id']
            self.revoke_creative(row)
            provider = AsyncMock()
            self.execute({'id': call_id}, complete=provider)
            provider.assert_not_awaited()
            with tenant_session(self.context) as session:
                call = session.get(PlanningCallRecord, call_id)
                self.assertEqual(call.state, 'failed')
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual(reservation.state, 'released')

    def test_reapproval_without_intermediate_evaluation_does_not_revive_old_evidence(self):
        from app.entitlements.cost_models import SpendReservationRecord
        from app.generation.review import create_review
        from app.marketing import workflow
        with patch.dict('os.environ', {'MARKETAI_PLANNING_PROVIDER': 'openai', 'MARKETAI_PLANNING_MODEL': 'text-test', 'MARKETAI_PLANNING_MAX_COST_USD': '0.10'}):
            row = self.paid_evaluated()
            original = row['evaluation']
            cap = self.client.get('/api/v1/marketing/capabilities').json()['paid']
            row = self.command(row, 'explanation', {'execution_mode': 'byok', 'quote_hash': cap['quote_hash']}, 202)
            call_id = row['explanation']['id']
            asset_id = self.revoke_creative(row)
            with tenant_session(self.context) as session:
                prior = workflow.active_quality(session, self.context, asset_id)
                create_review(session, self.context, asset_id, 'approved', 'Fixture: fresh approval after withdrawal', self.context.initiator_id, prior[0].id)
            restored = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
            self.assertFalse(restored['evaluation']['current'])
            self.assertFalse(restored['explanation']['current'])
            response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/explanation", headers=self.headers(restored['revision']),
                json={'execution_mode': 'byok', 'quote_hash': cap['quote_hash']})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['code'], 'evaluation_evidence_changed')
            provider = AsyncMock()
            self.execute({'id': call_id}, complete=provider)
            provider.assert_not_awaited()
            with tenant_session(self.context) as session:
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == session.get(PlanningCallRecord, call_id).job_id))
                self.assertEqual(reservation.state, 'released')
                self.assertEqual(session.get(workflow.Evaluation, original['id']).result_json['qualified_asset_version_ids'], original['qualified_asset_version_ids'])
            refreshed = self.command(restored, 'evaluate')
            self.assertNotEqual(refreshed['evaluation']['id'], original['id'])
            self.assertTrue(refreshed['evaluation']['current'])
            self.assertIn(asset_id, refreshed['evaluation']['qualified_asset_version_ids'])
            # Even approval -> approval is a new review, not the old evidence.
            import json
            from app.marketing.explanation import example
            refreshed = self.command(refreshed, 'explanation', {'execution_mode': 'byok', 'quote_hash': cap['quote_hash']}, 202)
            with tenant_session(self.context) as session:
                output = json.dumps(example(session.get(PlanningCallRecord, refreshed['explanation']['id']).input_json))
            self.execute({'id': refreshed['explanation']['id']}, complete=AsyncMock(return_value=output))
            completed = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
            self.assertTrue(completed['explanation']['current'])
            self.assertIsNotNone(completed['explanation']['data'])
            with tenant_session(self.context) as session:
                prior = workflow.active_quality(session, self.context, asset_id)
                create_review(session, self.context, asset_id, 'approved', 'Fixture: amended approval evidence', self.context.initiator_id, prior[0].id)
            changed = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
            self.assertFalse(changed['evaluation']['current'])
            self.assertFalse(changed['explanation']['current'])
            self.assertIsNone(changed['explanation']['data'])

    def test_legacy_evaluation_without_review_ids_requires_unchanged_earlier_reviews(self):
        from copy import deepcopy
        from app.marketing import workflow
        from app.marketing.service import digest
        from app.adapters.persistence.models import new_id
        from app.generation.review import create_review
        _, row = self.evaluated()
        with tenant_session(self.context) as session:
            original = session.get(workflow.Evaluation, row['evaluation']['id'])
            inputs = deepcopy(original.input_json)
            inputs.pop('quality_review_ids')
            legacy = workflow.Evaluation(id=new_id('me'), workspace_id=self.context.workspace_id, round_id=row['id'],
                input_hash=digest(inputs), input_json=inputs, result_json=deepcopy(original.result_json))
            session.add(legacy); session.flush()
            domain_row = workflow.get_round(session, self.context, row['id'])
            self.assertTrue(workflow.evaluation_current(session, self.context, domain_row, legacy))
            asset_id = inputs['entries'][0]['asset_version_id']
            prior = workflow.active_quality(session, self.context, asset_id)
            create_review(session, self.context, asset_id, 'approved', 'Fixture: new review after a legacy evaluation', self.context.initiator_id, prior[0].id)
            self.assertFalse(workflow.evaluation_current(session, self.context, domain_row, legacy))
            self.assertNotIn('quality_review_ids', legacy.input_json)
            refreshed = workflow.evaluate(session, self.context, domain_row)
            self.assertNotEqual(refreshed.id, legacy.id)
            self.assertTrue(workflow.evaluation_current(session, self.context, domain_row, refreshed))

    def test_openai_text_request_is_bounded_standard_tier_and_not_stored(self):
        import httpx
        from app.marketing.planner import text_completion, system_prompt
        import json
        def handle(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["service_tier"], "default")
            self.assertIs(payload["store"], False)
            self.assertEqual(payload["max_tokens"], 8192)
            self.assertEqual(payload["model"], "gpt-4.1-mini-2025-04-14")
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            self.assertTrue(payload["response_format"]["json_schema"]["strict"])
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"focuses": []}'}}]})
        result = asyncio.run(text_completion('openai', 'gpt-4.1-mini-2025-04-14', b'controlled-key', system_prompt('explanation'), 'evidence',
                                             transport=httpx.MockTransport(handle)))
        self.assertEqual(result, '{"focuses": []}')

    def test_single_failed_execution_does_not_reject_or_blacklist_an_angle(self):
        from types import SimpleNamespace
        from tests.test_marketing_evaluation import fixture
        from app.marketing.evaluation import evaluate_snapshot
        from app.marketing.explanation import frozen_inputs
        inputs = fixture()
        for data in inputs["rows"]:
            if data["entry_id"] == "e0":
                data["spend"] = "2000"
        result = evaluate_snapshot(inputs)
        result.update(big_round_spend="15000", big_round_budget="20000", budget_exhausted=False, round_limit_reached=False)
        index = next(i for i, item in enumerate(result["results"]) if item["id"] == "asset:v0")
        self.assertEqual(result["results"][index]["business_status"], "does_not_pass")
        evidence = frozen_inputs(SimpleNamespace(id="round", snapshot_json=inputs["snapshot"]),
            SimpleNamespace(id="evaluation", input_hash="frozen-input", result_json=result, input_json={}))
        rendered = validate_output({"focuses": [{"evidence_id": f"result_{index}", "interpretation": "not_success"}]}, evidence)
        self.assertEqual(rendered["paragraphs"][0]["object_id"], "asset:v0")
        self.assertIn("不能永久否定整个角度", rendered["paragraphs"][0]["explanation"])
        self.assertNotIn("blacklist", rendered)

    def test_budget_exhaustion_can_close_without_champion_and_blocks_more_rounds(self):
        project, row = self.evaluated()
        self.assertTrue(row["evaluation"]["budget_exhausted"])
        self.assertEqual(row["evaluation"]["next_action"], "close_and_summarize")
        row = self.command(row, "close", {"reason": "insufficient_evidence", "note": "Budget spent; simulated data cannot supply real success"})
        project = self.read(project)
        project = self.edit(project, "confirm_plan", plan=project["plan"]).json()
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds", headers=self.headers(project["revision"]), json={
            "start_date": "2025-01-01", "end_date": "2025-01-07", "account_timezone": "UTC", "execution_mode": "mock", "protocol": {}})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "big_round_limit")
        self.assertEqual(row["state"], "closed")

    def test_meta_name_is_not_the_csv_identity(self):
        _, row = self.bound()
        csv = self.csv(row)
        lines = csv["csv_text"].splitlines()
        csv["csv_text"] = lines[0] + ",ad_name\n" + "\n".join(line + ",renamed-in-meta" for line in lines[1:]) + "\n"
        row = self.command(row, "metrics/preview", csv, 201)
        self.assertEqual(row["batches"][-1]["issues"], [])
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        self.assertEqual(row["batches"][-1]["row_count"], 35)

    def paid_evaluated(self):
        from tests.test_marketing_ai_qc import TestMarketingAIQC
        from app.marketing.handoff_worker import execute_export
        project, row = TestMarketingAIQC.setup_round(self, paid=True)
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Controlled provider fixture, not real performance"}, 201)
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        asyncio.run(execute_export(self.context, row["handoff"]["id"], self.blobs))
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        for i, entry in enumerate(row["handoff"]["entries"]):
            row = self.command(row, "bindings", {"entry_id": entry["entry_id"], "ad_account_id": "123", "ad_id": str(1000+i), "conditions_match": True}, 201)
        row = self.command(row, "metrics/preview", self.csv(row, source_mode="customer_file"), 201)
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        return self.command(row, "evaluate")

    def test_paid_explanation_timeout_retains_exposure_and_never_resends(self):
        from app.entitlements.cost_models import SpendReservationRecord
        with patch.dict('os.environ', {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            row = self.paid_evaluated()
            original = row["evaluation"]
            cap = self.client.get('/api/v1/marketing/capabilities').json()["paid"]
            row = self.command(row, "explanation", {"execution_mode": "byok", "quote_hash": cap["quote_hash"]}, 202)
            call_id = row["explanation"]["id"]
            complete = AsyncMock(side_effect=TimeoutError())
            self.execute({"id": call_id}, complete=complete)
            self.execute({"id": call_id}, complete=complete)
            complete.assert_awaited_once()
            row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
            self.assertEqual(row["explanation"]["state"], "uncertain")
            self.assertEqual(row["evaluation"], original)
            row = self.command(row, "explanation", {"execution_mode": "byok", "quote_hash": cap["quote_hash"]}, 202)
            self.assertEqual(row["explanation"]["id"], call_id)
            with tenant_session(self.context) as session:
                call = session.get(PlanningCallRecord, call_id)
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual(reservation.state, "uncertain")
                self.assertEqual(str(reservation.outstanding_budget_amount), "0.100000000000")

    def test_paid_invalid_interpretation_is_charged_but_basic_results_survive(self):
        from app.entitlements.cost_models import SpendReservationRecord
        with patch.dict('os.environ', {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            row = self.paid_evaluated()
            original = row["evaluation"]
            cap = self.client.get('/api/v1/marketing/capabilities').json()["paid"]
            row = self.command(row, "explanation", {"execution_mode": "byok", "quote_hash": cap["quote_hash"]}, 202)
            call_id = row["explanation"]["id"]
            self.execute({"id": call_id}, complete=AsyncMock(return_value='{"focuses":[{"evidence_id":"invented","interpretation":"retain_asset"}]}'))
            row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
            self.assertEqual(row["explanation"]["state"], "failed")
            self.assertIsNone(row["explanation"]["data"])
            self.assertEqual(row["evaluation"], original)
            with tenant_session(self.context) as session:
                call = session.get(PlanningCallRecord, call_id)
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual(reservation.state, "settled")

    def test_changed_evidence_before_dispatch_releases_cost_without_a_call(self):
        from app.entitlements.cost_models import SpendReservationRecord
        with patch.dict('os.environ', {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}):
            row = self.paid_evaluated()
            cap = self.client.get('/api/v1/marketing/capabilities').json()["paid"]
            row = self.command(row, "explanation", {"execution_mode": "byok", "quote_hash": cap["quote_hash"]}, 202)
            call_id = row["explanation"]["id"]
            old_batch = row["batches"][-1]["id"]
            csv = self.csv(row, source_mode="customer_file", replaces_batch_id=old_batch)
            csv["csv_text"] = csv["csv_text"].replace(",10,10000", ",11,10000")
            row = self.command(row, "metrics/preview", csv, 201)
            row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
            complete = AsyncMock()
            self.execute({"id": call_id}, complete=complete)
            complete.assert_not_awaited()
            with tenant_session(self.context) as session:
                call = session.get(PlanningCallRecord, call_id)
                self.assertEqual(call.state, "failed")
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual(reservation.state, "released")

    def evaluated(self):
        project, row = self.bound()
        row = self.command(row, "metrics/preview", self.csv(row), 201)
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        return project, self.command(row, "evaluate")

    def test_feedback_keeps_saved_review_and_additional_note_and_all_failed_checks(self):
        from app.marketing.workflow_models import MarketingFeedbackDraftRecord
        project = self.confirmed();self.generate(project);row = self.start_round(project)
        asset = row["assets"][0]
        row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "needs_changes",
            "checks": {k: "fail" for k in asset["required_checks"]}, "note": "Saved review: headline must clearly explain the travel use case."}, 201)
        row = self.command(row, "feedback-drafts", {"asset_version_id": asset["asset_version_id"],
            "provider": "mock", "model": "mock-v1", "note": "Additional adjustment: use fewer words."}, 201)
        draft = self.client.get(f"/api/v1/generation/drafts/{row['feedback_drafts'][0]['draft_id']}").json()
        prompt = draft["selectors"]["prompt"]
        self.assertIn("Saved review: headline", prompt)
        self.assertIn("Additional adjustment: use fewer words", prompt)
        for key in asset["required_checks"]:
            self.assertIn(f"\n- {key} (fail): ", prompt)
        with tenant_session(self.context) as session:
            record = session.scalar(select(MarketingFeedbackDraftRecord).where(MarketingFeedbackDraftRecord.round_id == row["id"]))
            self.assertIn("Saved review: headline", record.feedback_json["human_note"])
            self.assertEqual(record.feedback_json["request_note"], "Additional adjustment: use fewer words.")

    def test_qc_feedback_creates_new_version_and_requires_fresh_review(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        source = row["assets"][0]
        row = self.command(row, "reviews", {"asset_version_id": source["asset_version_id"], "quality": "needs_changes",
            "checks": {k: "fail" if k == "proposition_clear" else "pass" for k in source["required_checks"]},
            "note": "Enlarge the charging scene and make the travel value clear; keep the other comparison conditions."}, 201)
        body = {"asset_version_id": source["asset_version_id"], "provider": "mock", "model": "mock-v1"}
        original_row = row
        row = self.command(row, "feedback-drafts", body, 201, key="feedback-once")
        self.assertEqual(len(row["feedback_drafts"]), 1)
        replay = self.command(original_row, "feedback-drafts", body, 201, key="feedback-once")
        self.assertEqual(replay["feedback_drafts"], row["feedback_drafts"])
        draft_id = row["feedback_drafts"][0]["draft_id"]
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        self.assertIn("make the travel value clear", draft["selectors"]["prompt"])
        self.assertEqual(draft["regeneration_intent"], "quality_feedback")
        preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 201, preview.text)
        preview = preview.json()
        submitted = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
            json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})
        self.assertEqual(submitted.status_code, 202, submitted.text)
        with tenant_session(self.context) as session:
            item = session.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id == submitted.json()["generation_run_id"]))
            item_id = item.id
            self.assertEqual(item.source_asset_version_id, source["asset_version_id"])
            self.assertEqual(item.intent_kind, "quality_feedback")
            job_id = session.scalar(select(JobRecord.id).where(JobRecord.resource_id == item.id))
            original = session.get(AssetVersionRecord, source["asset_version_id"])
            old_hash, old_asset = original.content_sha256, original.asset_id
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(colour=(87, 12, 44)), "correction", False)
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        adapter.execute.assert_awaited_once()
        with tenant_session(self.context) as session:
            version = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.producing_run_item_id == item_id))
            self.assertEqual(version.asset_id, old_asset)
            self.assertEqual(version.parent_version_id, source["asset_version_id"])
            self.assertNotEqual(version.content_sha256, old_hash)
            self.assertEqual(session.get(AssetVersionRecord, source["asset_version_id"]).content_sha256, old_hash)
            new_id = version.id
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        new_asset = next(a for a in row["assets"] if a["asset_version_id"] == new_id)
        self.assertIsNone(new_asset["quality"])
        self.assertFalse(new_asset["ready"])
        self.assertEqual(len(row["assets"]), 6)
        selected = [a for a in row["assets"] if a["asset_version_id"] != source["asset_version_id"]]
        for asset in selected:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {key: "pass" for key in asset["required_checks"]}, "note": "Fresh review of this exact version"}, 201)
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in selected]}, 202)
        entry = next(e for e in row["handoff"]["entries"] if e["asset_version_id"] == new_id)
        self.assertEqual(entry["provenance"]["kind"], "qc_feedback_revision")
        self.assertEqual(entry["provenance"]["source_asset_version_id"], source["asset_version_id"])
        self.assertEqual(entry["provider"], "mock")

        # The gallery and its provider/run filters use the producing version,
        # while the historical Asset retains its original identity.
        from app.adapters.persistence.models import GenerationRunRecord
        run_id = submitted.json()["generation_run_id"]
        with tenant_session(self.context) as session:
            run = session.get(GenerationRunRecord, run_id)
            run.provider, run.model = "gemini", "version-specific-model"
        gallery = self.client.get("/api/v1/generation/assets", params={"run_id": run_id, "provider": "gemini"})
        self.assertEqual(gallery.status_code, 200, gallery.text)
        self.assertEqual([a["id"] for a in gallery.json()["items"]], [new_id])
        self.assertEqual(gallery.json()["items"][0]["model"], "version-specific-model")
        detail = self.client.get(f"/api/v1/generation/asset-versions/{new_id}").json()
        self.assertEqual(detail["provider"], "gemini")
        original_detail = self.client.get(f"/api/v1/generation/asset-versions/{source['asset_version_id']}").json()
        self.assertEqual(original_detail["provider"], "mock")


    def test_explicit_new_preview_can_generate_again_without_reusing_the_old_attempt(self):
        project = self.confirmed()
        self.generate(project)
        draft_id = self.read(project)["generation_draft_ids"][0]
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 201, preview.text)
        response = self.client.post('/api/v1/generation/runs', headers=self.headers(draft["revision"]),
            json={"preview_id": preview.json()["id"], "preview_hash": preview.json()["preview_hash"]})
        self.assertEqual(response.status_code, 202, response.text)
        from app.entitlements.cost_models import SpendReservationRecord
        with tenant_session(self.context) as session:
            item = session.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id == response.json()["generation_run_id"]))
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item.id))
            self.assertIsNotNone(reservation)
            self.assertEqual(len(reservation.semantic_key_hash), 64)

    def test_explanation_is_persisted_idempotent_and_never_mutates_verdicts(self):
        project, row = self.evaluated()
        original = row["evaluation"]
        project_before = self.read(project)
        row = self.command(row, "explanation", {"execution_mode": "mock"}, 202)
        call_id = row["explanation"]["id"]
        row = self.command(row, "explanation", {"execution_mode": "mock"}, 202)
        self.assertEqual(row["explanation"]["id"], call_id)
        self.execute({"id": call_id})
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        self.assertEqual(row["explanation"]["state"], "succeeded")
        self.assertTrue(row["explanation"]["current"])
        self.assertEqual(row["evaluation"], original)
        self.assertEqual(self.read(project)["revision"], project_before["revision"])
        self.assertEqual(row["explanation"]["data"]["evaluation_id"], original["id"])
        self.assertTrue(row["explanation"]["data"]["paragraphs"])
        self.assertEqual(row["explanation"]["execution_mode"], "mock")
        row = self.command(row, "metrics/preview", self.csv(row, replaces_batch_id=row["batches"][-1]["id"],
            csv_text=self.csv(row)["csv_text"].replace(",10,10000", ",11,10000")), 201)
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        self.assertFalse(row["explanation"]["current"])
        self.assertIsNone(row["explanation"]["data"])
        row = self.command(row, "evaluate")
        self.assertFalse(row["explanation"]["current"])

    def test_explanation_rejects_invented_evidence_or_upgraded_interpretations(self):
        _, row = self.evaluated()
        row = self.command(row, "explanation", {"execution_mode": "mock"}, 202)
        with tenant_session(self.context) as session:
            call = session.get(PlanningCallRecord, row["explanation"]["id"])
            inputs = call.input_json
        for report in (
            {"focuses": [{"evidence_id": "invented", "interpretation": "retain_asset"}]},
            {"focuses": [{"evidence_id": "result_0", "interpretation": "retain_asset"}]},
            {"focuses": [{"evidence_id": "comparison", "interpretation": "compare_scope"}], "summary": "角度已证明有效"},
        ):
            with self.assertRaises(ValueError):
                validate_output(report, inputs)
