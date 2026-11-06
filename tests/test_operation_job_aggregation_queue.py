"""The queue's half of Job -> Operation aggregation (spec §5.1-5.2, §16.8).

``tests/test_operation_job_aggregation.py`` pins the fold itself. This module
pins the wiring: every path that moves a Job to a terminal state must fold
its Operation, including the recovery sweeps that run outside a Worker fence
(lease expiry, authorization-epoch rejection, resource cancellation), and the
concurrency case where two Workers finish the last two Jobs at once.

The queue functions are exercised against the suite's real migrated SQLite
database so the Job state machine, the audit trail and the Operation history
are the production ones, not a hand-built subset.
"""
from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    JobRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.platform import jobs
from app.platform.job_outcomes import (
    JobFailed,
    JobObsolete,
    JobRetry,
    JobUncertain,
    settle_job_outcome,
)
from app.platform.operations import (
    OperationRecord,
    OperationState,
    OperationStateEvent,
    create_operation,
)
from app.platform.tenancy import TenantContext


class OperationAggregationQueueTests(LibraryFixtures):
    """One admitted Operation, a handful of sibling Jobs, the real queue."""

    def setUp(self) -> None:
        self.context = self.ctx(role="owner", actor="user_ops")
        self.owner = "worker-test"
        self._job_seq = 0

    # -- helpers ---------------------------------------------------------

    def _admit(self, *, contract_version: int = 1) -> str:
        with tenant_session(self.context) as session:
            operation = create_operation(
                session,
                command_type="localization.confirm",
                context=self.context,
                initial_state=(
                    OperationState.ADMITTED
                    if contract_version >= 1
                    else OperationState.QUEUED
                ),
                admission_contract_version=contract_version,
            )
            return operation.id

    def _enqueue(
        self,
        operation_id: str,
        *,
        resource_id: str = "res_1",
        safe_to_retry_after_crash: bool = False,
        max_attempts: int = 3,
    ) -> str:
        self._job_seq += 1
        with tenant_session(self.context) as session:
            job = JobRecord(
                id=new_id("job"),
                workspace_id=self.context.workspace_id,
                initiator_id=self.context.initiator_id,
                initiator_role="owner",
                authorization_epoch=self.context.authorization_epoch,
                type="localization.edit_attempt",
                resource_type="localization_item",
                resource_id=resource_id,
                payload_json={},
                idempotency_key=f"idem_{operation_id}_{self._job_seq}",
                state="queued",
                safe_to_retry_after_crash=safe_to_retry_after_crash,
                max_attempts=max_attempts,
                operation_id=operation_id,
                entitlement_version_at_enqueue=1,
                execution_contract_version=1,
            )
            session.add(job)
            session.flush()
            return job.id

    def _lease(self, job_id: str, *, running: bool = True) -> None:
        """Put one Job under this test's lease without racing the dispatcher."""
        now = utcnow()
        with tenant_session(self.context) as session:
            job = session.get(JobRecord, job_id)
            job.state = "leased"
            job.attempt += 1
            job.lease_owner = self.owner
            job.lease_expires_at = now + timedelta(seconds=60)
            job.heartbeat_at = now
            job.started_at = job.started_at or now
        if running:
            jobs.mark_running(self.context, job_id, self.owner)

    def _operation(self, operation_id: str) -> OperationRecord:
        with tenant_session(self.context) as session:
            return session.get(OperationRecord, operation_id)

    def _operation_states(self, operation_id: str) -> list[str]:
        with tenant_session(self.context) as session:
            return [
                event.to_state
                for event in session.scalars(
                    select(OperationStateEvent)
                    .where(OperationStateEvent.operation_id == operation_id)
                    .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
                )
            ]

    def _job_state(self, job_id: str) -> str:
        with tenant_session(self.context) as session:
            return session.get(JobRecord, job_id).state

    # Reads that must survive an authorization-epoch bump: once the
    # Workspace epoch moves, this test's own TenantContext is stale and
    # `tenant_session` refuses it -- which is the behaviour under test.
    def _job_state_unscoped(self, job_id: str) -> str:
        with platform_session() as session:
            return session.get(JobRecord, job_id).state

    def _operation_state_unscoped(self, operation_id: str) -> str:
        with platform_session() as session:
            return session.get(OperationRecord, operation_id).state

    def _operation_states_unscoped(self, operation_id: str) -> list[str]:
        with platform_session() as session:
            return [
                event.to_state
                for event in session.scalars(
                    select(OperationStateEvent)
                    .where(OperationStateEvent.operation_id == operation_id)
                    .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
                )
            ]

    # -- running promotion -----------------------------------------------

    def test_mark_running_promotes_the_admitted_operation_once(self):
        operation_id = self._admit()
        first = self._enqueue(operation_id)
        second = self._enqueue(operation_id)

        self._lease(first)
        self.assertEqual(
            self._operation(operation_id).state, OperationState.RUNNING.value
        )

        self._lease(second)
        self.assertEqual(
            self._operation_states(operation_id),
            [OperationState.ADMITTED.value, OperationState.RUNNING.value],
        )

    # -- the terminal paths ----------------------------------------------

    def test_every_sibling_succeeding_folds_the_operation(self):
        operation_id = self._admit()
        job_ids = [self._enqueue(operation_id) for _ in range(3)]

        for index, job_id in enumerate(job_ids):
            self._lease(job_id)
            jobs.succeed(self.context, job_id, self.owner)
            expected = (
                OperationState.SUCCEEDED.value
                if index == len(job_ids) - 1
                else OperationState.RUNNING.value
            )
            self.assertEqual(self._operation(operation_id).state, expected)

    def test_one_failed_sibling_folds_to_partially_succeeded(self):
        operation_id = self._admit()
        ok_job = self._enqueue(operation_id)
        bad_job = self._enqueue(operation_id)

        self._lease(ok_job)
        jobs.succeed(self.context, ok_job, self.owner)
        self._lease(bad_job)
        jobs.fail(
            self.context,
            bad_job,
            self.owner,
            code="provider_rejected",
            detail="controlled failure",
            retryable=False,
        )

        self.assertEqual(
            self._operation(operation_id).state,
            OperationState.PARTIALLY_SUCCEEDED.value,
        )

    def test_every_sibling_failing_folds_to_failed(self):
        operation_id = self._admit()
        job_ids = [self._enqueue(operation_id) for _ in range(2)]
        for job_id in job_ids:
            self._lease(job_id)
            jobs.fail(
                self.context,
                job_id,
                self.owner,
                code="provider_rejected",
                detail="",
                retryable=False,
            )

        self.assertEqual(
            self._operation(operation_id).state, OperationState.FAILED.value
        )

    def test_an_uncertain_sibling_folds_to_uncertain(self):
        operation_id = self._admit()
        ok_job = self._enqueue(operation_id)
        unknown_job = self._enqueue(operation_id)

        self._lease(ok_job)
        jobs.succeed(self.context, ok_job, self.owner)
        self._lease(unknown_job)
        jobs.mark_uncertain(
            self.context,
            unknown_job,
            self.owner,
            code="timeout",
            detail="no response before deadline",
        )

        self.assertEqual(
            self._operation(operation_id).state, OperationState.UNCERTAIN.value
        )

    def test_retry_wait_does_not_terminate_the_operation(self):
        operation_id = self._admit()
        job_id = self._enqueue(operation_id, max_attempts=3)

        self._lease(job_id)
        state = jobs.fail(
            self.context,
            job_id,
            self.owner,
            code="transient",
            detail="",
            retryable=True,
        )
        self.assertEqual(state, "retry_wait")
        self.assertEqual(
            self._operation(operation_id).state, OperationState.RUNNING.value
        )

        # The retry then succeeds, and only now does the command end.
        self._lease(job_id)
        jobs.succeed(self.context, job_id, self.owner)
        self.assertEqual(
            self._operation(operation_id).state, OperationState.SUCCEEDED.value
        )

    def test_exhausting_the_retry_budget_folds_to_failed(self):
        operation_id = self._admit()
        job_id = self._enqueue(operation_id, max_attempts=1)

        self._lease(job_id)
        state = jobs.fail(
            self.context,
            job_id,
            self.owner,
            code="transient",
            detail="",
            retryable=True,
        )
        self.assertEqual(state, "failed")
        self.assertEqual(
            self._operation(operation_id).state, OperationState.FAILED.value
        )

    def test_cancelling_a_resource_folds_its_operation(self):
        operation_id = self._admit()
        self._enqueue(operation_id, resource_id="item_cancel")
        self._enqueue(operation_id, resource_id="item_cancel")

        cancelled = jobs.cancel_resource(
            self.context, "localization_item", "item_cancel"
        )
        self.assertEqual(cancelled, 2)
        self.assertEqual(
            self._operation(operation_id).state, OperationState.CANCELLED.value
        )
        self.assertEqual(
            self._operation_states(operation_id),
            [OperationState.ADMITTED.value, OperationState.CANCELLED.value],
        )

    def test_cancelling_after_a_success_folds_to_partially_succeeded(self):
        operation_id = self._admit()
        done = self._enqueue(operation_id, resource_id="item_done")
        self._enqueue(operation_id, resource_id="item_pending")

        self._lease(done)
        jobs.succeed(self.context, done, self.owner)
        jobs.cancel_resource(self.context, "localization_item", "item_pending")

        self.assertEqual(
            self._operation(operation_id).state,
            OperationState.PARTIALLY_SUCCEEDED.value,
        )

    # -- the recovery sweeps ---------------------------------------------

    def test_expired_lease_on_an_unsafe_job_folds_to_uncertain(self):
        operation_id = self._admit()
        job_id = self._enqueue(operation_id)
        self._lease(job_id)

        with tenant_session(self.context) as session:
            session.get(JobRecord, job_id).lease_expires_at = utcnow() - timedelta(
                seconds=5
            )

        jobs.recover_expired_jobs()

        self.assertEqual(self._job_state(job_id), "uncertain")
        self.assertEqual(
            self._operation(operation_id).state, OperationState.UNCERTAIN.value
        )

    def test_expired_lease_on_a_retryable_job_leaves_the_operation_running(self):
        operation_id = self._admit()
        job_id = self._enqueue(operation_id, safe_to_retry_after_crash=True)
        self._lease(job_id)

        with tenant_session(self.context) as session:
            session.get(JobRecord, job_id).lease_expires_at = utcnow() - timedelta(
                seconds=5
            )

        jobs.recover_expired_jobs()

        self.assertEqual(self._job_state(job_id), "queued")
        self.assertEqual(
            self._operation(operation_id).state, OperationState.RUNNING.value
        )

    def test_authorization_epoch_rejection_folds_to_failed(self):
        """A revoked epoch retires the Jobs before any Worker claims one.
        The command failed with nothing dispatched -- ``admitted -> failed``,
        and never a fabricated ``running``."""
        operation_id = self._admit()
        job_id = self._enqueue(operation_id)

        with platform_session() as session:
            workspace = session.get(WorkspaceRecord, self.context.workspace_id)
            workspace.authorization_epoch = workspace.authorization_epoch + 1

        jobs.recover_expired_jobs()

        self.assertEqual(self._job_state_unscoped(job_id), "failed")
        self.assertEqual(
            self._operation_state_unscoped(operation_id), OperationState.FAILED.value
        )
        self.assertEqual(
            self._operation_states_unscoped(operation_id),
            [OperationState.ADMITTED.value, OperationState.FAILED.value],
        )

    def test_reconcile_sweep_folds_an_operation_left_behind_by_sql(self):
        """The PostgreSQL dispatcher retires Jobs inside its claim function,
        with no Python transition to fold from. The sweep is the reconciler
        for exactly that: here the Job is moved directly, bypassing the
        queue API the way that SQL does."""
        operation_id = self._admit()
        job_id = self._enqueue(operation_id)
        with tenant_session(self.context) as session:
            job = session.get(JobRecord, job_id)
            job.state = "failed"
            job.finished_at = utcnow()

        self.assertEqual(
            self._operation(operation_id).state, OperationState.ADMITTED.value
        )
        moved = jobs.reconcile_admitted_operations()
        self.assertGreaterEqual(moved, 1)
        self.assertEqual(
            self._operation(operation_id).state, OperationState.FAILED.value
        )

        # Idempotent: a second sweep finds nothing left to move here.
        self.assertEqual(
            self._operation(operation_id).state, OperationState.FAILED.value
        )
        jobs.reconcile_admitted_operations()
        self.assertEqual(
            self._operation_states(operation_id),
            [OperationState.ADMITTED.value, OperationState.FAILED.value],
        )

    # -- redelivery and concurrency --------------------------------------

    def test_redelivered_terminal_transition_appends_no_second_event(self):
        operation_id = self._admit()
        job_id = self._enqueue(operation_id)
        self._lease(job_id)
        jobs.succeed(self.context, job_id, self.owner)

        before = self._operation_states(operation_id)
        # A redelivery of the same Job finds the lease gone and refuses --
        # what matters is that no second terminal Operation event appears.
        with self.assertRaises(RuntimeError):
            jobs.succeed(self.context, job_id, self.owner)
        self.assertEqual(self._operation_states(operation_id), before)

    def test_two_workers_finishing_the_last_jobs_fold_exactly_once(self):
        """Without the row lock both Workers read the other's Job as still
        running, neither folds, and the Operation stays ``running`` with
        every Job terminal. With it, exactly one fold happens."""
        operation_id = self._admit()
        first = self._enqueue(operation_id)
        second = self._enqueue(operation_id)
        self._lease(first)
        self._lease(second)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def finish(job_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                jobs.succeed(self.context, job_id, self.owner)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=finish, args=(first,)),
            threading.Thread(target=finish, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(
            self._operation(operation_id).state, OperationState.SUCCEEDED.value
        )
        self.assertEqual(
            self._operation_states(operation_id).count(
                OperationState.SUCCEEDED.value
            ),
            1,
        )

    # -- legacy Operations -----------------------------------------------

    def test_legacy_queued_operations_are_untouched_by_the_queue(self):
        operation_id = self._admit(contract_version=0)
        job_id = self._enqueue(operation_id)

        self._lease(job_id)
        self.assertEqual(
            self._operation(operation_id).state, OperationState.QUEUED.value
        )
        jobs.succeed(self.context, job_id, self.owner)
        self.assertEqual(
            self._operation(operation_id).state, OperationState.QUEUED.value
        )
        self.assertEqual(
            self._operation_states(operation_id), [OperationState.QUEUED.value]
        )


class HandlerOutcomeSettlementTests(LibraryFixtures):
    """``settle_job_outcome`` is the only place the outcome table lives."""

    def setUp(self) -> None:
        self.context = self.ctx(role="owner", actor="user_outcome")
        self.owner = "worker-outcome"

    def _running_job(self, operation_id: str | None = None) -> str:
        now = utcnow()
        with tenant_session(self.context) as session:
            job = JobRecord(
                id=new_id("job"),
                workspace_id=self.context.workspace_id,
                initiator_id=self.context.initiator_id,
                initiator_role="owner",
                type="localization.edit_attempt",
                resource_type="localization_item",
                resource_id="item_outcome",
                payload_json={},
                idempotency_key=new_id("idem"),
                state="running",
                max_attempts=3,
                attempt=1,
                lease_owner=self.owner,
                lease_expires_at=now + timedelta(seconds=60),
                heartbeat_at=now,
                started_at=now,
                operation_id=operation_id,
                entitlement_version_at_enqueue=1 if operation_id else None,
                execution_contract_version=1 if operation_id else 0,
            )
            session.add(job)
            session.flush()
            return job.id

    def _job(self, job_id: str) -> JobRecord:
        with tenant_session(self.context) as session:
            return session.get(JobRecord, job_id)

    def test_no_signal_succeeds_the_job(self):
        job_id = self._running_job()
        settled = settle_job_outcome(self.context, job_id, self.owner, None)
        self.assertEqual(settled.job_state, "succeeded")
        self.assertEqual(self._job(job_id).state, "succeeded")

    def test_obsolete_closes_the_job_as_succeeded_and_audits_why(self):
        job_id = self._running_job()
        settled = settle_job_outcome(
            self.context,
            job_id,
            self.owner,
            JobObsolete("batch_terminal", "cancelled before dispatch"),
        )
        self.assertEqual(settled.job_state, "succeeded")
        self.assertEqual(self._job(job_id).state, "succeeded")

        from app.adapters.persistence.models import AuditEventRecord

        with tenant_session(self.context) as session:
            kinds = [
                event.event_type
                for event in session.scalars(
                    select(AuditEventRecord).where(
                        AuditEventRecord.resource_type == "job",
                        AuditEventRecord.resource_id == job_id,
                    )
                )
            ]
        self.assertIn("job.obsolete", kinds)

    def test_failed_signal_fails_the_job_without_a_retry(self):
        job_id = self._running_job()
        settled = settle_job_outcome(
            self.context, job_id, self.owner, JobFailed("provider_rejected", "nope")
        )
        self.assertEqual(settled.job_state, "failed")
        self.assertEqual(self._job(job_id).error_code, "provider_rejected")

    def test_retry_signal_schedules_a_retry(self):
        job_id = self._running_job()
        settled = settle_job_outcome(
            self.context,
            job_id,
            self.owner,
            JobRetry("transient", "try again", retry_after_seconds=1),
        )
        self.assertEqual(settled.job_state, "retry_wait")
        self.assertIsNotNone(self._job(job_id).next_attempt_at)

    def test_uncertain_signal_never_enters_the_retry_queue(self):
        job_id = self._running_job()
        settled = settle_job_outcome(
            self.context, job_id, self.owner, JobUncertain("timeout", "no response")
        )
        self.assertEqual(settled.job_state, "uncertain")
        job = self._job(job_id)
        self.assertEqual(job.state, "uncertain")
        self.assertIsNone(job.next_attempt_at)
