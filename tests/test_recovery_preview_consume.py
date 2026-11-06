"""Tests for atomic recovery preview consumption."""
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
    consume_recovery_preview,
)
from app.generation.submission import submit_generation


class TestConsumeRecoveryPreview(LibraryFixtures):
    def _setup_retry(self, ctx, s, *, ik_suffix=""):
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
            idempotency_key=f"submit-consume-{id(self)}{ik_suffix}",
        )
        run_id = result["generation_run_id"]
        items = s.query(RunItemRecord).filter_by(
            generation_run_id=run_id).all()
        s.execute(
            update(RunItemRecord).where(RunItemRecord.id == items[0].id)
            .values(state="failed", error_summary="test",
                    revision=items[0].revision + 1, updated_at=utcnow())
        )
        s.flush()
        from app.generation.run_queries import get_run
        run = get_run(s, ctx, run_id)

        rp = create_recovery_preview(
            s, ctx, run.id,
            command="retry_failed_items",
            selection_kind="all_eligible_failed",
            selection_item_ids=[],
            expected_run_revision=run.revision,
            idempotency_key=f"rp-consume-{id(self)}{ik_suffix}",
        )
        return run, rp

    def test_consume_creates_operation_and_command(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, rp = self._setup_retry(ctx, s)

            result = consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=rp["id"],
                preview_hash=rp["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key="consume-1",
            )
            self.assertIn("operation_id", result)
            self.assertIn("run_id", result)

    def test_consume_marks_preview_submitted(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, rp = self._setup_retry(ctx, s)

            consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=rp["id"],
                preview_hash=rp["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key="consume-2",
            )
            preview = s.get(ResolutionPreviewRecord, rp["id"])
            self.assertIsNotNone(preview.submitted_run_id)
            self.assertEqual(preview.submitted_run_id, run.id)

    def test_consume_reopens_failed_items_as_queued(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, rp = self._setup_retry(ctx, s)

            consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=rp["id"],
                preview_hash=rp["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key="consume-3",
            )
            targets = rp["targets"]
            for tid in targets:
                item = s.get(RunItemRecord, tid)
                self.assertEqual(item.state, "queued")
                self.assertFalse(item.same_snapshot_resumable)

    def test_idempotent_replay_before_consumed_check(self):
        """Replay returns original result, not preview_already_submitted."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, rp = self._setup_retry(ctx, s)

            r1 = consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=rp["id"],
                preview_hash=rp["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key="consume-idem",
            )
            r2 = consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=rp["id"],
                preview_hash=rp["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key="consume-idem",
            )
            self.assertEqual(r1["operation_id"], r2["operation_id"])

    def test_stale_hash_rejected(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run, rp = self._setup_retry(ctx, s)

            with self.assertRaises(RecoveryPreviewError) as cm:
                consume_recovery_preview(
                    s, ctx,
                    run_id=run.id,
                    command="retry_failed_items",
                    preview_id=rp["id"],
                    preview_hash="wrong_hash",
                    expected_run_revision=run.revision,
                    idempotency_key="consume-bad-hash",
                )
            self.assertEqual(cm.exception.code, "preview_hash_mismatch")

    def test_empty_preview_not_consumable(self):
        """A preview with no targets cannot be consumed."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            # Create run with no failed items
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
                idempotency_key="submit-empty-consume",
            )
            from app.generation.run_queries import get_run
            run = get_run(s, ctx, result["generation_run_id"])

            rp = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="all_eligible_failed",
                selection_item_ids=[],
                expected_run_revision=run.revision,
                idempotency_key="rp-empty-consume",
            )

            with self.assertRaises(RecoveryPreviewError) as cm:
                consume_recovery_preview(
                    s, ctx,
                    run_id=run.id,
                    command="retry_failed_items",
                    preview_id=rp["id"],
                    preview_hash=rp["preview_hash"],
                    expected_run_revision=run.revision,
                    idempotency_key="consume-empty",
                )
            self.assertEqual(cm.exception.code, "empty_preview")
