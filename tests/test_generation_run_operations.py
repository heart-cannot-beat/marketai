"""A Run's command Operations must settle with the Run.

Before ``app.generation.run_operations`` the ``submit`` Operation stayed
``running`` after its Run had succeeded and ``cancel``/``reconcile`` stayed
``queued`` forever: nothing in the domain closed them, and the generic Job
aggregator skips ``admission_contract_version`` 0 Operations by design. The
Run page (``RunProgressSource.ts``) only stops polling once the Run *and* its
latest command Operation are terminal, so every open Run page polled at the
fast cadence indefinitely under a "⚙️ Running" strip.
"""
from __future__ import annotations

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    GenerationRunRecord,
    JobRecord,
    OperationRecord,
    OperationStateEventRecord,
    new_id,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.generation.draft import create_draft
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.preview import create_preview
from app.generation.qc import run_qc, run_qc_with_blocking_failure
from app.generation.recovery_preview import consume_recovery_preview, create_recovery_preview
from app.generation.run_commands import cancel_run, reconcile_run
from app.generation.run_queries import get_run
from app.generation.submission import submit_generation
from app.generation.worker import handle_run_item, handle_run_item_dispatch_only
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs


class _RunOperationsBase(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def _submit(self, s, *, variants: int = 1) -> tuple[str, str]:
        ctx = self.ctx
        draft = create_draft(
            s, ctx,
            product_id=self._product_id(ctx, s),
            provider="mock", model="mock-v1",
            execution_mode="mock",
            combination_strategy={"kind": "full", "planner_version": "v1"},
            format_config={
                "aspect_ratio": "1:1",
                "image_size": "1024x1024",
                "variants_per_combination": variants,
            },
        )
        preview = create_preview(s, ctx, draft.id, draft.revision)
        result = submit_generation(
            s, ctx,
            preview_id=preview.id,
            preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision,
            idempotency_key=new_id("ik"),
        )
        return result["generation_run_id"], result["operation_id"]

    def _items(self, s, run_id: str) -> list[RunItemRecord]:
        return s.execute(
            select(RunItemRecord)
            .where(
                RunItemRecord.generation_run_id == run_id,
                RunItemRecord.workspace_id == self.ctx.workspace_id,
            )
            .order_by(RunItemRecord.ordinal)
        ).scalars().all()

    def _produced_version_id(self, s, item_id: str) -> str:
        """The version the item's *latest* dispatch produced (a retry adds one)."""
        return s.execute(
            select(RunItemAssetVersionBindingRecord.asset_version_id)
            .where(
                RunItemAssetVersionBindingRecord.run_item_id == item_id,
                RunItemAssetVersionBindingRecord.kind == "produced",
            )
            .order_by(
                RunItemAssetVersionBindingRecord.created_at.desc(),
                RunItemAssetVersionBindingRecord.id.desc(),
            )
        ).scalars().first()

    def _operation_state(self, s, operation_id: str) -> str:
        s.expire_all()
        return s.get(OperationRecord, operation_id).state

    def _operation_history(self, s, operation_id: str) -> list[str]:
        return list(
            s.execute(
                select(OperationStateEventRecord.to_state)
                .where(OperationStateEventRecord.operation_id == operation_id)
                .order_by(OperationStateEventRecord.created_at, OperationStateEventRecord.id)
            ).scalars()
        )

    def _run_status(self, s, run_id: str) -> str:
        s.expire_all()
        return s.get(GenerationRunRecord, run_id).status


class TestSubmitOperationFollowsTheRun(_RunOperationsBase):
    def test_submit_operation_settles_succeeded_with_the_run(self):
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, operation_id = self._submit(s)
            self.assertEqual(self._operation_state(s, operation_id), "running")

            items = self._items(s, run_id)
            self.assertTrue(items)
            for item in items:
                handle_run_item(s, ctx, item.id)
            # Dispatch alone leaves every item ``qc_pending``: the Run is
            # still running, and so is the submit Operation.
            self.assertEqual(self._run_status(s, run_id), "running")
            self.assertEqual(self._operation_state(s, operation_id), "running")

            for item in items:
                run_qc(s, ctx, self._produced_version_id(s, item.id), item.id)

            self.assertEqual(self._run_status(s, run_id), "succeeded")
            self.assertEqual(self._operation_state(s, operation_id), "succeeded")
            self.assertEqual(
                self._operation_history(s, operation_id),
                ["queued", "running", "succeeded"],
            )

    def test_submit_operation_settles_partially_succeeded(self):
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, operation_id = self._submit(s, variants=2)
            items = self._items(s, run_id)
            self.assertGreater(len(items), 1)
            for item in items:
                handle_run_item(s, ctx, item.id)

            first, *rest = items
            run_qc_with_blocking_failure(
                s, ctx, self._produced_version_id(s, first.id), first.id
            )
            for item in rest:
                run_qc(s, ctx, self._produced_version_id(s, item.id), item.id)

            self.assertEqual(self._run_status(s, run_id), "partially_succeeded")
            self.assertEqual(self._operation_state(s, operation_id), "partially_succeeded")

    def test_uncertain_run_parks_the_operation_until_it_resumes(self):
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, operation_id = self._submit(s, variants=2)
            items = self._items(s, run_id)

            # A crash between dispatch and response leaves the item (and the
            # Run) ``uncertain``: the Operation must say so rather than keep
            # claiming "running" work.
            handle_run_item_dispatch_only(s, ctx, items[0].id)
            self.assertEqual(self._run_status(s, run_id), "uncertain")
            self.assertEqual(self._operation_state(s, operation_id), "uncertain")

            # Reconciliation resolves the item; the next projection takes the
            # Operation back to ``running`` (the one backward edge spec §5.2
            # allows) and on to the Run's final outcome.
            from app.generation.worker import _sync_run_state_best_effort, _transition_item

            s.expire_all()
            _transition_item(s, ctx, s.get(RunItemRecord, items[0].id), state="failed")
            _sync_run_state_best_effort(s, ctx, run_id)
            self.assertEqual(self._run_status(s, run_id), "running")
            self.assertEqual(self._operation_state(s, operation_id), "running")

            for item in items[1:]:
                handle_run_item(s, ctx, item.id)
                run_qc(s, ctx, self._produced_version_id(s, item.id), item.id)
            self.assertEqual(self._run_status(s, run_id), "partially_succeeded")
            self.assertEqual(self._operation_state(s, operation_id), "partially_succeeded")
            self.assertEqual(
                self._operation_history(s, operation_id),
                ["queued", "running", "uncertain", "running", "partially_succeeded"],
            )


class TestRetryReopensUnderItsOwnOperation(_RunOperationsBase):
    def test_retry_operation_settles_while_the_failed_submit_keeps_its_history(self):
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, submit_operation_id = self._submit(s)
            items = self._items(s, run_id)
            for item in items:
                handle_run_item(s, ctx, item.id)
                run_qc_with_blocking_failure(
                    s, ctx, self._produced_version_id(s, item.id), item.id
                )
            self.assertEqual(self._run_status(s, run_id), "failed")
            self.assertEqual(self._operation_state(s, submit_operation_id), "failed")

            run = get_run(s, ctx, run_id)
            preview = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="all_eligible_failed",
                selection_item_ids=[],
                expected_run_revision=run.revision,
                idempotency_key=new_id("rp"),
            )
            retry = consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=preview["id"],
                preview_hash=preview["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key=new_id("ik"),
            )
            retry_operation_id = retry["operation_id"]
            self.assertEqual(self._operation_state(s, retry_operation_id), "running")

            for item in self._items(s, run_id):
                handle_run_item(s, ctx, item.id)
                run_qc(s, ctx, self._produced_version_id(s, item.id), item.id)

            self.assertEqual(self._run_status(s, run_id), "succeeded")
            self.assertEqual(self._operation_state(s, retry_operation_id), "succeeded")
            # Terminal history is immutable: the first attempt stays failed.
            self.assertEqual(self._operation_state(s, submit_operation_id), "failed")

    def test_retried_item_dispatches_as_the_next_attempt_under_its_newest_job(self):
        """``provider_call_attempts`` is unique on (workspace, semantic key, attempt_no).

        The first dispatch is attempt 0; a Recovery retry of the same RunItem
        must append attempt 1 -- and bind it to the Job the retry enqueued --
        instead of colliding with attempt 0 and failing the item again.
        """
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, _ = self._submit(s)
            item = self._items(s, run_id)[0]
            handle_run_item(s, ctx, item.id)
            run_qc_with_blocking_failure(
                s, ctx, self._produced_version_id(s, item.id), item.id
            )

            run = get_run(s, ctx, run_id)
            preview = create_recovery_preview(
                s, ctx, run.id,
                command="retry_failed_items",
                selection_kind="all_eligible_failed",
                selection_item_ids=[],
                expected_run_revision=run.revision,
                idempotency_key=new_id("rp"),
            )
            consume_recovery_preview(
                s, ctx,
                run_id=run.id,
                command="retry_failed_items",
                preview_id=preview["id"],
                preview_hash=preview["preview_hash"],
                expected_run_revision=run.revision,
                idempotency_key=new_id("ik"),
            )
            handle_run_item(s, ctx, item.id)

            attempts = s.execute(
                select(ProviderCallAttemptRecord)
                .where(
                    ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
                    ProviderCallAttemptRecord.run_item_id == item.id,
                )
                .order_by(ProviderCallAttemptRecord.attempt_no)
            ).scalars().all()
            self.assertEqual([a.attempt_no for a in attempts], [0, 1])
            self.assertEqual(len({a.call_semantic_key_hash for a in attempts}), 1)

            jobs = s.execute(
                select(JobRecord)
                .where(
                    JobRecord.workspace_id == ctx.workspace_id,
                    JobRecord.resource_type == "run_item",
                    JobRecord.resource_id == item.id,
                )
                .order_by(JobRecord.created_at, JobRecord.id)
            ).scalars().all()
            self.assertEqual(len(jobs), 2)
            self.assertEqual(attempts[0].job_id, jobs[0].id)
            self.assertEqual(attempts[1].job_id, jobs[1].id)
            # One SpendReservation per RunItem, reused across its attempts.
            self.assertEqual(attempts[0].spend_reservation_id, attempts[1].spend_reservation_id)


