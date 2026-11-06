import unittest

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id
from tests.support.library_fixtures import LibraryFixtures


class TestAtomicSubmission(LibraryFixtures):
    def test_submit_creates_run_and_items(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            self.assertIn("operation_id", result)
            self.assertIn("generation_run_id", result)

    def test_same_idempotency_key_same_request_returns_existing(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            ik = new_id("ik")
            r1 = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=ik,
            )
            r2 = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=ik,
            )
            self.assertEqual(r1["operation_id"], r2["operation_id"])
            self.assertEqual(r1["generation_run_id"], r2["generation_run_id"])

    def test_cross_workspace_submission_rejected(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import SubmissionError, submit_generation

        ctx_a = self._make_context("ws_submit_a")
        ctx_b = self._make_context("ws_submit_b")
        with tenant_session(ctx_a) as s:
            draft = create_draft(
                s, ctx_a,
                product_id=self._product_id(ctx_a, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx_a, draft.id, draft.revision)

        with tenant_session(ctx_b) as s:
            with self.assertRaises(SubmissionError) as cm:
                submit_generation(
                    s, ctx_b,
                    preview_id=preview.id,
                    preview_hash=preview.preview_hash,
                    expected_draft_revision=draft.revision,
                    idempotency_key=new_id("ik"),
                )
            self.assertEqual(cm.exception.code, "preview_not_found")

    def test_submit_creates_run_items_from_default_combination(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.models import RunItemRecord
        from sqlalchemy import select

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.workspace_id == ctx.workspace_id,
                    RunItemRecord.generation_run_id == result["generation_run_id"],
                )
            ).scalars().all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].state, "pending")
            self.assertEqual(len(items[0].semantic_fingerprint), 64)

    def test_submit_expands_variants_with_fixed_width_semantic_fingerprints(self):
        from sqlalchemy import select

        from app.generation.draft import create_draft
        from app.generation.models import RunItemRecord
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s,
                ctx,
                product_id=self._product_id(ctx, s),
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                format_config={"variants_per_combination": 3},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            result = submit_generation(
                s,
                ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            items = s.execute(
                select(RunItemRecord)
                .where(
                    RunItemRecord.workspace_id == ctx.workspace_id,
                    RunItemRecord.generation_run_id == result["generation_run_id"],
                )
                .order_by(RunItemRecord.ordinal)
            ).scalars().all()

            self.assertEqual([item.ordinal for item in items], [0, 1, 2])
            self.assertEqual([item.variant_index for item in items], [0, 1, 2])
            self.assertEqual(len({item.semantic_fingerprint for item in items}), 3)
            self.assertTrue(all(len(item.semantic_fingerprint) == 64 for item in items))


if __name__ == "__main__":
    unittest.main()
