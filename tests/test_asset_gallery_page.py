"""``GET /api/v1/generation/assets`` -- typed AssetGalleryPage (Issue #49 Task 3).

Replaces the previous generic ``Page[Any]`` asset listing
(``run_queries.list_assets``, still present but unused by any route as of
this task) with ``app.generation.asset_queries.list_asset_versions``: a
database keyset cursor over ``(created_at DESC, id DESC)``, server-side
filters, and Workspace-scoped facets.

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``TestClient`` with a ``request_tenant_context`` dependency override (see
``tests/test_v1_generation_routes.py`` and ``tests/test_asset_download_intent.py``),
not pytest fixtures like ``db_session``/``tenant_ctx``/``client``/
``auth_headers``/``seed_asset_versions`` -- this file follows the repo's real
conventions rather than the plan brief's pytest-flavored pseudocode.

``effective_review_value``/``latest_qc_state`` here are computed with a
simple "latest row wins" correlated subquery (ordered by ``created_at DESC,
id DESC`` / ``attempt_sequence DESC``) -- a reasonable interim definition
reusing real columns (``QualityReviewRecord.review_value``,
``AssetQCAttemptRecord.state``) that already exist, not the full
supersession-aware ``project_effective_review()`` head-review semantics
spec §0.3 assigns to Task 5. ``stable_dimension_signature``, ``mock_evidence``
and the ``tag`` filter stay stubbed (``None`` / no-op) because no such
columns exist yet. ``contextual_conclusion``/``conclusion`` are wired
(Task 6, spec §0.7) -- see ``tests/test_experiment_conclusions.py`` for
their coverage; this file keeps its existing
``test_gallery_conclusion_requires_experiment_context`` precondition test.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault before app.config is imported anywhere else in
# the process -- see tests/test_v1_library_routes.py for the same note.
from tests.support.library_fixtures import LibraryFixtures

from fastapi.testclient import TestClient

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    QualityReviewRecord,
    new_id,
    utcnow,
)
from app.main import app
from app.platform.http_identity import request_tenant_context


class TestAssetGalleryPage(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_asset_version(
        self,
        ctx,
        session,
        *,
        provider: str = "mock",
        origin_kind: str = "generation",
        execution_mode: str = "mock",
        aspect_ratio: str = "1:1",
        created_at=None,
    ) -> dict:
        product_id = self._product_id(ctx, session)
        plan_id = self._plan_id(ctx, session)
        run = GenerationRunRecord(
            id=new_id("gr"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            plan_id=plan_id,
            provider=provider,
            model="model-v1",
            aspect_ratio=aspect_ratio,
            image_size="1024x1024",
            variants=1,
            use_ref=False,
            status="running",
            total=1,
            done=0,
            failed=0,
            est_cost=Decimal("0"),
            currency="USD",
            revision=1,
        )
        session.add(run)
        session.flush()
        asset = AssetRecord(
            id=new_id("asset"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            run_id=run.id,
            combo_id="combo_1",
            combo_code="combo_1",
            tags_json={"palette": "warm"},
            filename="test.png",
            provider=provider,
            model="model-v1",
            prompt="test prompt",
            fingerprint="fp_" + new_id("h"),
            status="succeeded",
        )
        session.add(asset)
        session.flush()
        version = AssetVersionRecord(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset.id,
            kind="generated",
            aspect_ratio=aspect_ratio,
            object_key=f"legacy/{asset.id}.png",
            content_sha256="c" * 64,
            byte_size=1024,
            mime_type="image/png",
            origin_kind=origin_kind,
            execution_mode=execution_mode,
        )
        if created_at is not None:
            version.created_at = created_at
        session.add(version)
        session.flush()
        return {
            "id": version.id,
            "asset_id": asset.id,
            "product_id": product_id,
            "run_id": run.id,
        }

    def _seed_asset_versions(self, count: int = 3, **overrides) -> list[dict]:
        base = utcnow()
        versions = []
        with tenant_session(self.ctx) as session:
            for i in range(count):
                versions.append(
                    self._make_asset_version(
                        self.ctx,
                        session,
                        created_at=base + timedelta(seconds=i),
                        **overrides,
                    )
                )
        return versions

    def _add_review(self, version: dict, review_value: str) -> None:
        with tenant_session(self.ctx) as session:
            review = QualityReviewRecord(
                id=new_id("qr"),
                workspace_id=self.ctx.workspace_id,
                product_id=version["product_id"],
                asset_id=version["asset_id"],
                asset_version_id=version["id"],
                review_value=review_value,
                source="human",
                actor_id="user_a",
            )
            session.add(review)

    # ------------------------------------------------------------------
    # pagination
    # ------------------------------------------------------------------

    def test_gallery_returns_first_page(self):
        """GET /generation/assets returns paginated AssetVersionSummary items."""
        self._seed_asset_versions(3)

        response = self.client.get(
            "/api/v1/generation/assets", params={"limit": 2},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("items", data)
        self.assertIn("next_cursor", data)
        self.assertIn("facets", data)
        self.assertIn("allowed_actions", data)
        self.assertEqual(len(data["items"]), 2)
        self.assertIsNotNone(data["next_cursor"])

    def test_gallery_keyset_cursor_second_page(self):
        """Second page continues from cursor without repeating items."""
        self._seed_asset_versions(3)

        r1 = self.client.get("/api/v1/generation/assets", params={"limit": 2})
        cursor = r1.json()["next_cursor"]

        r2 = self.client.get(
            "/api/v1/generation/assets", params={"limit": 2, "cursor": cursor},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        page1_ids = {item["id"] for item in r1.json()["items"]}
        page2_ids = {item["id"] for item in r2.json()["items"]}
        self.assertTrue(page1_ids.isdisjoint(page2_ids))
        # Third seeded version surfaces on page 2, none repeated from page 1.
        self.assertEqual(len(page2_ids), 1)

    def test_gallery_does_not_leak_across_workspaces(self):
        """A version created under one Workspace is invisible from another."""
        self._seed_asset_versions(2)
        other_ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: other_ctx
        try:
            resp = self.client.get("/api/v1/generation/assets")
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.ctx

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["items"], [])

    # ------------------------------------------------------------------
    # filters
    # ------------------------------------------------------------------

    def test_gallery_filter_by_provider(self):
        """Provider filter narrows results."""
        self._seed_asset_versions(2, provider="openai")
        self._seed_asset_versions(2, provider="mock")

        response = self.client.get(
            "/api/v1/generation/assets", params={"provider": "openai", "limit": 50},
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item["provider"], "openai")

    def test_gallery_filter_by_effective_review(self):
        """review_value filter returns only versions with that latest review."""
        versions = self._seed_asset_versions(2, provider="mock")
        self._add_review(versions[0], "approved")

        response = self.client.get(
            "/api/v1/generation/assets", params={"review_value": "approved"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], versions[0]["id"])
        for item in items:
            self.assertEqual(item["effective_review_value"], "approved")

    def test_gallery_facets_computed_from_full_dataset(self):
        """Facets reflect the full matching dataset, not just the current page."""
        self._seed_asset_versions(2, provider="openai")
        self._seed_asset_versions(1, provider="mock")

        response = self.client.get(
            "/api/v1/generation/assets", params={"limit": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        facets = response.json()["facets"]
        self.assertIn("providers", facets)
        self.assertIsInstance(facets["providers"], list)
        for f in facets["providers"]:
            self.assertIn("value", f)
            self.assertIn("count", f)
        counts = {f["value"]: f["count"] for f in facets["providers"]}
        self.assertEqual(counts.get("openai"), 2)
        self.assertEqual(counts.get("mock"), 1)

    def test_gallery_invalid_cursor_returns_400(self):
        """An invalid cursor returns 400 invalid_cursor."""
        response = self.client.get(
            "/api/v1/generation/assets", params={"cursor": "bogus-cursor"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "invalid_cursor")

    def test_gallery_conclusion_requires_experiment_context(self):
        """conclusion filter without experiment_context returns 422."""
        self._seed_asset_versions(1)

        response = self.client.get(
            "/api/v1/generation/assets", params={"conclusion": "winner"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "experiment_context_required")
