"""Domain-level tests for recovery preview creation."""
import unittest

from sqlalchemy import update

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import ResolutionPreviewRecord, utcnow
from app.generation.draft import create_draft
from app.generation.models import RunItemRecord
from app.generation.preview import create_preview
from app.generation.recovery_preview import (
    RecoveryPreviewError,
    create_recovery_preview,
)
from app.generation.submission import submit_generation


class TestRecoveryPreviewCreation(LibraryFixtures):
    def _run_with_failed_items(self, ctx, s, *, count=1):
        """Create a Run and mark item(s) as failed."""
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
            preview_id=preview.id, preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision,
            idempotency_key=f"submit-rp-{id(self)}-{count}",
        )
        run_id = result["generation_run_id"]
        items = s.query(RunItemRecord).filter_by(
            generation_run_id=run_id).all()
        for item in items[:count]:
            s.execute(
                update(RunItemRecord).where(RunItemRecord.id == item.id)
                .values(state="failed", error_summary="test_failure",
                        revision=item.revision + 1, updated_at=utcnow())
            )
        s.flush()
        from app.generation.run_queries import get_run
        run = get_run(s, ctx, run_id)
        return run, items

    def _run_with_paused_items(self, ctx, s, *, resumable=True):
        run, items = self._run_with_failed_items(ctx, s)
        s.execute(
            update(RunItemRecord).where(RunItemRecord.id == items[0].id)
            .values(state="paused", same_snapshot_resumable=resumable,
                    revision=items[0].revision + 2, updated_at=utcnow())
        )
        s.flush()
        return run, items

    def test_create_retry_preview_with_explicit_item_ids(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_failed_items(ctx, s)
            failed_ids = [i.id for i in items if i.state == "failed"]

            result = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="item_ids",
                selection_item_ids=failed_ids,
                expected_run_revision=run.revision,
                idempotency_key="rp-retry-1",
            )

            self.assertIn("id", result)
            self.assertIn("preview_hash", result)
            self.assertEqual(result["command"], "retry_failed_items")
            self.assertEqual(result["purpose"], "run_recovery")
            self.assertEqual(set(result["targets"]), set(failed_ids))
            self.assertIsNotNone(result["estimated_additional_cost"])
            self.assertEqual(result["estimated_additional_cost"]["currency"], "USD")

    def test_create_retry_preview_all_eligible_failed(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_failed_items(ctx, s)

            result = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="all_eligible_failed",
                selection_item_ids=[],
                expected_run_revision=run.revision,
                idempotency_key="rp-retry-all-1",
            )

            self.assertTrue(len(result["targets"]) > 0)

    def test_create_resume_preview_with_resumable_items(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_paused_items(ctx, s, resumable=True)

            result = create_recovery_preview(
                s, ctx, run.id,
                command="resume_paused_items",
                selection_kind="all_resumable_paused",
                selection_item_ids=[],
                expected_run_revision=run.revision,
                idempotency_key="rp-resume-1",
            )

            self.assertTrue(len(result["targets"]) > 0)

    def test_resume_rejects_non_resumable_paused(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_paused_items(ctx, s, resumable=False)
            paused_ids = [i.id for i in items if i.state == "paused"]

            with self.assertRaises(RecoveryPreviewError) as cm:
                create_recovery_preview(
                    s, ctx, run.id,
                    command="resume_paused_items",
                    selection_kind="item_ids",
                    selection_item_ids=paused_ids,
                    expected_run_revision=run.revision,
                    idempotency_key="rp-resume-bad",
                )
            self.assertIn("not_resumable", cm.exception.code)

    def test_retry_rejects_non_failed_items(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_failed_items(ctx, s)
            succeeded_ids = [i.id for i in items if i.state != "failed"]
            if not succeeded_ids:
                return  # all items are failed, skip

            with self.assertRaises(RecoveryPreviewError) as cm:
                create_recovery_preview(
                    s, ctx, run.id,
                    command="retry_failed_items",
                    selection_kind="item_ids",
                    selection_item_ids=succeeded_ids,
                    expected_run_revision=run.revision,
                    idempotency_key="rp-retry-bad",
                )
            self.assertEqual(cm.exception.code, "invalid_target_state")

    def test_stale_run_revision_rejected(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_failed_items(ctx, s)
            failed_ids = [i.id for i in items if i.state == "failed"]

            with self.assertRaises(RecoveryPreviewError) as cm:
                create_recovery_preview(
                    s, ctx, run.id,
                    command="retry_failed_items",
                    selection_kind="item_ids",
                    selection_item_ids=failed_ids,
                    expected_run_revision=run.revision + 999,
                    idempotency_key="rp-stale-rev",
                )
            self.assertEqual(cm.exception.code, "stale_run_revision")

    def test_idempotent_replay_returns_same_preview(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, items = self._run_with_failed_items(ctx, s)
            failed_ids = [i.id for i in items if i.state == "failed"]

            result1 = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="item_ids",
                selection_item_ids=failed_ids,
                expected_run_revision=run.revision,
                idempotency_key="rp-idem-1",
            )
            result2 = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="item_ids",
                selection_item_ids=failed_ids,
                expected_run_revision=run.revision,
                idempotency_key="rp-idem-1",
            )
            self.assertEqual(result1["id"], result2["id"])

    def test_tenant_isolation(self):
        ctx1 = self._make_context()
        ctx2 = self._make_context(workspace_id="ws_other")
        with tenant_session(ctx1) as s:
            run, items = self._run_with_failed_items(ctx1, s)
            failed_ids = [i.id for i in items if i.state == "failed"]

        with tenant_session(ctx2) as s:
            with self.assertRaises(Exception):
                create_recovery_preview(
                    s, ctx2, run.id,
                    command="retry_failed_items",
                    selection_kind="item_ids",
                    selection_item_ids=failed_ids,
                    expected_run_revision=1,
                    idempotency_key="rp-tenant",
                )

    def test_all_eligible_empty_creates_zero_cost_preview(self):
        """all_eligible_failed on a run with no failed items creates an
        empty zero-cost preview (not a 422)."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            # Create a run without failing any items
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
                preview_id=preview.id, preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key="submit-empty-1",
            )
            from app.generation.run_queries import get_run
            run = get_run(s, ctx, result["generation_run_id"])

            rp = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="all_eligible_failed",
                selection_item_ids=[],
                expected_run_revision=run.revision,
                idempotency_key="rp-empty-1",
            )
            self.assertEqual(rp["targets"], [])
            self.assertEqual(rp["estimated_additional_cost"]["amount"], "0")


if __name__ == "__main__":
    unittest.main()
