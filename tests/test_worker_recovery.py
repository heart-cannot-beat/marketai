"""Tests for Worker `same_snapshot_resumable` handling (Issue #21 recovery preview, Task 3).

Covers:
  - A recoverable StepRejection (BYOK with no credential evidence) pauses the
    RunItem and sets `same_snapshot_resumable`.
  - A non-recoverable StepRejection (sandbox with no adapter advertised)
    fails the RunItem and leaves `same_snapshot_resumable` False.
  - Transitioning a paused, resumable item to `queued` clears the flag.
"""
from __future__ import annotations

import unittest

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id, utcnow
from app.generation.models import RunItemRecord
from app.generation.worker import _transition_item, handle_run_item


class TestWorkerPauseResumable(LibraryFixtures):
    def _create_pending_item(self, ctx, session, execution_mode: str) -> str:
        """A bare RunItem, bypassing draft/preview/submit -- the pause path in
        `handle_run_item` is rejected by `admit_external_step` before any
        ProviderAttempt/Job/SpendReservation lookup, so none of that
        scaffolding is needed here.
        """
        run_id = self._create_generation_run(ctx, session)
        item = RunItemRecord(
            id=new_id("ri"),
            workspace_id=ctx.workspace_id,
            generation_run_id=run_id,
            ordinal=0,
            combination_fingerprint="fp_test",
            semantic_fingerprint="sf_test",
            execution_mode=execution_mode,
            state="pending",
            revision=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(item)
        session.flush()
        return item.id

    def test_pause_sets_same_snapshot_resumable(self):
        """A recoverable StepRejection (BYOK, no credential evidence) pauses
        the item and sets `same_snapshot_resumable`."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            item_id = self._create_pending_item(ctx, s, execution_mode="byok")

            handle_run_item(s, ctx, item_id)

            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "paused")
            self.assertTrue(item.same_snapshot_resumable)

    def test_unrecoverable_rejection_leaves_resumable_false(self):
        """A non-recoverable StepRejection (sandbox, no adapter advertised)
        fails the item and leaves `same_snapshot_resumable` False."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            item_id = self._create_pending_item(ctx, s, execution_mode="sandbox")

            handle_run_item(s, ctx, item_id)

            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertFalse(item.same_snapshot_resumable)

    def test_transition_to_queued_clears_resumable(self):
        """Re-queuing a paused, resumable item clears `same_snapshot_resumable`."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            item_id = self._create_pending_item(ctx, s, execution_mode="byok")
            handle_run_item(s, ctx, item_id)

            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "paused")
            self.assertTrue(item.same_snapshot_resumable)

            _transition_item(s, ctx, item, state="queued")

            s.refresh(item)
            self.assertEqual(item.state, "queued")
            self.assertFalse(item.same_snapshot_resumable)


if __name__ == "__main__":
    unittest.main()
