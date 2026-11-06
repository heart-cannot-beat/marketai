"""A Worker-side fault must reach the RunItem, not only the Job.

Before this regression test, everything that raised in
``WorkerRuntime._generation_run_item_legacy`` before ``handle_run_item``
reached a terminal transition -- the Mock materialization, its object-store
commit, a stale-revision CAS -- settled only the Job as ``failed``. Jobs are
invisible to the Run page, so the Run sat at ``running`` with every item
``pending`` forever and Recovery offered nothing to retry.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import GenerationRunRecord, new_id
from app.generation.models import RunItemRecord
from app.platform.blob_store import BlobStoreError
from app.platform.jobs import JobRecord
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs


class TestHandlerFaultProjection(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def _submit_mock_run(self, ctx):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

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
        return result["generation_run_id"]

    def test_commit_failure_marks_the_item_failed_and_folds_the_run(self):
        from app.worker_runtime import WorkerRuntime

        ctx = self.ctx
        run_id = self._submit_mock_run(ctx)

        async def refuse_commit(*_args, **_kwargs):
            raise BlobStoreError(
                "Railway Bucket commit generated artifact failed (EndpointConnectionError)"
            )

        # Drain only this Workspace's Jobs: earlier tests' leftovers are
        # deferred first so the refusing store never touches them.
        quarantine_foreign_jobs(ctx.workspace_id)
        runtime = WorkerRuntime(owner="fault-worker")
        with patch("app.generation.worker.materialize_mock_output", refuse_commit):
            while asyncio.run(runtime.run_once()):
                pass

        with tenant_session(ctx) as s:
            jobs = s.execute(
                select(JobRecord).where(
                    JobRecord.workspace_id == ctx.workspace_id,
                    JobRecord.type == "generation.run_item",
                )
            ).scalars().all()
            self.assertTrue(jobs)
            self.assertEqual({job.state for job in jobs}, {"failed"})
            self.assertEqual({job.error_code for job in jobs}, {"generation_run_item_failed"})

            items = {
                item.id: item
                for item in s.execute(
                    select(RunItemRecord).where(
                        RunItemRecord.generation_run_id == run_id,
                        RunItemRecord.workspace_id == ctx.workspace_id,
                    )
                ).scalars()
            }
            self.assertEqual(len(items), len(jobs))
            for job in jobs:
                item = items[job.payload_json["run_item_id"]]
                self.assertEqual(item.state, "failed")
                self.assertIn("generation_run_item_failed: BlobStoreError", item.error_summary)
                self.assertIn("EndpointConnectionError", item.error_summary)
                self.assertFalse(item.same_snapshot_resumable)

            run = s.get(GenerationRunRecord, run_id)
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.failed, len(items))
            self.assertEqual(run.done, 0)

    def test_a_settled_item_is_left_alone(self):
        from app.generation.worker import fail_run_item_after_handler_fault
        from app.generation.worker import handle_run_item

        ctx = self.ctx
        run_id = self._submit_mock_run(ctx)

        with tenant_session(ctx) as s:
            item = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == run_id,
                    RunItemRecord.workspace_id == ctx.workspace_id,
                )
            ).scalars().first()
            handle_run_item(s, ctx, item.id)
            s.expire_all()
            self.assertEqual(s.get(RunItemRecord, item.id).state, "qc_pending")

            fail_run_item_after_handler_fault(
                s, ctx, item.id, error_summary="generation_run_item_failed: late fault"
            )
            s.expire_all()
            settled = s.get(RunItemRecord, item.id)
            self.assertEqual(settled.state, "qc_pending")
            self.assertEqual(settled.error_summary, "")
