import unittest

from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id


class TestRegenerationIntents(LibraryFixtures):
    def test_same_recipe_preserves_prompt(self):
        from app.generation.regeneration import create_regeneration_draft

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            draft = create_regeneration_draft(
                s, ctx, av_id, "same_recipe",
            )
            self.assertEqual(draft.regeneration_intent, "same_recipe")
            self.assertEqual(draft.source_asset_version_id, av_id)

    def test_new_seed_changes_fingerprint(self):
        from app.generation.regeneration import create_regeneration_draft

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            draft = create_regeneration_draft(
                s, ctx, av_id, "new_seed",
            )
            self.assertEqual(draft.regeneration_intent, "new_seed")

    def test_quality_feedback_requires_qc_attempt(self):
        from app.generation.regeneration import (
            create_regeneration_draft, RegenerationError,
        )

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s, skip_qc=True)
            with self.assertRaises(RegenerationError) as cm:
                create_regeneration_draft(
                    s, ctx, av_id, "quality_feedback",
                )
            self.assertEqual(cm.exception.code, "qc_attempt_required")

    def test_quality_feedback_succeeds_with_qc_attempt(self):
        from app.generation.regeneration import create_regeneration_draft

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            draft = create_regeneration_draft(
                s, ctx, av_id, "quality_feedback",
            )
            self.assertEqual(draft.regeneration_intent, "quality_feedback")
            self.assertEqual(draft.source_asset_version_id, av_id)

    def test_parent_version_chain(self):
        from app.generation.regeneration import create_regeneration_draft

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            draft = create_regeneration_draft(
                s, ctx, av_id, "edit_prompt",
                prompt_overrides={"new_prompt": "Different prompt"},
            )
            self.assertEqual(draft.regeneration_intent, "edit_prompt")
            self.assertEqual(draft.source_asset_version_id, av_id)
            self.assertEqual(draft.selectors_json, {"new_prompt": "Different prompt"})

    def test_source_recipe_identity_carried_from_producing_run_item(self):
        from app.generation.regeneration import create_regeneration_draft
        from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
        from sqlalchemy import select

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            binding = s.execute(
                select(RunItemAssetVersionBindingRecord).where(
                    RunItemAssetVersionBindingRecord.asset_version_id == av_id,
                    RunItemAssetVersionBindingRecord.kind == "produced",
                )
            ).scalar_one()
            item = s.get(RunItemRecord, binding.run_item_id)

            draft = create_regeneration_draft(
                s, ctx, av_id, "same_recipe",
            )
            self.assertEqual(draft.source_recipe_identity, item.semantic_fingerprint)

    def test_invalid_intent_rejected(self):
        from app.generation.regeneration import create_regeneration_draft, RegenerationError

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            with self.assertRaises(RegenerationError) as cm:
                create_regeneration_draft(s, ctx, av_id, "bogus_intent")
            self.assertEqual(cm.exception.code, "invalid_intent")

    def test_source_not_found_rejected(self):
        from app.generation.regeneration import create_regeneration_draft, RegenerationError

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            with self.assertRaises(RegenerationError) as cm:
                create_regeneration_draft(s, ctx, "av_nonexistent", "same_recipe")
            self.assertEqual(cm.exception.code, "source_not_found")

    def test_cross_workspace_source_rejected(self):
        from app.generation.regeneration import create_regeneration_draft, RegenerationError

        ctx_a = self._make_context("ws_regen_a")
        ctx_b = self._make_context("ws_regen_b")

        with tenant_session(ctx_a) as s:
            av_id = self._create_approved_version(ctx_a, s)

        with tenant_session(ctx_b) as s:
            with self.assertRaises(RegenerationError) as cm:
                create_regeneration_draft(s, ctx_b, av_id, "same_recipe")
            self.assertEqual(cm.exception.code, "source_not_found")

    def test_regenerated_draft_resolves_product_from_source_version(self):
        from app.generation.regeneration import create_regeneration_draft
        from app.adapters.persistence.models import AssetVersionRecord, AssetRecord

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            version = s.get(AssetVersionRecord, av_id)
            asset = s.get(AssetRecord, version.asset_id)

            draft = create_regeneration_draft(s, ctx, av_id, "same_recipe")
            self.assertEqual(draft.product_id, asset.product_id)

    def _create_approved_version(self, ctx, session, skip_qc=False):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.qc import run_qc
        from app.generation.review import create_review
        from app.generation.models import RunItemRecord, RunItemAssetVersionBindingRecord
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

        binding = session.execute(
            select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.run_item_id == items[0].id,
                RunItemAssetVersionBindingRecord.kind == "produced",
            )
        ).scalar_one()

        if not skip_qc:
            run_qc(session, ctx, binding.asset_version_id, items[0].id)
            create_review(
                session, ctx, binding.asset_version_id,
                "approved", "Good", "test",
            )
        return binding.asset_version_id


if __name__ == "__main__":
    unittest.main()
