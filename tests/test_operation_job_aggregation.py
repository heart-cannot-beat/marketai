"""Job -> Operation aggregation (spec §5.1-5.2, §16.8).

An admitted Operation is the user-facing half of a command whose real work
is a set of sibling Jobs. Until those Jobs are folded back into it, the
Operation is a projection of nothing: it stays ``admitted`` forever while
every Job finishes, and a polling browser never learns the command ended.

These tests pin the fold itself -- the state machine's new ``admitted``
entry state, the outcome table, its concurrency behaviour, and the
guarantee that legacy ``queued`` Operations are left to their own handlers.
The queue-level wiring (``jobs.succeed``/``fail``/``mark_uncertain``/
``cancel_resource``/recovery) is exercised in
``tests/test_operation_job_aggregation_queue.py``.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    JobRecord,
    WorkspaceRecord,
    utcnow,
)
from app.platform.operations import (
    JOB_TERMINAL_STATES,
    OperationRecord,
    OperationState,
    OperationStateEvent,
    aggregate_operation_from_jobs,
    create_operation,
    derive_operation_outcome,
    start_operation_for_job,
    transition_operation,
)
from app.platform.tenancy import TenantContext, apply_to_session


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


class TestDeriveOperationOutcome(unittest.TestCase):
    """The outcome table, as a pure function of sibling Job states."""

    def test_no_jobs_is_undecided(self):
        self.assertIsNone(derive_operation_outcome([]))

    def test_any_non_terminal_job_is_undecided(self):
        for in_flight in ("pending", "queued", "leased", "running", "retry_wait"):
            with self.subTest(state=in_flight):
                self.assertIsNone(
                    derive_operation_outcome(["succeeded", in_flight])
                )

    def test_retry_wait_is_not_a_terminal_job_state(self):
        """A Job waiting to be retried has not finished. Folding its
        Operation here would report an outcome the command has not
        reached, and the next attempt could still succeed."""
        self.assertNotIn("retry_wait", JOB_TERMINAL_STATES)

    def test_all_succeeded_is_succeeded(self):
        self.assertIs(
            derive_operation_outcome(["succeeded", "succeeded", "succeeded"]),
            OperationState.SUCCEEDED,
        )

    def test_mixed_success_and_failure_is_partially_succeeded(self):
        self.assertIs(
            derive_operation_outcome(["succeeded", "failed"]),
            OperationState.PARTIALLY_SUCCEEDED,
        )

    def test_success_alongside_cancellation_is_partially_succeeded(self):
        self.assertIs(
            derive_operation_outcome(["succeeded", "cancelled"]),
            OperationState.PARTIALLY_SUCCEEDED,
        )

    def test_all_failed_is_failed(self):
        self.assertIs(
            derive_operation_outcome(["failed", "failed"]), OperationState.FAILED
        )

    def test_failure_alongside_cancellation_is_failed(self):
        self.assertIs(
            derive_operation_outcome(["failed", "cancelled"]), OperationState.FAILED
        )

    def test_all_cancelled_is_cancelled(self):
        self.assertIs(
            derive_operation_outcome(["cancelled", "cancelled"]),
            OperationState.CANCELLED,
        )

    def test_uncertain_outranks_every_other_terminal_state(self):
        """spec §16.8: an ambiguous paid call is neither a success nor a
        failure. Reporting ``partially_succeeded`` would tell the user the
        rest is settled while a reconciliation pass may still move it."""
        for siblings in (
            ["uncertain"],
            ["succeeded", "uncertain"],
            ["failed", "uncertain"],
            ["cancelled", "uncertain"],
            ["succeeded", "failed", "cancelled", "uncertain"],
        ):
            with self.subTest(siblings=siblings):
                self.assertIs(
                    derive_operation_outcome(siblings), OperationState.UNCERTAIN
                )


class AggregationTestCase(unittest.TestCase):
    """Real models against a scratch SQLite database (see test_operations)."""

    def setUp(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-op-aggregation-"))
        self.engine = create_engine(
            _sqlite_url(self._tmp_dir / "aggregation.db"),
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                WorkspaceRecord.__table__,
                OperationRecord.__table__,
                OperationStateEvent.__table__,
                JobRecord.__table__,
            ],
        )
        self.session = Session(bind=self.engine)
        self.workspace_id = "ws_aggregation"
        self.session.add(
            WorkspaceRecord(
                id=self.workspace_id, name="Aggregation", created_at=utcnow()
            )
        )
        self.session.commit()
        self.context = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_agg"
        )
        apply_to_session(self.session, self.context)
        self._job_seq = 0

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _admit(self, *, contract_version: int = 1) -> OperationRecord:
        operation = create_operation(
            self.session,
            command_type="localization.confirm",
            context=self.context,
            initial_state=(
                OperationState.ADMITTED
                if contract_version >= 1
                else OperationState.QUEUED
            ),
            admission_contract_version=contract_version,
        )
        self.session.commit()
        return operation

    def _job(self, operation: OperationRecord, state: str) -> JobRecord:
        self._job_seq += 1
        job = JobRecord(
            id=f"job_agg_{self._job_seq}",
            workspace_id=self.workspace_id,
            initiator_id="user_agg",
            initiator_role="owner",
            type="localization.edit_attempt",
            payload_json={},
            idempotency_key=f"idem_{self._job_seq}",
            state=state,
            operation_id=operation.id,
            entitlement_version_at_enqueue=1,
            execution_contract_version=1,
        )
        self.session.add(job)
        self.session.commit()
        return job

    def _aggregate(self, operation: OperationRecord):
        return aggregate_operation_from_jobs(
            self.session, operation.id, actor="system:worker", context=self.context
        )

    def _events(self, operation: OperationRecord) -> list[OperationStateEvent]:
        return list(
            self.session.scalars(
                select(OperationStateEvent)
                .where(OperationStateEvent.operation_id == operation.id)
                .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
            )
        )

    # -- the admitted entry state ----------------------------------------

    def test_admission_persists_its_own_creation_event(self):
        operation = self._admit()
        events = self._events(operation)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].from_state)
        self.assertEqual(events[0].to_state, OperationState.ADMITTED.value)

    def test_first_job_promotes_admitted_to_running_once(self):
        operation = self._admit()
        self._job(operation, "running")
        self._job(operation, "queued")

        self.assertIsNotNone(
            start_operation_for_job(
                self.session, operation.id, actor="system:worker", context=self.context
            )
        )
        self.session.commit()
        self.assertEqual(operation.state, OperationState.RUNNING.value)

        # The second Job of the same command changes nothing.
        self.assertIsNone(
            start_operation_for_job(
                self.session, operation.id, actor="system:worker", context=self.context
            )
        )
        to_states = [event.to_state for event in self._events(operation)]
        self.assertEqual(
            to_states, [OperationState.ADMITTED.value, OperationState.RUNNING.value]
        )

    # -- the fold --------------------------------------------------------

    def test_multi_job_success_folds_to_succeeded(self):
        operation = self._admit()
        for _ in range(3):
            self._job(operation, "succeeded")

        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.SUCCEEDED.value)

    def test_success_without_a_running_transition_records_the_dispatch(self):
        """A Worker crash between ``jobs.mark_running`` and the Operation
        promotion leaves an ``admitted`` Operation with finished Jobs. The
        fold must not invent ``admitted -> succeeded``: the Jobs are
        evidence the work ran, so the history gets both events."""
        operation = self._admit()
        self._job(operation, "succeeded")

        self._aggregate(operation)
        self.session.commit()

        self.assertEqual(operation.state, OperationState.SUCCEEDED.value)
        self.assertEqual(
            [event.to_state for event in self._events(operation)],
            [
                OperationState.ADMITTED.value,
                OperationState.RUNNING.value,
                OperationState.SUCCEEDED.value,
            ],
        )

    def test_partial_success_folds_to_partially_succeeded(self):
        operation = self._admit()
        self._job(operation, "succeeded")
        self._job(operation, "failed")

        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.PARTIALLY_SUCCEEDED.value)

    def test_total_failure_folds_to_failed_without_claiming_a_dispatch(self):
        """Every Job rejected before any Worker claimed one (a stale
        authorization epoch) is a failure with nothing having run --
        ``admitted -> failed`` directly, no fabricated ``running``."""
        operation = self._admit()
        self._job(operation, "failed")
        self._job(operation, "failed")

        self._aggregate(operation)
        self.session.commit()

        self.assertEqual(operation.state, OperationState.FAILED.value)
        self.assertEqual(
            [event.to_state for event in self._events(operation)],
            [OperationState.ADMITTED.value, OperationState.FAILED.value],
        )

    def test_all_cancelled_folds_to_cancelled(self):
        operation = self._admit()
        self._job(operation, "cancelled")
        self._job(operation, "cancelled")

        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.CANCELLED.value)

    def test_uncertain_job_folds_to_uncertain(self):
        operation = self._admit()
        self._job(operation, "succeeded")
        self._job(operation, "uncertain")

        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.UNCERTAIN.value)

    def test_retry_wait_never_terminates_the_operation(self):
        operation = self._admit()
        self._job(operation, "succeeded")
        retrying = self._job(operation, "retry_wait")

        self.assertIsNone(self._aggregate(operation))
        self.session.commit()
        self.assertEqual(operation.state, OperationState.ADMITTED.value)

        # ... and the fold lands as soon as the retry itself finishes.
        retrying.state = "succeeded"
        self.session.commit()
        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.SUCCEEDED.value)

    def test_redelivered_aggregation_is_idempotent(self):
        """A redelivered Job runs the fold again. The second pass must not
        append a second terminal event -- the history is the evidence, and
        two ``succeeded`` rows would claim the command ended twice."""
        operation = self._admit()
        self._job(operation, "succeeded")

        self._aggregate(operation)
        self.session.commit()
        before = len(self._events(operation))

        for _ in range(3):
            self.assertIsNone(self._aggregate(operation))
        self.session.commit()

        self.assertEqual(len(self._events(operation)), before)
        self.assertEqual(operation.state, OperationState.SUCCEEDED.value)

    def test_a_terminal_operation_is_never_reopened(self):
        operation = self._admit()
        job = self._job(operation, "succeeded")
        self._aggregate(operation)
        self.session.commit()

        # A late redelivery flips a sibling to failed. The Operation has
        # already reported its outcome; history is not rewritten.
        job.state = "failed"
        self.session.commit()
        self.assertIsNone(self._aggregate(operation))
        self.assertEqual(operation.state, OperationState.SUCCEEDED.value)

    def test_a_reconciled_uncertain_operation_can_still_partially_succeed(self):
        """Reconciliation resolves the ambiguous call, and the command then
        reads "some succeeded, some failed". If the state machine refused
        that pair, the transition would raise inside the Job write that
        triggered it and roll that Job's own settled state back."""
        operation = self._admit()
        succeeded = self._job(operation, "succeeded")
        ambiguous = self._job(operation, "uncertain")

        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.UNCERTAIN.value)

        # An `ExternalCallCoordinator.reconcile()` pass settles it as failed.
        ambiguous.state = "failed"
        self.session.commit()

        self.assertIsNotNone(self._aggregate(operation))
        self.session.commit()
        self.assertEqual(operation.state, OperationState.PARTIALLY_SUCCEEDED.value)
        self.assertEqual(succeeded.state, "succeeded")

    def test_an_unreachable_pair_is_skipped_rather_than_raised(self):
        """Aggregation runs inside a Job's own terminal transaction. A
        state-machine gap must not roll that Job back."""
        operation = self._admit()
        self._job(operation, "succeeded")
        transition_operation(
            self.session,
            operation.id,
            OperationState.CANCELLED,
            reason="cancelled before the fold",
            actor="system:worker",
        )
        self.session.commit()

        # `cancelled` is terminal, so this is the terminal guard rather than
        # the transition guard -- but neither may raise.
        self.assertIsNone(self._aggregate(operation))
        self.assertEqual(operation.state, OperationState.CANCELLED.value)

    def test_uncertain_operation_is_not_re_folded_to_uncertain(self):
        operation = self._admit()
        self._job(operation, "uncertain")
        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(operation.state, OperationState.UNCERTAIN.value)

        self.assertIsNone(self._aggregate(operation))
        self.assertEqual(
            [event.to_state for event in self._events(operation)],
            [
                OperationState.ADMITTED.value,
                OperationState.UNCERTAIN.value,
            ],
        )

    # -- what the fold must not touch ------------------------------------

    def test_legacy_queued_operations_are_left_to_their_own_handlers(self):
        """Operations admitted before the durable admission contract do not
        carry a complete Job set, and their domain handlers own their
        transitions. Folding them generically would race those handlers."""
        operation = self._admit(contract_version=0)
        self._job(operation, "succeeded")

        self.assertIsNone(self._aggregate(operation))
        self.assertIsNone(
            start_operation_for_job(
                self.session, operation.id, actor="system:worker", context=self.context
            )
        )
        self.assertEqual(operation.state, OperationState.QUEUED.value)

    def test_operation_with_no_jobs_is_left_alone(self):
        operation = self._admit()
        self.assertIsNone(self._aggregate(operation))
        self.assertEqual(operation.state, OperationState.ADMITTED.value)

    def test_missing_or_absent_operation_id_is_a_no_op(self):
        self.assertIsNone(
            aggregate_operation_from_jobs(
                self.session, None, actor="system:worker", context=self.context
            )
        )
        self.assertIsNone(
            aggregate_operation_from_jobs(
                self.session, "op_absent", actor="system:worker", context=self.context
            )
        )

    def test_jobs_of_another_operation_do_not_decide_this_one(self):
        first = self._admit()
        second = self._admit()
        self._job(first, "succeeded")
        self._job(second, "running")

        self._aggregate(first)
        self.session.commit()
        self.assertEqual(first.state, OperationState.SUCCEEDED.value)
        self.assertIsNone(self._aggregate(second))
        self.assertEqual(second.state, OperationState.ADMITTED.value)

    def test_running_operation_folds_without_a_second_running_event(self):
        operation = self._admit()
        self._job(operation, "succeeded")
        transition_operation(
            self.session,
            operation.id,
            OperationState.RUNNING,
            reason="first admitted Job started",
            actor="system:worker",
        )
        self.session.commit()

        self._aggregate(operation)
        self.session.commit()
        self.assertEqual(
            [event.to_state for event in self._events(operation)],
            [
                OperationState.ADMITTED.value,
                OperationState.RUNNING.value,
                OperationState.SUCCEEDED.value,
            ],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
