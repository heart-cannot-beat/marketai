"""``GET /api/v1/generation/asset-versions/{id}`` -- AssetVersionDetail
projection (Issue #49 Task 4, spec §0.2).

Extends ``app.generation.run_queries.get_asset_version_detail`` (the
pre-#49 detail projection, kept in ``run_queries`` unchanged but no longer
called from any route) with
``app.generation.asset_queries.get_asset_version_detail``: the full
provenance shape -- seed, prompt, append-only ``reviews[]``, QC attempt
history, and parent/children lineage -- Browser Task 8's ``LightboxModal``
consumes.

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``TestClient`` with a ``request_tenant_context`` dependency override (see
``tests/test_asset_gallery_page.py`` and ``tests/test_asset_download_intent.py``),
not pytest fixtures like ``db_session``/``client``/``auth_headers``/
``make_asset_version_with_provenance``/``make_regenerated_version`` -- this
file follows the repo's real conventions rather than the plan brief's
pytest-flavored pseudocode.

``review_head_id``/``effective_review_value`` use the same interim "latest
row wins" definition Task 3's ``_version_summary`` uses (not yet Task 5's
supersession-aware head-review rule). ``contextual_conclusions`` is
``app.generation.experiment_conclusions.project_contextual_conclusions``
(Task 6, spec §0.7) -- ``[]`` here because none of this file's fixtures
attach a contextual verdict; see ``tests/test_experiment_conclusions.py``
for the populated case.
"""
from __future__ import annotations

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
)
from app.generation.models import AssetQCAttemptRecord, RunItemRecord
from app.main import app
from app.platform.http_identity import request_tenant_context


