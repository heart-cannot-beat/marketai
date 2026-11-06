import unittest

from app.adapters.persistence.database import tenant_session
from tests.support.library_fixtures import LibraryFixtures


class TestPreviewCreation(LibraryFixtures):
    def test_create_initial_preview(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

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
            self.assertEqual(preview.purpose, "initial_generation")
            self.assertEqual(preview.execution_mode, "mock")
            self.assertIsNotNone(preview.preview_hash)

    def test_stale_draft_revision_rejected(self):
        from app.generation.draft import create_draft, update_draft
        from app.generation.preview import PreviewError, create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            update_draft(s, ctx, draft.id, expected_revision=1, model="mock-v2")
            with self.assertRaises(PreviewError) as cm:
                create_preview(s, ctx, draft.id, 1)
            self.assertEqual(cm.exception.code, "stale_draft_revision")

    def test_cross_workspace_preview_rejected(self):
        from app.generation.draft import DraftNotFoundError, create_draft
        from app.generation.preview import create_preview

        ctx_a = self._make_context("ws_preview_a")
        ctx_b = self._make_context("ws_preview_b")
        with tenant_session(ctx_a) as s:
            draft = create_draft(
                s, ctx_a,
                product_id=self._product_id(ctx_a, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
        with tenant_session(ctx_b) as s:
            with self.assertRaises(DraftNotFoundError):
                create_preview(s, ctx_b, draft.id, draft.revision)

    def test_preview_hash_deterministic(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            p1 = create_preview(s, ctx, draft.id, draft.revision)
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            p2 = create_preview(s, ctx, draft.id, draft.revision)
        self.assertEqual(p1.recipe_input_hash, p2.recipe_input_hash)


class TestSnapshotReuse(LibraryFixtures):
    def test_insert_or_reuse_snapshot(self):
        from app.generation.snapshot import insert_or_reuse_snapshot

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            snap1 = insert_or_reuse_snapshot(
                s, ctx,
                canonical_json={"prompt": "test", "provider": "mock"},
                execution_mode="mock",
                canonicalization_version="v1",
            )
            snap2 = insert_or_reuse_snapshot(
                s, ctx,
                canonical_json={"prompt": "test", "provider": "mock"},
                execution_mode="mock",
                canonicalization_version="v1",
            )
            self.assertEqual(snap1.id, snap2.id)


if __name__ == "__main__":
    unittest.main()
