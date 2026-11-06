import unittest
from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session


class TestDraftCRUD(LibraryFixtures):
    def test_create_draft_resolves_library_product_profile_identity(self):
        from app.generation.draft import create_draft
        from app.library import service
        from app.library.contracts import ENTRY_PRODUCT

        ctx = self._make_context()
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="Generation Bridge Product",
        )

        with tenant_session(ctx) as s:
            draft = create_draft(
                s,
                ctx,
                product_id=entry["id"],
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )

            self.assertNotEqual(draft.product_id, entry["id"])

    def test_create_draft(self):
        from app.generation.draft import create_draft
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            self.assertEqual(draft.workspace_id, ctx.workspace_id)
            self.assertEqual(draft.revision, 1)
            self.assertEqual(draft.state, "editing")

    def test_update_draft_cas(self):
        from app.generation.draft import create_draft, update_draft
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            updated = update_draft(
                s, ctx, draft.id,
                expected_revision=1,
                model="mock-v2",
            )
            self.assertEqual(updated.model, "mock-v2")
            self.assertEqual(updated.revision, 2)

    def test_update_draft_stale_revision_rejected(self):
        from app.generation.draft import create_draft, update_draft, StaleRevisionError
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            update_draft(s, ctx, draft.id, expected_revision=1, model="mock-v2")
            with self.assertRaises(StaleRevisionError):
                update_draft(s, ctx, draft.id, expected_revision=1, model="mock-v3")

    def test_archive_draft(self):
        from app.generation.draft import create_draft, archive_draft
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            archive_draft(s, ctx, draft.id, expected_revision=1)
            self.assertEqual(draft.state, "archived")
            self.assertIsNotNone(draft.archived_at)

    def test_cross_workspace_draft_rejected(self):
        from app.generation.draft import create_draft, update_draft
        ctx_a = self._make_context("ws_a")
        ctx_b = self._make_context("ws_b")
        with tenant_session(ctx_a) as s:
            draft = create_draft(
                s, ctx_a,
                product_id=self._product_id(ctx_a, s),
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
        with tenant_session(ctx_b) as s:
            from app.generation.draft import DraftNotFoundError
            with self.assertRaises(DraftNotFoundError):
                update_draft(s, ctx_b, draft.id, expected_revision=1, model="x")
