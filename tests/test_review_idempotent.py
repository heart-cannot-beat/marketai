"""Idempotent review creation + effective-review projection (Issue #49 Task 5).

Two test classes:

* ``TestProjectEffectiveReview`` -- direct, non-HTTP tests of
  ``app.generation.review.project_effective_review`` /
  ``is_ready_for_experiment`` / the ``create_review`` head guard against a
  real tenant-scoped session.
* ``TestReviewRouteIdempotency`` -- HTTP-level tests of
  ``POST /api/v1/generation/asset-versions/{version_id}/reviews`` via the
  real FastAPI app + ``TestClient``, including the head guard and gallery/
  detail wiring of the real supersession-aware projection.

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``tenant_session``/``TestClient`` with a ``request_tenant_context`` dependency
override (see ``tests/test_asset_download_intent.py`` and
``tests/test_asset_gallery_page.py``), not pytest fixtures like
``db_session``/``tenant_ctx``/``client``/``auth_headers``/``make_asset_version``
-- this file follows the repo's real conventions rather than the plan
brief's pytest-flavored pseudocode.
"""
from __future__ import annotations

import unittest

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault before app.config is imported anywhere else in
# the process -- see tests/test_v1_library_routes.py for the same note.
from tests.support.library_fixtures import LibraryFixtures

from fastapi.testclient import TestClient

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord, new_id
from app.generation.review import (
    ReviewConflictError,
    create_review,
    is_ready_for_experiment,
    project_effective_review,
)
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext


class TestProjectEffectiveReview(LibraryFixtures):
    """Direct tests of ``project_effective_review`` and the head guard."""

    def _make_version(self, ctx: TenantContext, session) -> str:
        asset_id = self._asset_id(ctx, session)
        version = AssetVersionRecord(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset_id}.png",
            content_sha256="c" * 64,
            byte_size=100,
            mime_type="image/png",
        )
        session.add(version)
        session.flush()
        return version.id

    def test_effective_review_is_newest_non_superseded_head(self):
        """project_effective_review returns the newest non-superseded review value."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)

            r1 = create_review(s, ctx, version_id, "approved", "initial", "actor-1")
            self.assertEqual(project_effective_review(s, ctx, version_id), "approved")

            create_review(
                s, ctx, version_id, "needs_changes", "correction", "actor-1",
                supersedes_review_id=r1.id,
            )
            self.assertEqual(
                project_effective_review(s, ctx, version_id), "needs_changes",
            )

    def test_cleared_review_removes_effective_value(self):
        """A 'cleared' head makes the effective review None -- a tombstone."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)

            r1 = create_review(s, ctx, version_id, "approved", "initial", "actor-1")
            create_review(
                s, ctx, version_id, "cleared", "withdraw", "actor-1",
                supersedes_review_id=r1.id,
            )
            self.assertIsNone(project_effective_review(s, ctx, version_id))

    def test_effective_review_is_none_with_no_reviews(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)
            self.assertIsNone(project_effective_review(s, ctx, version_id))

    def test_effective_review_exists_guard(self):
        """Submitting a review without supersedes_review_id when a head
        already exists returns 409 effective_review_exists."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)
            create_review(s, ctx, version_id, "approved", "initial", "actor-1")

            with self.assertRaises(ReviewConflictError) as cm:
                create_review(
                    s, ctx, version_id, "rejected", "second", "actor-2",
                    supersedes_review_id=None,
                )
            self.assertEqual(cm.exception.code, "effective_review_exists")

    def test_effective_review_exists_guard_continues_from_cleared_head(self):
        """The guard fires even when the head is 'cleared' -- the correction
        chain continues from the last row, not the last visible value."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)
            r1 = create_review(s, ctx, version_id, "approved", "initial", "actor-1")
            create_review(
                s, ctx, version_id, "cleared", "withdraw", "actor-1",
                supersedes_review_id=r1.id,
            )

            with self.assertRaises(ReviewConflictError) as cm:
                create_review(
                    s, ctx, version_id, "approved", "re-approve", "actor-1",
                    supersedes_review_id=None,
                )
            self.assertEqual(cm.exception.code, "effective_review_exists")

    def test_first_review_on_a_version_needs_no_supersedes(self):
        """No head yet -- the guard does not fire for the very first review."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)
            review = create_review(s, ctx, version_id, "approved", "initial", "actor-1")
            self.assertIsNone(review.supersedes_review_id)

    def test_is_ready_for_experiment_uses_effective_review(self):
        """is_ready_for_experiment uses effective review, not any historical Approved."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version_id = self._make_version(ctx, s)
            r1 = create_review(s, ctx, version_id, "approved", "initial", "actor-1")
            self.assertTrue(is_ready_for_experiment(s, ctx, version_id))

            create_review(
                s, ctx, version_id, "cleared", "undo", "actor-1",
                supersedes_review_id=r1.id,
            )
            self.assertFalse(is_ready_for_experiment(s, ctx, version_id))


