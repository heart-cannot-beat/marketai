"""HTTP-level tests for the Generation v1 sub-router (Issue #27 Task 3).

Exercises the real FastAPI app via ``TestClient`` with ``request_tenant_context``
overridden to a fixed ``TenantContext``, the same pattern
``tests/test_v1_library_routes.py`` uses, so requests hit the actual route
bodies, ``tenant_session`` and the RFC 9457 problem-handler wiring registered
in ``app/main.py`` rather than calling the Generation domain modules
directly.

25 routes exist under ``/api/v1/generation``; this suite walks the primary
happy path end to end (draft -> preview -> submit -> run/run-item reads,
recovery-preview -> retry -> re-queue, review, regeneration-draft,
export -> manifest -> download-intent) plus the edge cases specific to this
task: missing Idempotency-Key, stale revision (If-Match), and the
``quality_feedback`` regeneration QC-attempt gate.

The asset-version download-intent route (Issue #49 Task 2) has its own
dedicated coverage in ``tests/test_asset_download_intent.py`` -- this file
keeps only a minimal smoke assertion that a version with no committed blob
ref fails closed with the stable ``asset_blob_unavailable`` code, so this
suite's route inventory still exercises every mounted path once.
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault before app.config is imported anywhere else in
# the process -- see tests/test_v1_library_routes.py for the same note.
from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, new_id
from app.generation.models import AssetQCAttemptRecord
from app.main import app
from app.platform.http_identity import request_tenant_context
from tests.support.storage_limit import set_storage_headroom


class TestGenerationV1Routes(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-{tag}-{new_id('k')}"}

    @contextmanager
    def _as_other_workspace(self):
        """Temporarily switch ``request_tenant_context`` to a second, freshly
        provisioned Workspace -- for negative tenant-isolation assertions
        (a resource created under ``self.ctx`` must 404, not leak, when
        looked up from a different Workspace)."""
        other_ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: other_ctx
        try:
            yield other_ctx
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.ctx

    def _create_draft(self, **overrides) -> dict:
        product_id = overrides.pop("product_id", None) or self._product_id(self.ctx)
        body = {
            "product_id": product_id,
            "provider": "mock",
            "model": "mock-v1",
            "execution_mode": "mock",
            "combination_strategy": {"kind": "full", "planner_version": "v1"},
        }
        body.update(overrides)
        resp = self.client.post(
            "/api/v1/generation/drafts", json=body, headers=self._ik("create-draft"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _create_preview(self, draft: dict) -> dict:
        resp = self.client.post(
            f"/api/v1/generation/drafts/{draft['id']}/previews",
            headers={**self._ik("preview"), "If-Match": f'"{draft["revision"]}"'},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _submit_run(self, draft: dict, preview: dict) -> dict:
        resp = self.client.post(
            "/api/v1/generation/runs",
            json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]},
            headers={**self._ik("submit"), "If-Match": f'"{draft["revision"]}"'},
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        return resp.json()

    def _create_asset_version(self) -> str:
        with tenant_session(self.ctx) as session:
            av = AssetVersionRecord(
                id=new_id("av"),
                workspace_id=self.ctx.workspace_id,
                asset_id=self._asset_id(self.ctx, session),
                kind="generated",
                aspect_ratio="1:1",
                object_key="test/obj.png",
                content_sha256="a" * 64,
                byte_size=1024,
                mime_type="image/png",
                origin_kind="generation",
                execution_mode="mock",
            )
            session.add(av)
            session.flush()
            return av.id

    # ------------------------------------------------------------------
    # Draft -> Preview -> Run happy path
    # ------------------------------------------------------------------

    def test_create_draft_requires_idempotency_key(self):
        product_id = self._product_id(self.ctx)
        resp = self.client.post(
            "/api/v1/generation/drafts",
            json={"product_id": product_id, "provider": "mock", "model": "mock-v1",
                  "execution_mode": "mock", "combination_strategy": {}},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_create_and_get_draft_roundtrip_with_etag(self):
        draft = self._create_draft()
        self.assertEqual(draft["state"], "editing")
        self.assertEqual(draft["revision"], 1)
        allowed = {a["action"]: a["allowed"] for a in draft["allowed_actions"]}
        self.assertTrue(allowed["update"])
        self.assertTrue(allowed["preview"])

        resp = self.client.get(f"/api/v1/generation/drafts/{draft['id']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["ETag"], '"1"')
        self.assertEqual(resp.json()["id"], draft["id"])

    def test_get_draft_unknown_returns_404(self):
        resp = self.client.get("/api/v1/generation/drafts/gd_missing")
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "draft_not_found")

    def test_update_draft_requires_if_match_and_bumps_revision(self):
        draft = self._create_draft()

        no_if_match = self.client.patch(
            f"/api/v1/generation/drafts/{draft['id']}",
            json={"selectors": {"a": 1}}, headers=self._ik("update-no-match"),
        )
        self.assertEqual(no_if_match.status_code, 428, no_if_match.text)

        resp = self.client.patch(
            f"/api/v1/generation/drafts/{draft['id']}",
            json={"selectors": {"a": 1}},
            headers={**self._ik("update"), "If-Match": f'"{draft["revision"]}"'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        updated = resp.json()
        self.assertEqual(updated["revision"], draft["revision"] + 1)
        self.assertEqual(updated["selectors"], {"a": 1})

        stale = self.client.patch(
            f"/api/v1/generation/drafts/{draft['id']}",
            json={"selectors": {"b": 2}},
            headers={**self._ik("update-stale"), "If-Match": f'"{draft["revision"]}"'},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "stale_revision")

    def test_preview_and_submit_run_then_read_run_and_items(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        self.assertEqual(preview["draft_id"], draft["id"])
        self.assertTrue(any(a["action"] == "submit" and a["allowed"] for a in preview["allowed_actions"]))

        submit_result = self._submit_run(draft, preview)
        self.assertIn("operation_id", submit_result)
        run_id = submit_result["generation_run_id"]

        get_resp = self.client.get(f"/api/v1/generation/runs/{run_id}")
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        self.assertIn("ETag", get_resp.headers)
        run_detail = get_resp.json()
        self.assertEqual(run_detail["id"], run_id)
        self.assertGreaterEqual(run_detail["total"], 1)
        self.assertEqual(len(run_detail["commands"]), 1)
        self.assertEqual(run_detail["commands"][0]["command_kind"], "submit")

        list_resp = self.client.get("/api/v1/generation/runs")
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        self.assertIn(run_id, [r["id"] for r in list_resp.json()["items"]])

        items_resp = self.client.get(f"/api/v1/generation/runs/{run_id}/items")
        self.assertEqual(items_resp.status_code, 200, items_resp.text)
        items = items_resp.json()["items"]
        self.assertGreaterEqual(len(items), 1)

        item_id = items[0]["id"]
        item_detail_resp = self.client.get(f"/api/v1/generation/runs/{run_id}/items/{item_id}")
        self.assertEqual(item_detail_resp.status_code, 200, item_detail_resp.text)
        self.assertEqual(item_detail_resp.json()["id"], item_id)

    def test_get_preview_unknown_returns_404(self):
        resp = self.client.get("/api/v1/generation/previews/rpv_missing")
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "preview_not_found")

    # ------------------------------------------------------------------
    # Run commands
    # ------------------------------------------------------------------

    def test_cancel_run_command(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        submit_result = self._submit_run(draft, preview)
        run_id = submit_result["generation_run_id"]

        run_detail = self.client.get(f"/api/v1/generation/runs/{run_id}").json()

        no_if_match = self.client.post(
            f"/api/v1/generation/runs/{run_id}/cancel", headers=self._ik("cancel-no-match"),
        )
        self.assertEqual(no_if_match.status_code, 428, no_if_match.text)

        resp = self.client.post(
            f"/api/v1/generation/runs/{run_id}/cancel",
            headers={**self._ik("cancel"), "If-Match": f'"{run_detail["revision"]}"'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["run_id"], run_id)

        after = self.client.get(f"/api/v1/generation/runs/{run_id}").json()
        self.assertEqual(after["status"], "cancelled")
        self.assertEqual(after["revision"], run_detail["revision"] + 1)

        # A second cancel at the (now stale) original revision conflicts.
        conflict = self.client.post(
            f"/api/v1/generation/runs/{run_id}/cancel",
            headers={**self._ik("cancel-again"), "If-Match": f'"{run_detail["revision"]}"'},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_get_run_unknown_returns_404(self):
        resp = self.client.get("/api/v1/generation/runs/gr_missing")
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "generation_run_not_found")

    # ------------------------------------------------------------------
    # Recovery previews
    # ------------------------------------------------------------------

    def test_recovery_preview_create_and_get(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        submit_result = self._submit_run(draft, preview)
        run_id = submit_result["generation_run_id"]

        # Fail an item
        from app.adapters.persistence.database import tenant_session
        from app.generation.models import RunItemRecord
        from sqlalchemy import update
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            items = s.query(RunItemRecord).filter_by(
                generation_run_id=run_id).all()
            s.execute(
                update(RunItemRecord).where(RunItemRecord.id == items[0].id)
                .values(state="failed", revision=items[0].revision + 1)
            )

        run_detail = self.client.get(f"/api/v1/generation/runs/{run_id}").json()

        # POST recovery preview
        resp = self.client.post(
            f"/api/v1/generation/runs/{run_id}/recovery-previews",
            json={
                "command": "retry_failed_items",
                "selection": {"kind": "all_eligible_failed"},
            },
            headers={
                **self._ik("recovery-1"),
                "If-Match": f'"{run_detail["revision"]}"',
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        rp = resp.json()
        self.assertIn("id", rp)
        self.assertIn("preview_hash", rp)
        self.assertEqual(rp["purpose"], "run_recovery")
        self.assertIn("Location", resp.headers)

        # GET recovery preview via shared endpoint
        get_resp = self.client.get(
            f"/api/v1/generation/previews/{rp['id']}"
        )
        self.assertEqual(get_resp.status_code, 200, get_resp.text)
        got = get_resp.json()
        self.assertEqual(got["purpose"], "run_recovery")
        self.assertEqual(got["id"], rp["id"])

    def test_initial_preview_get_still_returns_initial_purpose(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        resp = self.client.get(f"/api/v1/generation/previews/{preview['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["purpose"], "initial_generation")

    def test_recovery_retry_end_to_end(self):
        """Create draft -> preview -> submit -> fail item -> recovery preview ->
        retry -> item re-queued."""
        draft = self._create_draft()
        preview = self._create_preview(draft)
        submit_result = self._submit_run(draft, preview)
        run_id = submit_result["generation_run_id"]

        # Fail an item
        from app.adapters.persistence.database import tenant_session
        from app.generation.models import RunItemRecord
        from sqlalchemy import update
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            items = s.query(RunItemRecord).filter_by(
                generation_run_id=run_id).all()
            s.execute(
                update(RunItemRecord).where(RunItemRecord.id == items[0].id)
                .values(state="failed", error_summary="test",
                        revision=items[0].revision + 1)
            )

        run_detail = self.client.get(f"/api/v1/generation/runs/{run_id}").json()

        # Create recovery preview
        rp_resp = self.client.post(
            f"/api/v1/generation/runs/{run_id}/recovery-previews",
            json={
                "command": "retry_failed_items",
                "selection": {"kind": "all_eligible_failed"},
            },
            headers={
                **self._ik("e2e-rp"),
                "If-Match": f'"{run_detail["revision"]}"',
            },
        )
        self.assertEqual(rp_resp.status_code, 201)
        rp = rp_resp.json()

        # Consume via retry-failed-items
        run_detail2 = self.client.get(f"/api/v1/generation/runs/{run_id}").json()
        retry_resp = self.client.post(
            f"/api/v1/generation/runs/{run_id}/retry-failed-items",
            json={"preview_id": rp["id"], "preview_hash": rp["preview_hash"]},
            headers={
                **self._ik("e2e-retry"),
                "If-Match": f'"{run_detail2["revision"]}"',
            },
        )
        self.assertEqual(retry_resp.status_code, 200, retry_resp.text)

        # Verify item is re-queued
        with tenant_session(ctx) as s:
            items = s.query(RunItemRecord).filter_by(
                generation_run_id=run_id).all()
            target_ids = set(rp["targets"])
            for item in items:
                if item.id in target_ids:
                    self.assertEqual(item.state, "queued")

    def test_storage_at_the_limit_refuses_a_run_but_not_its_replay(self):
        """#158: a Workspace that stores its storage.bytes.max admits no new
        Run; one byte below it does, and a replay is never re-checked."""
        draft = self._create_draft()
        preview = self._create_preview(draft)
        body = {"preview_id": preview["id"], "preview_hash": preview["preview_hash"]}
        headers = {**self._ik("submit-full"), "If-Match": f'"{draft["revision"]}"'}

        set_storage_headroom(self.ctx, 0)
        refused = self.client.post("/api/v1/generation/runs", json=body, headers=headers)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["code"], "storage_limit_reached")
        self.assertEqual(refused.json()["params"]["limit_bytes"], refused.json()["params"]["stored_bytes"])
        self.assertIn("limit_gib", refused.json()["params"])
        self.assertEqual(self.client.get("/api/v1/generation/runs").json()["items"], [])

        with self._as_other_workspace() as other:  # its own limit, not this one's
            other_draft = self._create_draft(product_id=self._product_id(other))
            self._submit_run(other_draft, self._create_preview(other_draft))

        set_storage_headroom(self.ctx, 1)
        admitted = self.client.post("/api/v1/generation/runs", json=body, headers=headers)
        self.assertEqual(admitted.status_code, 202, admitted.text)

        set_storage_headroom(self.ctx, 0)
        replay = self.client.post("/api/v1/generation/runs", json=body, headers=headers)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["operation_id"], admitted.json()["operation_id"])

    def test_storage_at_the_limit_refuses_a_retry_of_failed_items(self):
        draft = self._create_draft()
        run_id = self._submit_run(draft, self._create_preview(draft))["generation_run_id"]
        from app.generation.models import RunItemRecord
        from sqlalchemy import update
        with tenant_session(self.ctx) as s:
            item = s.query(RunItemRecord).filter_by(generation_run_id=run_id).first()
            s.execute(update(RunItemRecord).where(RunItemRecord.id == item.id)
                      .values(state="failed", error_summary="test", revision=item.revision + 1))
        revision = self.client.get(f"/api/v1/generation/runs/{run_id}").json()["revision"]
        rp = self.client.post(
            f"/api/v1/generation/runs/{run_id}/recovery-previews",
            json={"command": "retry_failed_items", "selection": {"kind": "all_eligible_failed"}},
            headers={**self._ik("full-rp"), "If-Match": f'"{revision}"'},
        ).json()
        revision = self.client.get(f"/api/v1/generation/runs/{run_id}").json()["revision"]
        retry = {"preview_id": rp["id"], "preview_hash": rp["preview_hash"]}
        headers = {**self._ik("full-retry"), "If-Match": f'"{revision}"'}

        set_storage_headroom(self.ctx, 0)
        refused = self.client.post(f"/api/v1/generation/runs/{run_id}/retry-failed-items", json=retry, headers=headers)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["code"], "storage_limit_reached")
        with tenant_session(self.ctx) as s:
            self.assertEqual(s.get(RunItemRecord, item.id).state, "failed")

        set_storage_headroom(self.ctx, 1)
        admitted = self.client.post(f"/api/v1/generation/runs/{run_id}/retry-failed-items", json=retry, headers=headers)
        self.assertEqual(admitted.status_code, 200, admitted.text)
        with tenant_session(self.ctx) as s:
            self.assertEqual(s.get(RunItemRecord, item.id).state, "queued")

    # ------------------------------------------------------------------
    # Assets / AssetVersions / Reviews / Regeneration
    # ------------------------------------------------------------------

    def test_list_assets_and_get_asset_version_detail(self):
        version_id = self._create_asset_version()

        list_resp = self.client.get("/api/v1/generation/assets")
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        self.assertIn(version_id, [a["id"] for a in list_resp.json()["items"]])

        detail_resp = self.client.get(f"/api/v1/generation/asset-versions/{version_id}")
        self.assertEqual(detail_resp.status_code, 200, detail_resp.text)
        detail = detail_resp.json()
        self.assertEqual(detail["id"], version_id)
        self.assertEqual(detail["reviews"], [])
        allowed = {a["action"]: a["allowed"] for a in detail["allowed_actions"]}
        self.assertTrue(allowed["download"])  # object_key is set

    def test_get_asset_version_unknown_returns_404(self):
        resp = self.client.get("/api/v1/generation/asset-versions/av_missing")
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.headers["content-type"], "application/problem+json")
        body = resp.json()
        self.assertEqual(body["code"], "asset_version_not_found")
        self.assertEqual(body["status"], 404)
        self.assertTrue(body["trace_id"])
        self.assertFalse(body["retryable"])

    def test_create_review_then_shows_on_asset_version_detail(self):
        version_id = self._create_asset_version()
        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "looks good"},
            headers=self._ik("review"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        review = resp.json()
        self.assertEqual(review["asset_version_id"], version_id)
        self.assertEqual(review["review_value"], "approved")

        detail = self.client.get(f"/api/v1/generation/asset-versions/{version_id}").json()
        self.assertEqual(len(detail["reviews"]), 1)
        self.assertEqual(detail["reviews"][0]["review_value"], "approved")

    def test_create_regeneration_draft_same_recipe(self):
        version_id = self._create_asset_version()
        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json={"intent": "same_recipe"},
            headers=self._ik("regen"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        draft = resp.json()
        self.assertEqual(draft["regeneration_intent"], "same_recipe")
        self.assertEqual(draft["source_asset_version_id"], version_id)

    def test_regeneration_draft_quality_feedback_requires_matching_qc_attempt(self):
        version_id = self._create_asset_version()

        missing_id = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json={"intent": "quality_feedback"},
            headers=self._ik("regen-missing-qc"),
        )
        self.assertEqual(missing_id.status_code, 422, missing_id.text)

        mismatched = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json={"intent": "quality_feedback", "qc_attempt_id": "qc_does_not_exist"},
            headers=self._ik("regen-mismatch-qc"),
        )
        self.assertEqual(mismatched.status_code, 409, mismatched.text)
        self.assertEqual(mismatched.json()["code"], "qc_attempt_mismatch")

        with tenant_session(self.ctx) as session:
            qc = AssetQCAttemptRecord(
                id=new_id("qca"),
                workspace_id=self.ctx.workspace_id,
                asset_version_id=version_id,
                run_item_id=self._create_run_item(self.ctx, session),
                qc_policy_version="v1",
                rule_set_version="v1",
                input_hash="h" * 64,
                execution_mode="mock",
                state="failed_nonblocking",
                attempt_sequence=1,
            )
            session.add(qc)
            session.flush()
            qc_id = qc.id

        ok = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json={"intent": "quality_feedback", "qc_attempt_id": qc_id},
            headers=self._ik("regen-ok-qc"),
        )
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(ok.json()["regeneration_intent"], "quality_feedback")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def test_export_create_manifest_and_download_intent_not_ready(self):
        version_id = self._create_asset_version()

        create_resp = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "explicit", "version_ids": [version_id]}},
            headers=self._ik("export"),
        )
        self.assertEqual(create_resp.status_code, 202, create_resp.text)
        self.assertIn("Location", create_resp.headers)
        queued = create_resp.json()
        self.assertIn("package_id", queued)
        self.assertIn("operation_id", queued)
        self.assertEqual(queued["state"], "queued")
        package_id = queued["package_id"]

        detail_resp = self.client.get(f"/api/v1/generation/exports/{package_id}")
        self.assertEqual(detail_resp.status_code, 200, detail_resp.text)
        detail = detail_resp.json()
        self.assertEqual(detail["package_id"], package_id)
        self.assertEqual(detail["operation_id"], queued["operation_id"])
        self.assertEqual(detail["execution_mode"], "mock")
        self.assertEqual(detail["state"], "pending")

        manifest_resp = self.client.get(f"/api/v1/generation/exports/{package_id}/manifest")
        self.assertEqual(manifest_resp.status_code, 200, manifest_resp.text)
        manifest = manifest_resp.json()
        self.assertEqual(manifest["total_versions"], 1)
        self.assertEqual(manifest["entries"][0]["asset_version_id"], version_id)

        # Package is still "pending" (Worker hasn't built the ZIP): the
        # download-intent must refuse rather than issue a capability against
        # a blob that doesn't exist yet.
        intent_resp = self.client.post(
            f"/api/v1/generation/exports/{package_id}/download-intent",
            headers=self._ik("export-intent"),
        )
        self.assertEqual(intent_resp.status_code, 409, intent_resp.text)
        self.assertEqual(intent_resp.json()["code"], "export_package_not_ready")

    # ------------------------------------------------------------------
    # Capabilities endpoint
    # ------------------------------------------------------------------

    def test_capabilities_returns_200(self):
        resp = self.client.get("/api/v1/generation/capabilities")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("providers", body)
        self.assertIn("models", body)
        self.assertIn("modes", body)
        self.assertIn("format_limits", body)
        self.assertIn("credential_readiness", body)
        self.assertIn("mock", body["modes"])

    def test_capabilities_etag_caching(self):
        resp1 = self.client.get("/api/v1/generation/capabilities")
        self.assertEqual(resp1.status_code, 200)
        etag = resp1.headers.get("etag")
        self.assertIsNotNone(etag)
        resp2 = self.client.get(
            "/api/v1/generation/capabilities",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(resp2.status_code, 304)

    # ------------------------------------------------------------------
    # Asset-version download-intent (Issue #49 Task 2) -- smoke only; full
    # coverage lives in tests/test_asset_download_intent.py.
    # ------------------------------------------------------------------

    def test_asset_version_download_intent_requires_committed_blob_ref(self):
        # _create_asset_version() above sets only the legacy object_key,
        # not committed_object_key/committed_storage_version_id -- exactly
        # the "not yet committed" case resolve_asset_blob_ref fails closed on.
        version_id = self._create_asset_version()
        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/download-intent",
            headers=self._ik("dl-intent"),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "asset_blob_unavailable")

    # ------------------------------------------------------------------
    # Tenant isolation
    # ------------------------------------------------------------------

    def test_draft_is_not_visible_from_another_workspace(self):
        draft = self._create_draft()

        with self._as_other_workspace():
            resp = self.client.get(f"/api/v1/generation/drafts/{draft['id']}")
            self.assertEqual(resp.status_code, 404, resp.text)

    def test_preview_is_not_visible_from_another_workspace(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)

        with self._as_other_workspace():
            resp = self.client.get(f"/api/v1/generation/previews/{preview['id']}")
            self.assertEqual(resp.status_code, 404, resp.text)
            self.assertEqual(resp.json()["code"], "preview_not_found")

    def test_run_detail_is_not_visible_from_another_workspace(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        submit_result = self._submit_run(draft, preview)
        run_id = submit_result["generation_run_id"]

        with self._as_other_workspace():
            resp = self.client.get(f"/api/v1/generation/runs/{run_id}")
            self.assertEqual(resp.status_code, 404, resp.text)
            self.assertEqual(resp.json()["code"], "generation_run_not_found")

    def test_run_item_detail_is_not_visible_from_another_workspace(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        submit_result = self._submit_run(draft, preview)
        run_id = submit_result["generation_run_id"]
        items = self.client.get(f"/api/v1/generation/runs/{run_id}/items").json()["items"]
        item_id = items[0]["id"]

        with self._as_other_workspace():
            resp = self.client.get(f"/api/v1/generation/runs/{run_id}/items/{item_id}")
            self.assertEqual(resp.status_code, 404, resp.text)
            self.assertEqual(resp.json()["code"], "generation_run_not_found")

    def test_asset_version_detail_is_not_visible_from_another_workspace(self):
        version_id = self._create_asset_version()

        with self._as_other_workspace():
            resp = self.client.get(f"/api/v1/generation/asset-versions/{version_id}")
            self.assertEqual(resp.status_code, 404, resp.text)
            self.assertEqual(resp.json()["code"], "asset_version_not_found")

    def test_tenant_isolation_recovery_preview(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)
        submit_result = self._submit_run(draft, preview)
        run_id = submit_result["generation_run_id"]

        with self._as_other_workspace():
            resp = self.client.post(
                f"/api/v1/generation/runs/{run_id}/recovery-previews",
                json={
                    "command": "retry_failed_items",
                    "selection": {"kind": "all_eligible_failed"},
                },
                headers={**self._ik("tenant-rp"), "If-Match": '"1"'},
            )
            self.assertIn(resp.status_code, (404, 403))
