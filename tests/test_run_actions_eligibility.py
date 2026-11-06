"""Tests for run_actions eligibility with real item queries."""
import unittest

from sqlalchemy import update

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import utcnow
from app.generation.draft import create_draft
from app.generation.models import RunItemRecord
from app.generation.preview import create_preview
from app.generation.run_queries import run_actions, get_run
from app.generation.submission import submit_generation


class TestRunActionsEligibility(LibraryFixtures):
    def _create_run(self, ctx, s):
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
            idempotency_key=f"submit-actions-{id(self)}",
        )
        return result["generation_run_id"]

    def _action_map(self, actions):
        return {a["action"]: a for a in [a.model_dump() if hasattr(a, 'model_dump') else a for a in actions]}

    def test_resume_paused_eligible_when_resumable_item_exists(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id = self._create_run(ctx, s)
            items = s.query(RunItemRecord).filter_by(
                generation_run_id=run_id).all()
            s.execute(
                update(RunItemRecord).where(RunItemRecord.id == items[0].id)
                .values(state="paused", same_snapshot_resumable=True,
                        revision=items[0].revision + 1, updated_at=utcnow())
            )
            s.flush()

            run = get_run(s, ctx, run_id)
            actions = run_actions(s, ctx, run)
            am = self._action_map(actions)
            self.assertTrue(am["resume_paused"]["allowed"])

    def test_resume_paused_not_eligible_when_paused_but_not_resumable(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id = self._create_run(ctx, s)
            items = s.query(RunItemRecord).filter_by(
                generation_run_id=run_id).all()
            s.execute(
                update(RunItemRecord).where(RunItemRecord.id == items[0].id)
                .values(state="paused", same_snapshot_resumable=False,
                        revision=items[0].revision + 1, updated_at=utcnow())
            )
            s.flush()

            run = get_run(s, ctx, run_id)
            actions = run_actions(s, ctx, run)
            am = self._action_map(actions)
            self.assertFalse(am["resume_paused"]["allowed"])

    def test_reconcile_eligible_when_uncertain_item_exists(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id = self._create_run(ctx, s)
            items = s.query(RunItemRecord).filter_by(
                generation_run_id=run_id).all()
            s.execute(
                update(RunItemRecord).where(RunItemRecord.id == items[0].id)
                .values(state="uncertain",
                        revision=items[0].revision + 1, updated_at=utcnow())
            )
            s.flush()

            run = get_run(s, ctx, run_id)
            actions = run_actions(s, ctx, run)
            am = self._action_map(actions)
            self.assertTrue(am["reconcile"]["allowed"])

    def test_reconcile_not_eligible_when_no_uncertain_items(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id = self._create_run(ctx, s)
            run = get_run(s, ctx, run_id)
            actions = run_actions(s, ctx, run)
            am = self._action_map(actions)
            self.assertFalse(am["reconcile"]["allowed"])
