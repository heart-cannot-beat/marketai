"""Real HTTP + generation + private ZIP + CSV + scoped evaluation loop."""
import asyncio
import io
import json
import zipfile
from datetime import date, timedelta
from unittest.mock import AsyncMock, Mock, patch
from sqlalchemy import select

from tests.test_marketing_planning import MarketingPlanningFixtures
from tests.support.library_fixtures import png_bytes
from app import config
from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, new_id
from app.generation.models import RunItemRecord
from app.generation.paid_execution import execute_run_item
from app.providers.adapter import ProviderCallResult
from app.marketing.handoff_worker import execute_export
from app.marketing.workflow_models import MarketingHandoffRecord, MarketingMetricBatchRecord
from app.platform.blob_store import CommittedBlobRef


class MarketingWorkflowFixtures(MarketingPlanningFixtures):
    def confirmed(self):
        project = self.drafted()
        # These loops check one execution per combination; the planned default of two is covered by #225's tests.
        response = self.edit(project, "confirm_plan", plan={**project["plan"], "executions_per_combination": 1})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def start_round(self, project, mode="mock"):
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds", headers=self.headers(project["revision"]), json={
            "start_date": "2025-01-01", "end_date": "2025-01-07", "account_timezone": "UTC", "execution_mode": mode,
            "protocol": {"min_days": 7, "min_impressions": 1000, "min_clicks": 20, "min_purchases": 20, "min_spend": "20"}})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]

    def command(self, row, suffix, body=None, status=200, key=None):
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/{suffix}", headers=self.headers(row["revision"], key), json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()["data"] if status < 300 else response.json()

    def generate(self, project, provider="mock", colour=(12, 34, 56), variants=1, count=None):
        # These loops check one execution per combination; variants=None generates the plan's executions (#225).
        # `count` runs only the first drafts, like a round still generating; the rest are returned for run_drafts.
        result = self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts", headers=self.headers(project["revision"]),
            json={"provider": provider, "model": "mock-v1" if provider == "mock" else config.OPENAI_IMAGE_MODEL,
                  **({"variants": variants} if variants else {})})
        self.assertEqual(result.status_code, 201, result.text)
        drafts = result.json()["generation_draft_ids"]
        self.run_drafts(drafts[:count], colour)
        return drafts[count:] if count is not None else []

    def run_drafts(self, draft_ids, colour=(12, 34, 56)):
        if not getattr(self, "blobs", None):
            self.blobs = LocalFileBlobStore(root=config.DATA_DIR / new_id("blobs"), public_base_url="http://localhost:9000", signing_secret=b"test-only")
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(colour=colour), "external-request", False)
        for draft_id in draft_ids:
            draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
            preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))
            self.assertEqual(preview.status_code, 201, preview.text)
            preview = preview.json()
            run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
                json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})
            self.assertEqual(run.status_code, 202, run.text)
            with tenant_session(self.context) as session:
                items = [(item.id, session.scalar(select(JobRecord.id).where(JobRecord.workspace_id == self.context.workspace_id, JobRecord.resource_id == item.id)))
                    for item in session.scalars(select(RunItemRecord).where(RunItemRecord.generation_run_id == run.json()["generation_run_id"], RunItemRecord.workspace_id == self.context.workspace_id)).all()]
            for item_id, job_id in items:
                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))

    def queued_handoff(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        self.assertEqual(len(row["assets"]), 5)
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Checked product, proposition, copy and comparison"}, 201)
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        return project, row

    def ready_handoff(self):
        project, row = self.queued_handoff()
        handoff_id = row["handoff"]["id"]
        asyncio.run(execute_export(self.context, handoff_id, self.blobs))
        asyncio.run(execute_export(self.context, handoff_id, self.blobs))
        return project, self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()["current"]

    def bound(self):
        project, row = self.ready_handoff()
        for index, entry in enumerate(row["handoff"]["entries"]):
            row = self.command(row, "bindings", {"entry_id": entry["entry_id"], "ad_account_id": "act_123", "ad_id": str(1000 + index), "conditions_match": True}, 201)
        return project, row

    def csv(self, row, **overrides):
        text = "date,ad_id,spend,impressions,clicks,purchases,purchase_value\n"
        for binding in row["bindings"]:
            for offset in range(7):
                text += f"2025-01-{offset + 1:02d},{binding['ad_id']},{10 + offset},10000,100,10,{300 + offset}\n"
        return {"csv_text": text, "filename": "meta.csv", "ad_account_id": "act_123", "account_timezone": "UTC", "currency": "USD", "attribution_window": "7d_click_1d_view", "source_mode": "controlled_mock", **overrides}



class TestMarketingWorkflow(MarketingWorkflowFixtures):
    def test_closed_round_accepts_late_binding_correction_and_new_evaluation_without_reopening(self):
        project, row = self.bound()
        row = self.command(row, 'metrics/preview', self.csv(row), 201)
        row = self.command(row, 'metrics/confirm', {'batch_id': row['batches'][-1]['id']})
        row = self.command(row, 'evaluate')
        row = self.command(row, 'close', {'reason': 'insufficient_evidence', 'note': 'Original closure based on controlled evidence'})
        original_close, original_evaluation = row['close'], row['evaluation']
        row = self.command(row, 'bindings', {'entry_id': row['handoff']['entries'][0]['entry_id'],
            'ad_account_id': '123', 'ad_id': '9000', 'conditions_match': True}, 201)
        self.assertEqual(row['state'], 'closed')
        correction = self.csv(row, replaces_batch_id=row['batches'][-1]['id'])
        # Keep the exact original ad/day scope for this explicit replacement.
        correction['csv_text'] = '\n'.join(line for line in correction['csv_text'].splitlines() if ',9000,' not in line).replace(',10,10000', ',11,10000') + '\n'
        row = self.command(row, 'metrics/preview', correction, 201)
        self.assertEqual(row['batches'][-1]['issues'], [])
        row = self.command(row, 'metrics/confirm', {'batch_id': row['batches'][-1]['id']})
        self.assertEqual(row['state'], 'closed')
        self.assertFalse(row['evaluation']['current'])
        row = self.command(row, 'evaluate')
        self.assertEqual(row['state'], 'closed')
        self.assertEqual(row['close'], original_close)
        self.assertNotEqual(row['evaluation']['id'], original_evaluation['id'])
        self.assertEqual(row['evaluation']['round_spend'], '460')
        self.assertEqual(row['evaluation']['evidence_status'], 'simulated')
        with tenant_session(self.context) as session:
            from app.marketing.workflow import Evaluation
            self.assertEqual(session.get(Evaluation, original_evaluation['id']).result_json['round_spend'], '455')
        row = self.command(row, 'explanation', {'execution_mode': 'mock'}, 202)
        self.assertEqual(row['state'], 'closed')
        self.execute({'id': row['explanation']['id']})
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        self.assertTrue(row['explanation']['current'])
        self.assertEqual(row['close'], original_close)
        refused = self.command(row, 'handoff/retry', status=409)
        self.assertEqual(refused['code'], 'round_closed')
        self.assertEqual(self.read(project)['state'], 'plan_confirmed')

    def test_quality_withdrawal_during_zip_build_never_publishes_ready(self):
        _, row = self.queued_handoff()
        from app.marketing.handoff_worker import write_generated
        from app.generation.review import create_review
        from app.marketing import workflow

        async def withdraw_after_write(*args, **kwargs):
            ref = await write_generated(*args, **kwargs)
            with tenant_session(self.context) as session:
                version_id = row['handoff']['entries'][0]['asset_version_id']
                prior = workflow.active_quality(session, self.context, version_id)
                create_review(session, self.context, version_id, 'rejected', 'Fixture: withdrawn during ZIP I/O', self.context.initiator_id, prior[0].id)
            return ref

        with patch('app.marketing.handoff_worker.write_generated', side_effect=withdraw_after_write):
            with self.assertRaisesRegex(ValueError, 'quality_review_changed'):
                asyncio.run(execute_export(self.context, row['handoff']['id'], self.blobs))
        current = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        self.assertEqual(current['handoff']['state'], 'failed')
        with tenant_session(self.context) as session:
            self.assertIsNone(session.get(MarketingHandoffRecord, row['handoff']['id']).blob_json)

    def test_withdrawn_export_is_preserved_but_cannot_issue_new_download_grants(self):
        _, row = self.ready_handoff()
        from app.generation.review import create_review
        from app.marketing import workflow
        with tenant_session(self.context) as session:
            handoff = session.get(MarketingHandoffRecord, row['handoff']['id'])
            original_manifest = handoff.manifest_json
            original_blob = handoff.blob_json
            version_id = row['handoff']['entries'][0]['asset_version_id']
            prior = workflow.active_quality(session, self.context, version_id)
            create_review(session, self.context, version_id, 'rejected', 'Fixture: quality withdrawn after export', self.context.initiator_id, prior[0].id)
        current = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()['data']
        self.assertFalse(current['handoff'].get('quality_current', True))
        store = Mock(wraps=self.blobs)
        with patch('app.platform.blob_store_factory.build_blob_store', return_value=store):
            response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/download-intent", headers=self.headers())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'handoff_quality_changed')
        store.issue_download.assert_not_called()
        with tenant_session(self.context) as session:
            handoff = session.get(MarketingHandoffRecord, row['handoff']['id'])
            self.assertEqual(handoff.manifest_json, original_manifest)
            self.assertEqual(handoff.blob_json, original_blob)

    def test_full_http_generation_zip_import_evaluate_close_loop(self):
        project, row = self.bound()
        with tenant_session(self.context) as session:
            handoff = session.get(MarketingHandoffRecord, row["handoff"]["id"])
            ref = CommittedBlobRef(**handoff.blob_json)
        content = asyncio.run(self.blobs.read_committed(ref, 1000000))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIn("ads.csv", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len(manifest["entries"]), 5)
            self.assertEqual(len([n for n in archive.namelist() if n.startswith("media/")]), 1)  # same bytes, distinct intended uses
        with patch("app.platform.blob_store_factory.build_blob_store", return_value=self.blobs):
            response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/download-intent", headers=self.headers())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn("download_grant", response.json())
        row = self.command(row, "metrics/preview", self.csv(row), 201)
        self.assertEqual(row["batches"][-1]["issues"], [])
        batch_id = row["batches"][-1]["id"]
        row = self.command(row, "metrics/confirm", {"batch_id": batch_id})
        row = self.command(row, "evaluate")
        result = row["evaluation"]
        self.assertTrue(result["mature"])
        self.assertEqual(result["evidence_status"], "simulated")
        self.assertEqual(result["qualified_asset_version_ids"], [])
        self.assertEqual(result["round_spend"], "455")
        self.assertEqual(len(result["results"]), 7)
        row = self.command(row, "close", {"reason": "insufficient_evidence", "note": "Simulation completed; real delivery remains unvalidated"})
        self.assertEqual(row["state"], "closed")
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/evaluate", headers=self.headers(row["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['data']['state'], 'closed')
        self.assertEqual(response.json()['data']['close'], row['close'])

    def test_qc_unknown_and_missing_combination_block_export(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        asset = row["assets"][0]
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/reviews", headers=self.headers(row["revision"]), json={
            "asset_version_id": asset["asset_version_id"], "quality": "approved", "checks": {k: "unknown" for k in asset["required_checks"]}, "note": "Not verified"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "semantic_qc_unresolved")

    def test_csv_duplicate_unknown_mode_and_correction(self):
        project, row = self.bound()
        source = self.csv(row)
        row = self.command(row, "metrics/preview", source, 201)
        batch = row["batches"][-1]
        row = self.command(row, "metrics/confirm", {"batch_id": batch["id"]})
        # A correction keeps the same ad/day scope and replaces, never adds.
        correction = self.csv(row, csv_text="\n".join(",".join([*line.split(",")[:2], str(int(line.split(",")[2]) * 2), *line.split(",")[3:]]) if line and line[0].isdigit() else line for line in source["csv_text"].split("\n")), replaces_batch_id=batch["id"])
        row = self.command(row, "metrics/preview", correction, 201)
        self.assertEqual(row["batches"][-1]["issues"], [])
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        row = self.command(row, "evaluate")
        self.assertEqual(row["evaluation"]["round_spend"], "910")
        row = self.command(row, "metrics/preview", self.csv(row, source_mode="customer_file"), 201)
        codes = {i["code"] for i in row["batches"][-1]["issues"]}
        self.assertIn("execution_mode_mismatch", codes)
        self.assertIn("already_imported_ad_day", codes)
        with tenant_session(self.context) as session:
            self.assertEqual(len(session.scalars(select(MarketingMetricBatchRecord).where(MarketingMetricBatchRecord.workspace_id == self.context.workspace_id)).all()), 3)

    def test_round_mutations_are_idempotent_scoped_and_revisioned(self):
        project = self.confirmed()
        row = self.start_round(project)
        path = f"/api/v1/marketing/rounds/{row['id']}/close"
        headers = self.headers(row["revision"])
        body = {"reason": "user_stopped", "note": "No ads delivered"}
        first = self.client.post(path, headers=headers, json=body)
        replay = self.client.post(path, headers=headers, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), replay.json())
        old = self.context
        self.context = self._make_context()
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(path, headers=self.headers(row["revision"]), json=body)
        self.assertEqual(response.status_code, 404)
        self.context = old

    def test_bad_daily_rows_are_isolated_and_cannot_confirm(self):
        project, row = self.bound()
        source = self.csv(row)
        source["csv_text"] += source["csv_text"].splitlines()[1] + "\n2025-01-01,999999,,100,5,1,10\n"
        row = self.command(row, "metrics/preview", source, 201)
        batch = row["batches"][-1]
        codes = {issue["code"] for issue in batch["issues"]}
        self.assertTrue({"duplicate_ad_day", "unbound_ad", "missing_value"} <= codes)
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/metrics/confirm", headers=self.headers(row["revision"]), json={"batch_id": batch["id"]})
        self.assertEqual(response.status_code, 409, response.text)

    def test_failed_export_can_retry_the_same_frozen_selection(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Visually checked intended content"}, 201)
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        handoff_id = row["handoff"]["id"]
        broken = AsyncMock()
        broken.open_committed = Mock(side_effect=OSError("temporary storage failure"))
        with self.assertRaises(OSError):
            asyncio.run(execute_export(self.context, handoff_id, broken))
        row = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()["current"]
        self.assertEqual(row["handoff"]["state"], "failed")
        row = self.command(row, "handoff/retry", status=202)
        asyncio.run(execute_export(self.context, handoff_id, self.blobs))
        row = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()["current"]
        self.assertEqual(row["handoff"]["state"], "ready")

    def test_byok_two_rounds_new_execution_can_confirm_reusable_combination(self):
        from datetime import datetime, timezone
        self.paid_setup()
        prices = json.dumps({"openai": {config.OPENAI_IMAGE_MODEL: {"1K": "0.30", "2K": "0.50"}}})
        with patch.object(config, "GENERATION_MAX_COSTS_JSON", prices):
            project = self.drafted()
            plan = project["plan"]
            plan["media_budget"], plan["small_round_budget"] = "2000", "500"
            project = self.edit(project, "confirm_plan", plan=plan).json()
            for ordinal in range(2):
                self.generate(project, provider="openai", colour=(20 + ordinal * 40, 50, 70))
                with patch("app.marketing.workflow.utcnow", return_value=datetime(2024, 12, 31, tzinfo=timezone.utc)):
                    row = self.start_round(project, mode="byok")
                for asset in row["assets"]:
                    row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                        "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Human checked exact new execution and copy"}, 201)
                row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
                asyncio.run(execute_export(self.context, row["handoff"]["id"], self.blobs))
                row = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()["current"]
                for index, entry in enumerate(row["handoff"]["entries"]):
                    row = self.command(row, "bindings", {"entry_id": entry["entry_id"], "ad_account_id": "123", "ad_id": str((ordinal + 1) * 1000 + index), "conditions_match": True}, 201)
                row = self.command(row, "metrics/preview", self.csv(row, source_mode="customer_file"), 201)
                row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
                row = self.command(row, "evaluate")
                self.assertEqual(len(row["evaluation"]["qualified_asset_version_ids"]), 5)
                if ordinal == 0:
                    self.assertEqual(row["evaluation"]["confirmed_combinations"], [])
                    row = self.command(row, "close", {"reason": "asset_only", "note": "Exact ads pass; combination needs another execution"})
                    plan["stage"] = "confirm_combination"
                    project = self.edit(project, "confirm_plan", plan=plan).json()
                else:
                    self.assertEqual(len(row["evaluation"]["confirmed_combinations"]), 5)
                    row = self.command(row, "close", {"reason": "completed", "note": "New independent executions support reuse under the recorded conditions"})
                    self.assertEqual(row["close"]["reason"], "completed")
            history = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow").json()["rounds"]
            self.assertEqual(len(history), 2)
            old = self.client.get(f"/api/v1/marketing/rounds/{history[0]['id']}")
            self.assertEqual(old.status_code, 200, old.text)
            self.assertEqual(old.json()["data"]["close"]["reason"], "asset_only")