class TestAssetVersionDetail(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_run(self, ctx, session, product_id: str, plan_id: str) -> str:
        run = GenerationRunRecord(
            id=new_id("gr"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            plan_id=plan_id,
            provider="mock",
            model="mock-v1",
            aspect_ratio="1:1",
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
        return run.id

    def _make_asset_version(
        self, ctx, session, *, run_id: str | None = None, product_id: str | None = None,
        **overrides,
    ) -> AssetVersionRecord:
        product_id = product_id or self._product_id(ctx, session)
        plan_id = self._plan_id(ctx, session)
        run_id = run_id or self._make_run(ctx, session, product_id, plan_id)
        asset = AssetRecord(
            id=new_id("asset"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            run_id=run_id,
            combo_id="combo_1",
            combo_code="combo_1",
            tags_json={"palette": "warm"},
            filename="test.png",
            provider="mock",
            model="mock-v1",
            prompt="a warm-toned product hero shot",
            fingerprint="fp_" + new_id("h"),
            status="succeeded",
        )
        session.add(asset)
        session.flush()
        fields = dict(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset.id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset.id}.png",
            content_sha256="c" * 64,
            byte_size=1024,
            mime_type="image/png",
            origin_kind="generation",
            execution_mode="mock",
            requested_seed="42",
            effective_seed="42",
        )
        fields.update(overrides)
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        return version

    def _make_run_item(
        self, ctx, session, *, generation_run_id: str, intent_kind: str,
        source_asset_version_id: str | None = None,
    ) -> RunItemRecord:
        run_item = RunItemRecord(
            id=new_id("ri"),
            workspace_id=ctx.workspace_id,
            generation_run_id=generation_run_id,
            ordinal=0,
            combination_identity_json={},
            combination_fingerprint="cf_" + new_id("h"),
            variant_index=0,
            semantic_fingerprint="sf_" + new_id("h"),
            intent_kind=intent_kind,
            source_asset_version_id=source_asset_version_id,
            execution_mode="mock",
            state="succeeded",
        )
        session.add(run_item)
        session.flush()
        return run_item

    def _make_asset_version_with_provenance(self) -> str:
        """A version with reviews and a QC attempt attached."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            product_id = self._product_id(self.ctx, session)
            asset = session.get(AssetRecord, version.asset_id)

            review = QualityReviewRecord(
                id=new_id("qr"),
                workspace_id=self.ctx.workspace_id,
                product_id=asset.product_id,
                asset_id=asset.id,
                asset_version_id=version_id,
                review_value="approved",
                source="human",
                actor_id="user_a",
            )
            session.add(review)

            qc = AssetQCAttemptRecord(
                id=new_id("qc"),
                workspace_id=self.ctx.workspace_id,
                asset_version_id=version_id,
                run_item_id=self._make_run_item(
                    self.ctx, session, generation_run_id=asset.run_id,
                    intent_kind="new_generation",
                ).id,
                feedback_round=0,
                qc_policy_version="v1",
                rule_set_version="v1",
                input_hash="h" * 64,
                execution_mode="mock",
                state="passed",
                attempt_sequence=1,
            )
            session.add(qc)
            session.flush()
        return version_id

    def _make_regenerated_version(self) -> tuple[str, str]:
        """A parent AssetVersion and a child regenerated from it."""
        with tenant_session(self.ctx) as session:
            parent = self._make_asset_version(self.ctx, session)
            parent_id = parent.id
            parent_asset = session.get(AssetRecord, parent.asset_id)

            child_run_item = self._make_run_item(
                self.ctx, session, generation_run_id=parent_asset.run_id,
                intent_kind="new_seed", source_asset_version_id=parent_id,
            )
            child = self._make_asset_version(
                self.ctx, session,
                run_id=parent_asset.run_id,
                product_id=parent_asset.product_id,
                parent_version_id=parent_id,
                producing_run_item_id=child_run_item.id,
            )
            child_id = child.id
        return parent_id, child_id

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_detail_returns_full_provenance(self):
        """GET /generation/asset-versions/{id} returns the full detail projection."""
        version_id = self._make_asset_version_with_provenance()

        response = self.client.get(f"/api/v1/generation/asset-versions/{version_id}")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()

        self.assertEqual(data["id"], version_id)
        self.assertIn("provider", data)
        self.assertIn("model", data)
        self.assertIn("aspect_ratio", data)
        self.assertIn("seed", data)
        self.assertIn("requested", data["seed"])
        self.assertIn("effective", data["seed"])
        self.assertIn("unavailable_reason", data["seed"])
        self.assertEqual(data["seed"]["requested"], "42")
        self.assertEqual(data["seed"]["effective"], "42")
        self.assertIn("prompt", data)
        self.assertEqual(data["prompt"], "a warm-toned product hero shot")
        self.assertIn("reviews", data)
        self.assertIsInstance(data["reviews"], list)
        self.assertEqual(len(data["reviews"]), 1)
        self.assertEqual(data["reviews"][0]["review_value"], "approved")
        self.assertIn("qc_attempts", data)
        self.assertIsInstance(data["qc_attempts"], list)
        self.assertEqual(len(data["qc_attempts"]), 1)
        self.assertEqual(data["qc_attempts"][0]["state"], "passed")
        self.assertEqual(data["latest_qc_attempt_id"], data["qc_attempts"][0]["id"])
        self.assertIn("contextual_conclusions", data)
        self.assertEqual(data["contextual_conclusions"], [])
        self.assertIn("parent", data)
        self.assertIsNone(data["parent"])
        self.assertIn("children", data)
        self.assertEqual(data["children"], [])
        self.assertIn("allowed_actions", data)
        self.assertIn("review_head_id", data)
        self.assertEqual(data["review_head_id"], data["reviews"][0]["id"])
        self.assertIn("effective_review_value", data)
        self.assertEqual(data["effective_review_value"], "approved")

    def test_detail_returns_404_for_wrong_workspace(self):
        """Cross-workspace version ID returns 404."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id

        other_ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: other_ctx
        try:
            response = self.client.get(f"/api/v1/generation/asset-versions/{version_id}")
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.ctx

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["code"], "asset_version_not_found")

    def test_detail_returns_404_for_unknown_version(self):
        """A version id that never existed also returns 404."""
        response = self.client.get("/api/v1/generation/asset-versions/does-not-exist")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["code"], "asset_version_not_found")

    def test_detail_includes_lineage(self):
        """Parent/children lineage is populated for regenerated versions."""
        parent_id, child_id = self._make_regenerated_version()

        parent_response = self.client.get(f"/api/v1/generation/asset-versions/{parent_id}")
        self.assertEqual(parent_response.status_code, 200, parent_response.text)
        parent_data = parent_response.json()
        self.assertGreaterEqual(len(parent_data["children"]), 1)
        self.assertEqual(parent_data["children"][0]["version_id"], child_id)
        self.assertEqual(parent_data["children"][0]["intent"], "new_seed")
        self.assertIsNone(parent_data["parent"])

        child_response = self.client.get(f"/api/v1/generation/asset-versions/{child_id}")
        self.assertEqual(child_response.status_code, 200, child_response.text)
        child_data = child_response.json()
        self.assertIsNotNone(child_data["parent"])
        self.assertEqual(child_data["parent"]["version_id"], parent_id)
        self.assertEqual(child_data["parent"]["intent"], "new_seed")
