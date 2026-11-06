"""HTTP-level tests for the real async export flow (Issue #49 Task 9).

``POST /generation/exports`` returns 202 with ``{ package_id, operation_id,
state }`` plus a ``Location`` header; ``GET /generation/exports/{id}``
recovers the package's own state across a refresh.

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``tenant_session``/``TestClient`` with a ``request_tenant_context``
dependency override (see ``tests/test_asset_download_intent.py`` and
``tests/test_v1_generation_routes.py``), not pytest fixtures like
``client``/``auth_headers``/``seed_export_versions``/``db_session`` -- this
file follows the repo's real conventions rather than the plan brief's
pytest-flavored pseudocode.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AssetVersionRecord, JobRecord, new_id
from app.main import app
from app.platform.http_identity import request_tenant_context


class TestExportAsync(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        # Every successful create_export_package() call enqueues a durable
        # "generation.export_package" Job that this suite never drains via
        # a real Worker loop (these are router/contract tests, not Worker
        # tests -- see tests/test_export_worker.py for those). jobs.claim_next
        # claims the globally oldest queued/retry_wait Job across the whole
        # process-shared SQLite DB with no type/workspace filter, so a
        # left-behind "queued" row here would otherwise be silently claimed
        # by an unrelated later test's own runtime.run_once() call. Cancel
        # this Workspace's Jobs on teardown so this suite never leaks one.
        self.addCleanup(self._drain_export_jobs)

    def _drain_export_jobs(self) -> None:
        with platform_session() as session:
            session.query(JobRecord).filter(
                JobRecord.workspace_id == self.ctx.workspace_id,
                JobRecord.state.in_(("queued", "retry_wait")),
            ).update({"state": "cancelled"}, synchronize_session=False)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-{tag}-{new_id('k')}"}

    def _create_version(self, **overrides) -> str:
        with tenant_session(self.ctx) as session:
            asset_id = self._asset_id(self.ctx, session)
            fields = dict(
                id=new_id("av"),
                workspace_id=self.ctx.workspace_id,
                asset_id=asset_id,
                kind="generated",
                aspect_ratio="1:1",
                object_key=f"legacy/{asset_id}.png",
                content_sha256="e" * 64,
                byte_size=100,
                mime_type="image/png",
                origin_kind="generation",
                execution_mode="mock",
            )
            fields.update(overrides)
            version = AssetVersionRecord(**fields)
            session.add(version)
            session.flush()
            return version.id

    # ------------------------------------------------------------------

    def test_export_returns_202_with_operation(self):
        """POST /generation/exports returns 202 with package_id and operation_id."""
        version_id = self._create_version()

        response = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "explicit", "version_ids": [version_id]}},
            headers=self._ik("export-202-1"),
        )
        self.assertEqual(response.status_code, 202, response.text)
        data = response.json()
        self.assertIn("package_id", data)
        self.assertIn("operation_id", data)
        self.assertEqual(data["state"], "queued")
        self.assertIn("Location", response.headers)

    def test_export_replay_returns_same_package(self):
        """Replaying with the same key returns the same package."""
        version_id = self._create_version()
        headers = self._ik("export-replay-1")
        body = {"selector": {"kind": "explicit", "version_ids": [version_id]}}

        r1 = self.client.post("/api/v1/generation/exports", json=body, headers=headers)
        r2 = self.client.post("/api/v1/generation/exports", json=body, headers=headers)

        self.assertEqual(r1.status_code, 202, r1.text)
        self.assertEqual(r2.status_code, 202, r2.text)
        self.assertEqual(r1.json()["package_id"], r2.json()["package_id"])

    def test_export_mixed_modes_rejected(self):
        """Mixed execution modes are rejected."""
        version_id_1 = self._create_version(execution_mode="mock")
        version_id_2 = self._create_version(execution_mode="sandbox")

        response = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "explicit", "version_ids": [version_id_1, version_id_2]}},
            headers=self._ik("export-mixed-1"),
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "mixed_execution_modes")

    def test_export_package_detail_recovers_state(self):
        """GET /generation/exports/{id} returns current state for refresh recovery."""
        version_id = self._create_version()

        r1 = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "explicit", "version_ids": [version_id]}},
            headers=self._ik("export-detail-1"),
        )
        self.assertEqual(r1.status_code, 202, r1.text)
        package_id = r1.json()["package_id"]

        r2 = self.client.get(f"/api/v1/generation/exports/{package_id}")
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["package_id"], package_id)
        self.assertIn("operation_id", r2.json())
        self.assertIn("state", r2.json())

    def test_export_approved_only_preset(self):
        """approved_only preset resolves to only approved versions."""
        version_id = self._create_version()
        with tenant_session(self.ctx) as session:
            from app.generation.review import create_review
            create_review(session, self.ctx, version_id, "approved", "Good", "test")

        response = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "approved_only", "scope": {}}},
            headers=self._ik("export-approved-1"),
        )
        self.assertEqual(response.status_code, 202, response.text)

    def test_export_approved_only_excludes_unreviewed(self):
        """approved_only excludes versions with no (or non-approved) review."""
        self._create_version()  # unreviewed -- must not be selected

        response = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "approved_only", "scope": {}}},
            headers=self._ik("export-approved-empty"),
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "empty_selection")

    def test_export_winner_only_requires_context(self):
        """winner_only without experiment_context_id is rejected."""
        version_id = self._create_version()

        response = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "winner_only", "scope": {}}},
            headers=self._ik("export-winner-no-ctx"),
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "experiment_context_required")

    def test_export_all_successful_preset(self):
        """all_successful resolves to every AssetVersion in the Workspace."""
        self._create_version()
        self._create_version(content_sha256="f" * 64)

        response = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "all_successful", "scope": {}}},
            headers=self._ik("export-all-1"),
        )
        self.assertEqual(response.status_code, 202, response.text)

        package_id = response.json()["package_id"]
        manifest_resp = self.client.get(f"/api/v1/generation/exports/{package_id}/manifest")
        self.assertEqual(manifest_resp.status_code, 200, manifest_resp.text)
        self.assertEqual(manifest_resp.json()["total_versions"], 2)

    def test_export_download_intent_before_ready_returns_409(self):
        version_id = self._create_version()
        create_resp = self.client.post(
            "/api/v1/generation/exports",
            json={"selector": {"kind": "explicit", "version_ids": [version_id]}},
            headers=self._ik("export-notready"),
        )
        package_id = create_resp.json()["package_id"]

        intent_resp = self.client.post(
            f"/api/v1/generation/exports/{package_id}/download-intent",
            headers=self._ik("export-notready-intent"),
        )
        self.assertEqual(intent_resp.status_code, 409, intent_resp.text)
        self.assertEqual(intent_resp.json()["code"], "export_package_not_ready")


if __name__ == "__main__":
    import unittest

    unittest.main()