class TestReviewRouteIdempotency(LibraryFixtures):
    """``POST /generation/asset-versions/{version_id}/reviews``."""

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-{tag}-{new_id('k')}"}

    def _create_version(self) -> str:
        with tenant_session(self.ctx) as session:
            asset_id = self._asset_id(self.ctx, session)
            version = AssetVersionRecord(
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
            )
            session.add(version)
            session.flush()
            return version.id

    def test_review_replay_returns_same_record(self):
        """Replaying a review with the same Idempotency-Key returns the same review."""
        version_id = self._create_version()
        headers = self._ik("replay")
        body = {"review_value": "approved", "reason": "good"}

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json=body, headers=headers,
        )
        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json=body, headers=headers,
        )

        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(r1.json()["id"], r2.json()["id"])

    def test_review_replay_different_body_conflicts(self):
        """Reusing an Idempotency-Key with a different body returns 409."""
        version_id = self._create_version()
        headers = self._ik("conflict")

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "good"},
            headers=headers,
        )
        self.assertEqual(r1.status_code, 201, r1.text)

        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "different reason entirely"},
            headers=headers,
        )
        self.assertEqual(r2.status_code, 409, r2.text)

    def test_review_requires_idempotency_key(self):
        version_id = self._create_version()
        resp = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "good"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_head_guard_returns_409_without_supersedes(self):
        """A second review without supersedes_review_id returns 409."""
        version_id = self._create_version()

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "good"},
            headers=self._ik("head-1"),
        )
        self.assertEqual(r1.status_code, 201, r1.text)

        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "rejected", "reason": "changed mind"},
            headers=self._ik("head-2"),
        )
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertEqual(r2.json()["code"], "effective_review_exists")

    def test_correction_with_supersedes_review_id_succeeds(self):
        """A correction naming supersedes_review_id is accepted and becomes
        the new effective head."""
        version_id = self._create_version()

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "good"},
            headers=self._ik("corr-1"),
        )
        self.assertEqual(r1.status_code, 201, r1.text)
        review_id = r1.json()["id"]

        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={
                "review_value": "needs_changes",
                "reason": "actually needs work",
                "supersedes_review_id": review_id,
            },
            headers=self._ik("corr-2"),
        )
        self.assertEqual(r2.status_code, 201, r2.text)

        detail = self.client.get(
            f"/api/v1/generation/asset-versions/{version_id}"
        ).json()
        self.assertEqual(detail["effective_review_value"], "needs_changes")
        self.assertEqual(detail["review_head_id"], r2.json()["id"])
        self.assertEqual(len(detail["reviews"]), 2)

    def test_gallery_reflects_cleared_review_as_unreviewed(self):
        """A cleared correction makes the gallery's effective_review_value
        None (and 'unreviewed' filterable) -- not the interim 'latest row
        wins' value, which would incorrectly surface 'cleared' itself."""
        version_id = self._create_version()

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "good"},
            headers=self._ik("clr-1"),
        )
        review_id = r1.json()["id"]

        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={
                "review_value": "cleared",
                "reason": "mistake",
                "supersedes_review_id": review_id,
            },
            headers=self._ik("clr-2"),
        )
        self.assertEqual(r2.status_code, 201, r2.text)

        gallery = self.client.get("/api/v1/generation/assets").json()
        item = next(i for i in gallery["items"] if i["id"] == version_id)
        self.assertIsNone(item["effective_review_value"])

        unreviewed = self.client.get(
            "/api/v1/generation/assets", params={"review_value": "unreviewed"},
        ).json()
        self.assertIn(version_id, {i["id"] for i in unreviewed["items"]})

        approved_only = self.client.get(
            "/api/v1/generation/assets", params={"review_value": "approved"},
        ).json()
        self.assertNotIn(version_id, {i["id"] for i in approved_only["items"]})

    def test_gallery_reviews_facet_reflects_effective_values(self):
        """The 'reviews' facet is populated from effective review values."""
        version_id = self._create_version()
        self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/reviews",
            json={"review_value": "approved", "reason": "good"},
            headers=self._ik("facet-1"),
        )

        gallery = self.client.get("/api/v1/generation/assets").json()
        facet_values = {f["value"] for f in gallery["facets"]["reviews"]}
        self.assertIn("approved", facet_values)


if __name__ == "__main__":
    unittest.main()
