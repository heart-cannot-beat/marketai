import unittest

from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id


class TestQualityControl(LibraryFixtures):
    def test_qc_pass_allows_approval(self):
        from app.adapters.persistence.models import GenerationRunRecord
        from app.generation.models import RunItemRecord
        from app.generation.qc import run_qc
        from app.generation.review import create_review

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, ri_id = self._create_generated_version(ctx, s)
            qc = run_qc(s, ctx, av_id, ri_id)
            self.assertEqual(qc.state, "passed")
            item = s.get(RunItemRecord, ri_id)
            run = s.get(GenerationRunRecord, item.generation_run_id)
            self.assertEqual(run.done, 1)
            self.assertEqual(run.status, "succeeded")
            review = create_review(
                s, ctx, av_id, "approved", "Good quality", ctx.initiator_id or "test",
            )
            self.assertEqual(review.review_value, "approved")

    def test_blocking_qc_prevents_approval(self):
        from app.generation.qc import run_qc_with_blocking_failure
        from app.generation.review import create_review, ReviewError

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, ri_id = self._create_generated_version(ctx, s)
            qc = run_qc_with_blocking_failure(s, ctx, av_id, ri_id)
            self.assertEqual(qc.state, "failed_blocking")
            with self.assertRaises(ReviewError) as cm:
                create_review(
                    s, ctx, av_id, "approved", "Looks good", "test",
                )
            self.assertEqual(cm.exception.code, "blocking_qc_failure")

    def test_review_is_exact_version_scoped(self):
        from app.generation.qc import run_qc
        from app.generation.review import create_review

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, ri_id = self._create_generated_version(ctx, s)
            run_qc(s, ctx, av_id, ri_id)
            review = create_review(
                s, ctx, av_id, "approved", "Good", "test",
            )
            self.assertEqual(review.asset_version_id, av_id)
            self.assertFalse(review.legacy_unscoped)

    def test_ready_for_experiment_requires_approved_no_blocking_qc(self):
        from app.generation.qc import run_qc
        from app.generation.review import create_review, is_ready_for_experiment

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id, ri_id = self._create_generated_version(ctx, s)
            self.assertFalse(is_ready_for_experiment(s, ctx, av_id))
            run_qc(s, ctx, av_id, ri_id)
            self.assertFalse(is_ready_for_experiment(s, ctx, av_id))
            create_review(s, ctx, av_id, "approved", "Good", "test")
            self.assertTrue(is_ready_for_experiment(s, ctx, av_id))

    def test_cross_workspace_qc_and_review_rejected(self):
        from sqlalchemy import select
        from sqlalchemy.exc import NoResultFound

        from app.adapters.persistence.models import QualityReviewRecord
        from app.generation.models import AssetQCAttemptRecord
        from app.generation.qc import run_qc
        from app.generation.review import ReviewError, create_review

        ctx_a = self._make_context("ws_qc_a")
        ctx_b = self._make_context("ws_qc_b")

        with tenant_session(ctx_a) as s:
            av_id, ri_id = self._create_generated_version(ctx_a, s)

        # run_qc's RunItem lookup is workspace-scoped: a foreign-workspace
        # context can't see workspace A's RunItem at all.
        with tenant_session(ctx_b) as s:
            with self.assertRaises(NoResultFound):
                run_qc(s, ctx_b, av_id, ri_id)

        # create_review's AssetVersion lookup is likewise workspace-scoped.
        with tenant_session(ctx_b) as s:
            with self.assertRaises(ReviewError) as cm:
                create_review(s, ctx_b, av_id, "approved", "cross-tenant", "test")
            self.assertEqual(cm.exception.code, "asset_version_not_found")

        # Neither rejected cross-workspace call left any trace: no QC attempt
        # and no review record exist for this asset_version_id anywhere.
        with tenant_session(ctx_a) as s:
            attempts = s.execute(
                select(AssetQCAttemptRecord).where(
                    AssetQCAttemptRecord.asset_version_id == av_id
                )
            ).scalars().all()
            self.assertEqual(attempts, [])
            reviews = s.execute(
                select(QualityReviewRecord).where(
                    QualityReviewRecord.asset_version_id == av_id
                )
            ).scalars().all()
            self.assertEqual(reviews, [])

    def _create_generated_version(self, ctx, session):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.models import RunItemRecord
        from sqlalchemy import select

        draft = create_draft(
            session, ctx,
            product_id=self._product_id(ctx, session),
            provider="mock", model="mock-v1",
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

        from app.generation.models import RunItemAssetVersionBindingRecord
        binding = session.execute(
            select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.run_item_id == items[0].id,
                RunItemAssetVersionBindingRecord.kind == "produced",
            )
        ).scalar_one()
        return binding.asset_version_id, items[0].id


if __name__ == "__main__":
    unittest.main()
