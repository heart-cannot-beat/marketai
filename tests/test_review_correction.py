"""Tests for the Review correction model (spec §6 -- review undo, #27 Task 4).

A ``cleared`` review corrects (supersedes) an earlier review on the exact
same AssetVersion. ``create_review`` enforces: ``cleared`` requires
``supersedes_review_id``; a prior review may be superseded at most once
(``ReviewConflictError``); and a review may only supersede another review
that belongs to the same AssetVersion (``ReviewConflictError``,
cross-version correction is rejected).
"""
import unittest

# ``tests.support.library_fixtures`` sets the MARKETAI_DATABASE_URL /
# MARKETAI_DATA_DIR env vars (a fresh per-process temp dir) at import time,
# and every ``app.*`` module reads them via ``app.config`` on first import.
# This import must come before any ``app.*`` import in this file, or the
# app modules below lock in the ambient/default DB path instead of the
# isolated test temp dir -- matching the convention in tests/test_generation_qc.py.
from tests.support.library_fixtures import LibraryFixtures

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.review import ReviewConflictError, create_review


class TestReviewCorrection(LibraryFixtures):
    def test_cleared_review_requires_supersedes(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, _ri_id = self._create_generated_version(ctx, s)
            with self.assertRaises(ValueError) as cm:
                create_review(
                    s, ctx, av_id, "cleared", "mistake", "user1",
                )
            self.assertIn("supersedes_review_id", str(cm.exception))

    def test_cleared_review_supersedes_existing(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, _ri_id = self._create_generated_version(ctx, s)
            r1 = create_review(s, ctx, av_id, "approved", "good", "user1")
            r2 = create_review(
                s, ctx, av_id, "cleared", "undo", "user1",
                supersedes_review_id=r1.id,
            )
            self.assertEqual(r2.supersedes_review_id, r1.id)

    def test_cannot_supersede_same_review_twice(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, _ri_id = self._create_generated_version(ctx, s)
            r1 = create_review(s, ctx, av_id, "approved", "good", "user1")
            create_review(
                s, ctx, av_id, "cleared", "undo", "user1",
                supersedes_review_id=r1.id,
            )
            with self.assertRaises(ReviewConflictError) as cm:
                create_review(
                    s, ctx, av_id, "rejected", "changed mind", "user1",
                    supersedes_review_id=r1.id,
                )
            self.assertEqual(cm.exception.code, "review_already_superseded")

    def test_cross_version_correction_rejected(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, _ri_id = self._create_generated_version(ctx, s)
            # A distinct ``model`` keeps the recipe hash (and therefore the
            # cost-reservation semantic key) from colliding with the first
            # AssetVersion created above -- these must be two genuinely
            # different AssetVersions, not the same one twice.
            other_av_id, _other_ri_id = self._create_generated_version(
                ctx, s, model="mock-v2",
            )
            r1 = create_review(s, ctx, av_id, "approved", "good", "user1")
            with self.assertRaises(ReviewConflictError) as cm:
                create_review(
                    s, ctx, other_av_id, "cleared", "undo", "user1",
                    supersedes_review_id=r1.id,
                )
            self.assertIn("cross-version", str(cm.exception))

    def test_non_cleared_review_does_not_require_supersedes(self):
        """Existing (non-correction) review flow is unaffected."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, _ri_id = self._create_generated_version(ctx, s)
            review = create_review(s, ctx, av_id, "approved", "good", "user1")
            self.assertIsNone(review.supersedes_review_id)

    def test_supersedes_review_not_found(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, _ri_id = self._create_generated_version(ctx, s)
            with self.assertRaises(ReviewConflictError) as cm:
                create_review(
                    s, ctx, av_id, "cleared", "undo", "user1",
                    supersedes_review_id="qr_missing",
                )
            self.assertEqual(cm.exception.code, "supersedes_review_not_found")

    def _create_generated_version(self, ctx, session, model: str = "mock-v1"):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item

        draft = create_draft(
            session, ctx,
            product_id=self._product_id(ctx, session),
            provider="mock", model=model,
            execution_mode="mock",
            combination_strategy={"kind": "full", "planner_version": "v1"},
        )
        preview = create_preview(session, ctx, draft.id, draft.revision)
        result = submit_generation(
            session, ctx,
            preview_id=preview.id,
            preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision,
            idempotency_key=new_id("ik"),
        )
        items = session.execute(
            select(RunItemRecord).where(
                RunItemRecord.generation_run_id == result["generation_run_id"]
            )
        ).scalars().all()
        handle_run_item(session, ctx, items[0].id)

        binding = session.execute(
            select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.run_item_id == items[0].id,
                RunItemAssetVersionBindingRecord.kind == "produced",
            )
        ).scalar_one()
        return binding.asset_version_id, items[0].id


if __name__ == "__main__":
    unittest.main()
