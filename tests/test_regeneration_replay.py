"""Idempotent regeneration-draft replay + review-summary batch (Issue #49
Task 7).

Adapted from the plan brief's pytest-flavored pseudocode
(``.superpowers/sdd/2026-09-08-assets-server-contracts/task-7-brief.md``) to
this repo's real conventions -- ``unittest.TestCase`` + ``LibraryFixtures`` +
``TestClient`` with a ``request_tenant_context`` dependency override, exactly
as ``tests/test_review_idempotent.py`` does for Task 5's idempotent review
route. There is no pytest ``client``/``auth_headers``/``make_asset_version``
fixture ecosystem in this repo, and the batch route lives at
``POST /api/v1/generation/regeneration-draft-batches`` grouping candidates by
``stable_dimension_signature`` -- a column that lives on the *producing*
``RunItemRecord``, not on ``AssetVersionRecord`` itself (see
``app.generation.regeneration._resolve_stable_dimension_signature``), so the
fixture helpers below wire a full AssetVersion -> ``produced`` binding ->
RunItem chain rather than a bare AssetVersion.

The regeneration-draft route this repo actually has is
``POST /asset-versions/{version_id}/regeneration-drafts`` (a path parameter,
not a ``source_version_id`` body field) -- the brief's pseudocode names a
different, nonexistent route; these tests hit the real one.
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from tests.support.library_fixtures import LibraryFixtures

from fastapi.testclient import TestClient

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, new_id, utcnow
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.review import create_review
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext


class _RegenerationBatchFixtures(LibraryFixtures):
    """Shared HTTP-level fixtures for both test classes below."""

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-{tag}-{new_id('k')}"}

    def _make_version(
        self,
        ctx: TenantContext,
        session,
        *,
        signature: str = "",
        review_value: str | None = None,
        offset_seconds: int = 0,
    ) -> str:
        """A full AssetVersion with a ``produced`` RunItem binding carrying
        ``stable_dimension_signature`` -- the join the batch domain function
        walks to group candidates (``AssetVersionRecord`` has no such column
        of its own)."""
        asset_id = self._asset_id(ctx, session)
        run_id = self._create_generation_run(ctx, session)
        item = RunItemRecord(
            id=new_id("ri"),
            workspace_id=ctx.workspace_id,
            generation_run_id=run_id,
            ordinal=0,
            combination_fingerprint="fp_" + new_id("h"),
            semantic_fingerprint="sf_" + new_id("h"),
            stable_dimension_signature=signature,
            execution_mode="mock",
            state="succeeded",
            revision=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(item)
        session.flush()

        version = AssetVersionRecord(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset_id}.png",
            content_sha256="c" * 64,
            byte_size=1024,
            mime_type="image/png",
            origin_kind="generation",
            execution_mode="mock",
            created_at=utcnow() + timedelta(seconds=offset_seconds),
        )
        session.add(version)
        session.flush()

        session.add(
            RunItemAssetVersionBindingRecord(
                id=new_id("rib"),
                workspace_id=ctx.workspace_id,
                run_item_id=item.id,
                asset_version_id=version.id,
                kind="produced",
            )
        )
        session.flush()

        if review_value is not None:
            create_review(session, ctx, version.id, review_value, "test", "actor-1")

        return version.id

    def _make_versions(
        self,
        ctx: TenantContext,
        *,
        signature: str,
        count: int,
        review_value: str,
    ) -> list[str]:
        with tenant_session(ctx) as session:
            return [
                self._make_version(
                    ctx, session,
                    signature=signature, review_value=review_value,
                    offset_seconds=i,
                )
                for i in range(count)
            ]


class TestRegenerationDraftReplay(_RegenerationBatchFixtures):
    """``POST /asset-versions/{version_id}/regeneration-drafts``."""

    def _create_version(self) -> str:
        with tenant_session(self.ctx) as session:
            return self._make_version(self.ctx, session)

    def test_regeneration_draft_replay_returns_same_draft(self):
        """Replaying a regeneration draft with the same key returns the same draft."""
        version_id = self._create_version()
        headers = self._ik("replay")
        body = {"intent": "same_recipe"}

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json=body, headers=headers,
        )
        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json=body, headers=headers,
        )

        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(r1.json()["id"], r2.json()["id"])

    def test_regeneration_replay_different_body_conflicts(self):
        """Reusing idempotency key with different intent returns 409."""
        version_id = self._create_version()
        headers = self._ik("conflict")

        r1 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json={"intent": "same_recipe"}, headers=headers,
        )
        r2 = self.client.post(
            f"/api/v1/generation/asset-versions/{version_id}/regeneration-drafts",
            json={"intent": "new_seed"}, headers=headers,
        )

        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 409, r2.text)


class TestReviewSummaryRegenerationBatch(_RegenerationBatchFixtures):
    """``POST /generation/regeneration-draft-batches``."""

    def test_review_summary_batch_needs_changes_creates_2_per_sig(self):
        """Review summary batch creates 2 drafts per stable dimension signature."""
        version_ids = self._make_versions(
            self.ctx, signature="sig-A", count=3, review_value="needs_changes",
        )

        headers = self._ik("batch-nc")
        body = {
            "action": "regenerate_needs_changes",
            "candidate_version_ids": version_ids,
        }
        response = self.client.post(
            "/api/v1/generation/regeneration-draft-batches",
            json=body, headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        drafts = response.json()["drafts"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["requested_variant_count"], 2)
        self.assertEqual(drafts[0]["stable_dimension_signature"], "sig-A")

    def test_review_summary_batch_approved_creates_3_per_sig(self):
        """Approved supplementation creates 3 drafts per signature."""
        version_ids = self._make_versions(
            self.ctx, signature="sig-B", count=5, review_value="approved",
        )

        headers = self._ik("batch-app")
        body = {
            "action": "supplement_approved",
            "candidate_version_ids": version_ids,
        }
        response = self.client.post(
            "/api/v1/generation/regeneration-draft-batches",
            json=body, headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        drafts = response.json()["drafts"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["requested_variant_count"], 3)
        self.assertEqual(drafts[0]["stable_dimension_signature"], "sig-B")

    def test_review_summary_batch_replay_returns_same(self):
        """Replaying the batch returns the same ordered Draft result."""
        version_ids = self._make_versions(
            self.ctx, signature="sig-C", count=2, review_value="needs_changes",
        )

        headers = self._ik("batch-replay")
        body = {
            "action": "regenerate_needs_changes",
            "candidate_version_ids": version_ids,
        }
        r1 = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )
        r2 = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )

        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(
            r1.json()["drafts"][0]["generation_draft_id"],
            r2.json()["drafts"][0]["generation_draft_id"],
        )

    def test_review_summary_batch_cross_workspace_fails(self):
        """Cross-workspace candidate IDs fail the whole batch."""
        other_ctx = self._make_context()
        other_workspace_version_ids = self._make_versions(
            other_ctx, signature="sig-D", count=1, review_value="needs_changes",
        )

        headers = self._ik("batch-xws")
        body = {
            "action": "regenerate_needs_changes",
            "candidate_version_ids": other_workspace_version_ids,
        }
        response = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_review_summary_batch_wrong_review_fails_whole_batch(self):
        """A candidate whose effective review doesn't match the action's
        required value fails the WHOLE batch (spec §0.6), not just itself --
        it must not be silently dropped while the other candidates succeed."""
        needs_changes_ids = self._make_versions(
            self.ctx, signature="sig-E", count=1, review_value="needs_changes",
        )
        approved_ids = self._make_versions(
            self.ctx, signature="sig-F", count=1, review_value="approved",
        )

        headers = self._ik("batch-wrong-review")
        body = {
            "action": "regenerate_needs_changes",
            "candidate_version_ids": needs_changes_ids + approved_ids,
        }
        response = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "wrong_review")

    def test_review_summary_batch_missing_signature_fails_whole_batch(self):
        """A candidate with no resolvable stable_dimension_signature fails
        the WHOLE batch (spec §0.6) instead of silently grouping in under
        an empty-string signature."""
        version_ids = self._make_versions(
            self.ctx, signature="", count=1, review_value="needs_changes",
        )

        headers = self._ik("batch-missing-sig")
        body = {
            "action": "regenerate_needs_changes",
            "candidate_version_ids": version_ids,
        }
        response = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "missing_signature")

    def test_review_summary_batch_three_groups_does_not_overflow_resource_id(self):
        """A batch spanning 3+ signature groups must not overflow the
        idempotency ledger's VARCHAR(100) response_resource_id (Issue #49
        Task 7 review, Finding 2) -- joining three 35-char Draft ids alone
        is already over 100 characters."""
        version_ids = []
        for signature in ("sig-G", "sig-H", "sig-I"):
            version_ids += self._make_versions(
                self.ctx, signature=signature, count=1, review_value="needs_changes",
            )

        headers = self._ik("batch-3groups")
        body = {
            "action": "regenerate_needs_changes",
            "candidate_version_ids": version_ids,
        }
        r1 = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )
        self.assertEqual(r1.status_code, 201, r1.text)
        drafts1 = r1.json()["drafts"]
        self.assertEqual(len(drafts1), 3)
        self.assertEqual(
            {d["stable_dimension_signature"] for d in drafts1},
            {"sig-G", "sig-H", "sig-I"},
        )

        # Replay must also reconstruct all three drafts, not just the one
        # whose id got bound as response_resource_id.
        r2 = self.client.post(
            "/api/v1/generation/regeneration-draft-batches", json=body, headers=headers,
        )
        self.assertEqual(r2.status_code, 201, r2.text)
        drafts2 = r2.json()["drafts"]
        self.assertEqual(
            [d["generation_draft_id"] for d in drafts1],
            [d["generation_draft_id"] for d in drafts2],
        )


if __name__ == "__main__":
    unittest.main()