class TestSynchronousCommandsSettleImmediately(_RunOperationsBase):
    def test_cancel_settles_its_own_and_the_submit_operation(self):
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, submit_operation_id = self._submit(s)
            run = get_run(s, ctx, run_id)

            result = cancel_run(
                s, ctx,
                run_id=run_id,
                expected_revision=run.revision,
                idempotency_key=new_id("ik"),
            )
            cancel_operation_id = result["operation_id"]

            self.assertEqual(self._run_status(s, run_id), "cancelled")
            self.assertEqual(self._operation_state(s, cancel_operation_id), "succeeded")
            self.assertEqual(
                self._operation_history(s, cancel_operation_id),
                ["queued", "running", "succeeded"],
            )
            self.assertEqual(self._operation_state(s, submit_operation_id), "cancelled")

    def test_reconcile_settles_its_operation(self):
        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, _ = self._submit(s)
            run = get_run(s, ctx, run_id)

            targets = [item.id for item in self._items(s, run_id)]
            key, revision = new_id("ik"), run.revision
            result = reconcile_run(
                s, ctx, run_id=run_id, expected_revision=revision,
                uncertain_targets=targets,
                operator_note="checked the provider console: nothing was billed",
                idempotency_key=key,
            )
            replay = reconcile_run(
                s, ctx, run_id=run_id, expected_revision=revision,
                uncertain_targets=targets,
                operator_note="checked the provider console: nothing was billed",
                idempotency_key=key,
            )
            self.assertEqual(result, replay)
            self.assertEqual(get_run(s, ctx, run_id).revision, revision + 1)
            self.assertEqual(self._operation_state(s, result["operation_id"]), "succeeded")

    def test_settled_operations_are_left_alone_by_later_projections(self):
        from app.generation.run_operations import settle_run_command_operations

        ctx = self.ctx
        with tenant_session(ctx) as s:
            run_id, submit_operation_id = self._submit(s)
            run = get_run(s, ctx, run_id)
            cancel_run(
                s, ctx,
                run_id=run_id,
                expected_revision=run.revision,
                idempotency_key=new_id("ik"),
            )
            self.assertEqual(self._operation_state(s, submit_operation_id), "cancelled")

            # A late RunItem completion recomputing the Run as succeeded must
            # not append a second terminal event to the cancelled Operation.
            moved = settle_run_command_operations(
                s, ctx, run_id, "succeeded", actor="late-worker"
            )
            self.assertEqual(moved, [])
            self.assertEqual(self._operation_state(s, submit_operation_id), "cancelled")
            self.assertEqual(
                self._operation_history(s, submit_operation_id),
                ["queued", "running", "cancelled"],
            )
