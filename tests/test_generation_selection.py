"""Provider-selection HTTP/Worker tests; only external adapters are faked."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from tests.support.library_fixtures import LibraryFixtures, png_bytes
from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord, JobRecord, GenerationRunRecord, new_id, utcnow
from app.adapters.blob_store_local import LocalFileBlobStore
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import CredentialProtector
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord, ProviderCostLedgerEntryRecord, ProviderCallAttemptRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.generation.models import RunItemRecord
from app.generation.paid_execution import execute_run_item, _prepare
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.providers.adapter import ProviderCallResult, ProviderDispatchError


class TestGenerationSelection(LibraryFixtures):
    def setUp(self):
        self.context = self._make_context()
        self.product = self._product_id(self.context)
        app.dependency_overrides[request_tenant_context] = lambda: self.context
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.prices = patch.object(config, "GENERATION_MAX_COSTS_JSON", json.dumps({
            "openai": {config.OPENAI_IMAGE_MODEL: {"1K": "0.30", "2K": "0.50"}},
        }))
        self.prices.start()
        self.addCleanup(self.prices.stop)
        self.blobs = LocalFileBlobStore(root=config.DATA_DIR / new_id("blobs"),
                                       public_base_url="http://localhost:9000", signing_secret=b"test-only")

    def headers(self, key=None, revision=None):
        return {"Idempotency-Key": key or new_id("key"),
                **({"If-Match": f'"{revision}"'} if revision else {})}

    def paid_setup(self, ctx=None, scope="generation", limit="5"):
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            owner = self.owner_ctx(ctx)
            admin = CredentialAdmin(CredentialProtector.local_only())
            meta = admin.create(session, owner, credential_key="image-key", provider="openai", plaintext=b"test-secret")
            admin.bind(session, owner, binding_scope=scope, provider="openai", credential_version_id=meta.credential_version_id)
            session.add(EntitlementProjectionRecord(workspace_id=ctx.workspace_id, entitlement_version=1,
                        lease_sequence=1, semantic_hash="test", issued_at=utcnow(), valid_until=utcnow() + timedelta(days=1),
                        resolved_entitlements_json={"controlled_mock": True}))
            session.add(SpendGuardrailRecord(workspace_id=ctx.workspace_id, budget_currency="USD",
                        per_operation_limit=Decimal(limit), rolling_24h_limit=Decimal(limit), period_limit=Decimal(limit),
                        outstanding_exposure_limit=Decimal(limit), created_at=utcnow(), updated_at=utcnow(), updated_by="owner"))

    def draft(self, provider="mock", variants=1):
        body = {"product_id": self.product, "provider": provider,
                "model": "mock-v1" if provider == "mock" else config.OPENAI_IMAGE_MODEL,
                "execution_mode": "mock" if provider == "mock" else "byok",
                "combination_strategy": {"mode": "single"}, "selectors": {"prompt": "A clean product scene"},
                "format_config": {"aspect_ratio": "4:5", "image_size": "1K", "variants": variants}}
        response = self.client.post("/api/v1/generation/drafts", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def preview(self, draft):
        response = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(revision=draft["revision"]))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def submit(self, draft, preview, key=None):
        return self.client.post("/api/v1/generation/runs", json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]},
                                headers=self.headers(key=key, revision=draft["revision"]))

    def items(self, run_id):
        with tenant_session(self.context) as session:
            return [(i.id, session.scalar(select(JobRecord.id).where(JobRecord.workspace_id == self.context.workspace_id,
                                                                      JobRecord.resource_id == i.id)))
                    for i in session.scalars(select(RunItemRecord).where(RunItemRecord.workspace_id == self.context.workspace_id,
                                                                         RunItemRecord.generation_run_id == run_id)).all()]

    def test_catalog_never_uses_global_or_other_workspace_credentials(self):
        other = self._make_context()
        self.paid_setup(other)
        with patch.object(config, "OPENAI_API_KEY", "global-secret"):
            response = self.client.get("/api/v1/generation/capabilities")
        options = {p["id"]: p for p in response.json()["provider_options"]}
        self.assertFalse(options["openai"]["credential_ready"])
        self.assertNotIn("secret", response.text)

    def test_only_generation_binding_scope_can_generate(self):
        self.paid_setup(scope="unrelated")
        options = self.client.get("/api/v1/generation/capabilities").json()["provider_options"]
        self.assertFalse(next(o for o in options if o["id"] == "openai")["available"])

    def test_draft_and_preview_idempotency_and_provider_update(self):
        draft = self.draft()
        body = {"provider": "openai", "model": config.OPENAI_IMAGE_MODEL, "execution_mode": "byok"}
        headers = self.headers(revision=1)
        path = f"/api/v1/generation/drafts/{draft['id']}"
        first = self.client.patch(path, json=body, headers=headers)
        replay = self.client.patch(path, json=body, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(replay.json()["provider"], "openai")
        self.assertEqual(replay.json()["revision"], 2)

    def test_stale_preview_cannot_submit_after_draft_changed(self):
        draft = self.draft()
        preview = self.preview(draft)
        self.client.patch(f"/api/v1/generation/drafts/{draft['id']}", json={"selectors": {"prompt": "Different creative"}}, headers=self.headers(revision=1))
        result = self.submit(draft, preview)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["code"], "stale_draft_revision")

    def test_mock_http_worker_download_path_and_duplicate_submission(self):
        draft = self.draft(variants=2)
        preview = self.preview(draft)
        self.assertEqual(preview["selection_maximum_cost"], "0")
        self.assertEqual(preview["quantity"], 2)
        key = new_id("submit")
        result = self.submit(draft, preview, key)
        replay = self.submit(draft, preview, key)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json(), replay.json())
        run_id = result.json()["generation_run_id"]
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(), "external-request", False)
        for item_id, job_id in self.items(run_id):
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        self.assertEqual(adapter.execute.await_count, 2)
        self.assertEqual(self.client.get(f"/api/v1/generation/runs/{run_id}").json()["status"], "succeeded")
        with tenant_session(self.context) as session:
            version = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.workspace_id == self.context.workspace_id))
            version_id = version.id
            self.assertEqual(version.execution_mode, "mock")
            self.assertGreater(version.byte_size, 0)
        with patch("app.platform.blob_store_factory.build_blob_store", return_value=self.blobs):
            download = self.client.post(f"/api/v1/generation/asset-versions/{version_id}/download-intent", headers=self.headers())
        self.assertEqual(download.status_code, 201, download.text)
        self.assertIn("download_grant", download.json())

    def test_paid_dispatch_reserves_settles_and_uses_workspace_secret(self):
        self.paid_setup()
        draft = self.draft("openai")
        preview = self.preview(draft)
        self.assertEqual(preview["selection_maximum_cost"], "0.30")
        result = self.submit(draft, preview)
        self.assertEqual(result.status_code, 202, result.text)
        item_id, job_id = self.items(result.json()["generation_run_id"])[0]
        captured = []
        async def execute(request):
            captured.append(bytes(request.credential))
            return ProviderCallResult(png_bytes(), "request-1", False)
        adapter = AsyncMock()
        adapter.execute.side_effect = execute
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        self.assertEqual(captured, [b"test-secret"])
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertEqual(reservation.state, "settled")
            self.assertEqual(reservation.outstanding_budget_amount, 0)
            ledgers = session.scalars(select(ProviderCostLedgerEntryRecord).where(ProviderCostLedgerEntryRecord.spend_reservation_id == reservation.id)).all()
            self.assertEqual({l.entry_type for l in ledgers}, {"quote", "reservation", "estimated_settlement"})
            attempt = session.scalar(select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.run_item_id == item_id))
            self.assertEqual(attempt.chargeability, "charged")

    def test_failure_before_committed_dispatch_releases_cost_and_closes_run(self):
        from app.generation.paid_execution import record_execution_failure
        self.paid_setup()
        draft = self.draft("openai")
        run_id = self.submit(draft, self.preview(draft)).json()["generation_run_id"]
        item_id, job_id = self.items(run_id)[0]
        record_execution_failure(self.context.for_system_execution(), job_id, item_id)
        record_execution_failure(self.context.for_system_execution(), job_id, item_id)
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertEqual(reservation.state, "released")
            self.assertEqual(reservation.outstanding_budget_amount, 0)
            self.assertEqual(session.get(GenerationRunRecord, run_id).status, "failed")
            self.assertIsNone(session.scalar(select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.run_item_id == item_id)))

    def test_failure_after_committed_fence_keeps_exposure_uncertain(self):
        from app.generation.paid_execution import record_execution_failure
        self.paid_setup()
        draft = self.draft("openai")
        run_id = self.submit(draft, self.preview(draft)).json()["generation_run_id"]
        item_id, job_id = self.items(run_id)[0]
        with tenant_session(self.context) as session:
            _prepare(session, self.context, job_id, item_id)
        record_execution_failure(self.context.for_system_execution(), job_id, item_id)
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertEqual(reservation.state, "uncertain")
            self.assertEqual(reservation.outstanding_budget_amount, Decimal("0.30"))
            self.assertEqual(session.get(GenerationRunRecord, run_id).status, "uncertain")

    def test_paid_admission_needs_no_update_permission_on_reservations(self):
        self.paid_setup()
        draft = self.draft("openai")
        preview = self.preview(draft)
        with tenant_session(self.context) as session:
            engine = session.get_bind()
        def reject_update(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith("update spend_reservations "):
                raise PermissionError("API has INSERT, not UPDATE, on spend_reservations")
        event.listen(engine, "before_cursor_execute", reject_update)
        try:
            result = self.submit(draft, preview)
        finally:
            event.remove(engine, "before_cursor_execute", reject_update)
        self.assertEqual(result.status_code, 202, result.text)
        item_id, _ = self.items(result.json()["generation_run_id"])[0]
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertIsNotNone(reservation)
            self.assertIsNotNone(reservation.preview_confirmation_id)
            self.assertIsNotNone(reservation.recipe_snapshot_id)

    def test_timeout_keeps_exposure_and_never_redispatches(self):
        self.paid_setup()
        draft = self.draft("openai")
        result = self.submit(draft, self.preview(draft))
        item_id, job_id = self.items(result.json()["generation_run_id"])[0]
        adapter = AsyncMock()
        adapter.execute.side_effect = ProviderDispatchError("may_have_dispatched", "timeout")
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        self.assertEqual(adapter.execute.await_count, 1)
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertEqual(reservation.state, "uncertain")
            self.assertEqual(reservation.outstanding_budget_amount, Decimal("0.30"))
            self.assertEqual(session.get(RunItemRecord, item_id).state, "uncertain")

    def test_guardrail_rechecked_at_submission(self):
        self.paid_setup()
        draft = self.draft("openai")
        preview = self.preview(draft)
        with tenant_session(self.context) as session:
            session.get(SpendGuardrailRecord, self.context.workspace_id).per_operation_limit = Decimal("0.10")
        result = self.submit(draft, preview)
        self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(result.json()["code"], "budget_exceeded")
        # Over the Workspace guardrail: the copy's numbers travel as params (#253).
        self.assertEqual(result.json()["params"]["currency"], "USD")
        self.assertEqual(result.json()["params"]["limit"], "0.1")

    def test_crash_after_committed_fence_is_uncertain_on_resume(self):
        self.paid_setup()
        draft = self.draft("openai")
        result = self.submit(draft, self.preview(draft))
        item_id, job_id = self.items(result.json()["generation_run_id"])[0]
        with tenant_session(self.context) as session:
            _prepare(session, self.context, job_id, item_id)
        adapter = AsyncMock()
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        adapter.execute.assert_not_awaited()
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(RunItemRecord, item_id).state, "uncertain")

    # -- Usage provider_calls (#8): counted when the provider answered ------

    def _provider_calls(self, item_id):
        from app.entitlements.usage_models import UsageEventRecord
        with tenant_session(self.context) as session:
            attempt_ids = set(session.scalars(select(ProviderCallAttemptRecord.id).where(
                ProviderCallAttemptRecord.workspace_id == self.context.workspace_id,
                ProviderCallAttemptRecord.run_item_id == item_id)))
            rows = session.scalars(select(UsageEventRecord).where(
                UsageEventRecord.workspace_id == self.context.workspace_id)).all()
            return [row.execution_mode for row in rows if row.source_resource_id in attempt_ids]

    def _paid_item(self):
        draft = self.draft("openai")
        result = self.submit(draft, self.preview(draft))
        self.assertEqual(result.status_code, 202, result.text)
        return self.items(result.json()["generation_run_id"])[0]

    def _run(self, item_id, job_id, adapter, times=1):
        for _ in range(times):
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))

    def test_usage_counts_one_byok_call_for_a_paid_image_and_none_on_replay(self):
        self.paid_setup()
        item_id, job_id = self._paid_item()
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(), "request-1", False)
        self._run(item_id, job_id, adapter, times=2)
        self.assertEqual(adapter.execute.await_count, 1)
        self.assertEqual(self._provider_calls(item_id), ["byok"])

    def test_usage_counts_a_paid_call_whose_output_is_unusable(self):
        self.paid_setup()
        item_id, job_id = self._paid_item()
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(b"not an image", "request-2", False)
        self._run(item_id, job_id, adapter, times=2)
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertEqual(reservation.state, "settled")
        self.assertEqual(self._provider_calls(item_id), ["byok"])

    def test_usage_counts_nothing_for_a_paid_call_that_never_left_or_timed_out(self):
        self.paid_setup(limit="50")
        for certainty, expected in (("not_dispatched", "released"), ("may_have_dispatched", "uncertain")):
            with self.subTest(certainty=certainty):
                item_id, job_id = self._paid_item()
                adapter = AsyncMock()
                adapter.execute.side_effect = ProviderDispatchError(certainty, "refused or lost")
                self._run(item_id, job_id, adapter)
                with tenant_session(self.context) as session:
                    reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
                    self.assertEqual(reservation.state, expected)
                self.assertEqual(self._provider_calls(item_id), [])

    def test_usage_counts_a_controlled_mock_image_in_the_mock_bucket(self):
        draft = self.draft()
        result = self.submit(draft, self.preview(draft))
        item_id, job_id = self.items(result.json()["generation_run_id"])[0]
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(), "mock-request", False)
        self._run(item_id, job_id, adapter, times=2)
        self.assertEqual(self._provider_calls(item_id), ["mock"])

    def test_real_wire_status_controls_ledger_and_resume_never_replays(self):
        """HTTP commands + real provider adapters; only vendor transport replaced."""
        import httpx
        from app.providers.router import get_provider
        self.paid_setup()
        with tenant_session(self.context) as session:
            admin = CredentialAdmin(CredentialProtector.local_only())
            for provider in ("gemini", "seedream"):
                meta = admin.create(session, self.owner_ctx(self.context), credential_key=f"{provider}-fixture", provider=provider, plaintext=b"fixture-key")
                admin.bind(session, self.owner_ctx(self.context), binding_scope="generation", provider=provider, credential_version_id=meta.credential_version_id)
        prices = {p: {get_provider(p).default_model: {"1K": "0.30"}} for p in ("openai", "gemini", "seedream")}
        client = httpx.AsyncClient
        with patch.object(config, "GENERATION_MAX_COSTS_JSON", json.dumps(prices)):
            for provider in prices:
                for status, expected in ((400, "released"), (503, "uncertain")):
                    with self.subTest(provider=provider, status=status):
                        draft = self.draft()
                        response = self.client.patch(f"/api/v1/generation/drafts/{draft['id']}", json={"provider": provider, "model": get_provider(provider).default_model, "execution_mode": "byok"}, headers=self.headers(revision=draft["revision"]))
                        self.assertEqual(response.status_code, 200, response.text)
                        draft = response.json()
                        submitted = self.submit(draft, self.preview(draft))
                        self.assertEqual(submitted.status_code, 202, submitted.text)
                        item_id, job_id = self.items(submitted.json()["generation_run_id"])[0]
                        calls = []
                        def respond(request):
                            calls.append(request)
                            return httpx.Response(status, text="invalid policy upstream 400")
                        with patch("httpx.AsyncClient", side_effect=lambda **kw: client(transport=httpx.MockTransport(respond), **kw)):
                            for _ in range(2):
                                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=get_provider(provider)))
                        self.assertEqual(len(calls), 1)
                        with tenant_session(self.context) as session:
                            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
                            self.assertEqual(reservation.state, expected)
                            self.assertEqual(reservation.outstanding_budget_amount, Decimal("0.30") if expected == "uncertain" else 0)
                            self.assertEqual(session.get(RunItemRecord, item_id).state, "uncertain" if expected == "uncertain" else "failed")
