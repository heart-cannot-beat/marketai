"""Asset blob resolution and download-intent route tests (Issue #49 Task 2).

Two test classes:

* ``TestResolveAssetBlobRef`` -- direct, non-HTTP tests of
  ``app.generation.asset_queries.resolve_asset_blob_ref`` against a real
  tenant-scoped session.
* ``TestAssetVersionDownloadIntentRoute`` -- HTTP-level tests of
  ``POST /api/v1/generation/asset-versions/{version_id}/download-intent``
  via the real FastAPI app + ``TestClient``.

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``tenant_session``/``TestClient`` with a ``request_tenant_context`` dependency
override (see ``tests/test_v1_generation_routes.py`` and Task 1's
``tests/test_committed_blob_refs.py``), not pytest fixtures like
``db_session``/``tenant_ctx``/``make_asset_version``/``auth_headers`` -- this
file follows the repo's real conventions rather than the plan brief's
pytest-flavored pseudocode.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault before app.config is imported anywhere else in
# the process -- see tests/test_v1_library_routes.py for the same note.
from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, new_id
from app.generation.asset_queries import AssetBlobError, resolve_asset_blob_ref
from app.main import app
from app.platform.blob_store import CommittedBlobRef
from app.platform.http_identity import request_tenant_context


class TestResolveAssetBlobRef(LibraryFixtures):
    """``app.generation.asset_queries.resolve_asset_blob_ref``."""

    def _make_version(self, ctx, session, **overrides) -> AssetVersionRecord:
        asset_id = self._asset_id(ctx, session)
        fields = dict(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            language="en",
            object_key=f"legacy/{asset_id}.png",
            content_sha256="c" * 64,
            byte_size=100,
            mime_type="image/png",
        )
        fields.update(overrides)
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        return version

    def test_resolve_asset_blob_ref_returns_committed_ref(self):
        """Returns CommittedBlobRef when the version has committed refs."""
        ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(
                ctx,
                session,
                committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
                committed_storage_version_id="sv-1",
                content_sha256="a" * 64,
                byte_size=1024,
                mime_type="image/png",
            )
            version_id = version.id
            session.flush()

            ref = resolve_asset_blob_ref(session, ctx, version_id)
            self.assertIsInstance(ref, CommittedBlobRef)
            self.assertEqual(
                ref.committed_key, "committed/ws1/assets/2026/09/08/v1.png"
            )
            self.assertEqual(ref.storage_version_id, "sv-1")
            self.assertEqual(ref.size_bytes, 1024)
            self.assertEqual(ref.checksum_sha256_b64, "a" * 64)
            self.assertEqual(ref.content_type_verified, "image/png")

    def test_resolve_asset_blob_ref_raises_for_missing_committed_ref(self):
        """Raises asset_blob_unavailable when committed_object_key is None."""
        ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(ctx, session, committed_object_key=None)
            version_id = version.id
            session.flush()

            with self.assertRaises(AssetBlobError) as caught:
                resolve_asset_blob_ref(session, ctx, version_id)
            self.assertEqual(caught.exception.code, "asset_blob_unavailable")

    def test_resolve_asset_blob_ref_raises_for_unknown_version(self):
        """Raises asset_blob_unavailable when the version id does not exist."""
        ctx = self._make_context()
        with tenant_session(ctx) as session:
            with self.assertRaises(AssetBlobError) as caught:
                resolve_asset_blob_ref(session, ctx, "does-not-exist")
            self.assertEqual(caught.exception.code, "asset_blob_unavailable")

    def test_resolve_asset_blob_ref_raises_for_wrong_workspace(self):
        """Cross-workspace version lookup fails closed, not a leak."""
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(
                ctx,
                session,
                committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
                committed_storage_version_id="sv-1",
            )
            version_id = version.id
            session.flush()

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(AssetBlobError) as caught:
                resolve_asset_blob_ref(other_session, other_ctx, version_id)
            self.assertEqual(caught.exception.code, "asset_blob_unavailable")


class TestAssetVersionDownloadIntentRoute(LibraryFixtures):
    """``POST /generation/asset-versions/{version_id}/download-intent``."""

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-{tag}-{new_id('k')}"}

    def _create_committed_asset_version(self, **overrides) -> str:
        with tenant_session(self.ctx) as session:
            asset_id = self._asset_id(self.ctx, session)
            fields = dict(
                id=new_id("ver"),
                workspace_id=self.ctx.workspace_id,
                asset_id=asset_id,
                kind="generated",
                aspect_ratio="1:1",
                object_key=f"legacy/{asset_id}.png",
                content_sha256="d" * 64,
                byte_size=1024,
                mime_type="image/png",
                origin_kind="generation",
                execution_mode="mock",
                committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
                committed_storage_version_id="sv-1",
            )
            fields.update(overrides)
            version = AssetVersionRecord(**fields)
            session.add(version)
            session.flush()
            return version.id

    def test_download_intent_route_returns_201(self):
        """POST returns 201 with intent_id/expires_at/download_grant."""
        version_id = self._create_committed_asset_version()

        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/download-intent",
            headers=self._ik("dl-1"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertIn("intent_id", data)
        self.assertIn("expires_at", data)
        self.assertIn("download_grant", data)
        self.assertIn("url", data["download_grant"])
        self.assertIn("content_disposition", data["download_grant"])
        self.assertIn("kind", data["download_grant"])

    def test_download_intent_replay_returns_same_intent(self):
        """Replaying the same Idempotency-Key returns the same intent id."""
        version_id = self._create_committed_asset_version()
        headers = self._ik("dl-replay")

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/download-intent",
            headers=headers,
        )
        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/download-intent",
            headers=headers,
        )

        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(r1.json()["intent_id"], r2.json()["intent_id"])
        # A replay still mints a fresh signed URL against the same
        # committed ref -- the intent id is stable, the URL need not be.
        self.assertIn("url", r2.json()["download_grant"])

    def test_download_intent_422_for_uncommitted_blob(self):
        """Returns 422 asset_blob_unavailable when there's no committed ref."""
        version_id = self._create_committed_asset_version(
            committed_object_key=None, committed_storage_version_id=None,
        )

        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/download-intent",
            headers=self._ik("dl-uncommitted"),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "asset_blob_unavailable")

    def test_download_intent_requires_idempotency_key(self):
        version_id = self._create_committed_asset_version()

        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/download-intent",
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_download_intent_422_for_unknown_version(self):
        resp = self.client.post(
            "/api/v1/generation/asset-versions/does-not-exist/download-intent",
            headers=self._ik("dl-missing"),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "asset_blob_unavailable")

    def test_download_intent_not_visible_from_another_workspace(self):
        """A version created under ``self.ctx`` fails closed from another Workspace."""
        version_id = self._create_committed_asset_version()
        other_ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: other_ctx
        try:
            resp = self.client.post(
                f"/api/v1/generation/asset-versions/{version_id}/download-intent",
                headers=self._ik("dl-other-ws"),
            )
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.ctx

        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "asset_blob_unavailable")
