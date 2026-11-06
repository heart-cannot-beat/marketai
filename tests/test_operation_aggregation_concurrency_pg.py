"""PostgreSQL evidence that Job -> Operation aggregation really serializes.

``tests/test_operation_job_aggregation_queue.py`` runs the same two-Worker
race on SQLite, where ``SELECT ... FOR UPDATE`` is silently ignored and
SQLite's own single-writer lock does the serializing. That proves the code
path, not the lock -- so it would keep passing if the ``with_for_update()``
were deleted.

This module runs it on a real PostgreSQL domain database, where two
concurrent sessions genuinely overlap. Without the row lock both Workers
read the other's Job as still in flight, neither folds, and the Operation
stays non-terminal forever with every sibling Job finished. The negative
control below asserts exactly that, by running the same race through an
unlocked read.

Set ``DOMAIN_OWNER_DATABASE_URL`` to a migrated domain database (CI does;
``make db-release`` provisions one locally).
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timezone


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")


@unittest.skipUnless(
    OWNER_URL,
    "set DOMAIN_OWNER_DATABASE_URL to a migrated PostgreSQL domain database",
)
class TestOperationAggregationConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine

        cls.engine = create_engine(OWNER_URL, future=True)

    def setUp(self) -> None:
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import WorkspaceRecord

        self.workspace_id = f"ws_op_agg_{uuid.uuid4().hex[:10]}"
        self.operation_id = f"op_agg_{uuid.uuid4().hex[:10]}"
        with Session(self.engine, future=True) as session:
            session.add(
                WorkspaceRecord(id=self.workspace_id, name=self.workspace_id)
            )
            session.commit()
        self.job_ids = [self._seed()]

    def _seed(self) -> str:
        """One admitted Operation with two Jobs, both still running."""
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import JobRecord
        from app.platform.operations import (
            OperationState,
            create_operation,
        )
        from app.platform.tenancy import TenantContext, apply_to_session

        context = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_agg"
        )
        self.context = context
        now = datetime.now(timezone.utc)
        with Session(self.engine, future=True) as session:
            apply_to_session(session, context)
            create_operation(
                session,
                context=context,
                operation_id=self.operation_id,
                command_type="localization.confirm",
                initial_state=OperationState.ADMITTED,
                admission_contract_version=1,
                now=now,
            )
            self.jobs = []
            for index in range(2):
                job = JobRecord(
                    id=f"job_agg_{uuid.uuid4().hex[:10]}",
                    workspace_id=self.workspace_id,
                    initiator_id="user_agg",
                    initiator_role="owner",
                    type="localization.edit_attempt",
                    payload_json={},
                    idempotency_key=f"idem_{self.operation_id}_{index}",
                    state="running",
                    operation_id=self.operation_id,
                    entitlement_version_at_enqueue=1,
                    execution_contract_version=1,
                )
                session.add(job)
                self.jobs.append(job.id)
            session.commit()
        return self.jobs[0]

    def _operation_state(self) -> str:
        from sqlalchemy.orm import Session

        from app.platform.operations import OperationRecord

        with Session(self.engine, future=True) as session:
            return session.get(OperationRecord, self.operation_id).state

    def _terminal_event_count(self) -> int:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from app.platform.operations import (
            TERMINAL_STATES,
            OperationStateEvent,
        )

        terminal = {state.value for state in TERMINAL_STATES}
        with Session(self.engine, future=True) as session:
            return sum(
                1
                for event in session.scalars(
                    select(OperationStateEvent).where(
                        OperationStateEvent.operation_id == self.operation_id
                    )
                )
                if event.to_state in terminal
            )

    def _race(self, finish) -> list[BaseException]:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run(job_id: str) -> None:
            try:
                barrier.wait(timeout=15)
                finish(job_id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(job_id,)) for job_id in self.jobs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return errors

    def _finish(self, job_id: str, *, lock: bool) -> None:
        """Settle one Job and fold its Operation, with or without the lock.

        The two paths are identical except for ``with_for_update()``, and
        both sleep between writing their own Job and reading the siblings.
        That sleep is what makes the race deterministic instead of likely:
        it guarantees both sessions have written an uncommitted Job before
        either reads, which is exactly the interleaving the lock exists for.
        """
        import time

        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import JobRecord
        from app.platform.operations import (
            OperationRecord,
            OperationState,
            derive_operation_outcome,
            transition_operation,
        )
        from app.platform.tenancy import apply_to_session

        with Session(self.engine, future=True) as session:
            apply_to_session(session, self.context)
            job = session.get(JobRecord, job_id)
            job.state = "succeeded"
            job.finished_at = datetime.now(timezone.utc)
            session.flush()

            time.sleep(0.5)

            statement = select(OperationRecord).where(
                OperationRecord.id == self.operation_id,
                OperationRecord.workspace_id == self.workspace_id,
            )
            if lock:
                statement = statement.with_for_update()
            operation = session.scalar(statement)

            states = session.scalars(
                select(JobRecord.state).where(
                    JobRecord.workspace_id == self.workspace_id,
                    JobRecord.operation_id == self.operation_id,
                )
            ).all()
            outcome = derive_operation_outcome(states)
            if outcome is not None and OperationState(operation.state) not in {
                OperationState.SUCCEEDED,
                OperationState.FAILED,
                OperationState.PARTIALLY_SUCCEEDED,
                OperationState.CANCELLED,
            }:
                if OperationState(operation.state) is OperationState.ADMITTED:
                    transition_operation(
                        session,
                        self.operation_id,
                        OperationState.RUNNING,
                        reason="dispatch observed while aggregating",
                        actor="system:worker",
                    )
                transition_operation(
                    session,
                    self.operation_id,
                    outcome,
                    reason="aggregated from sibling Jobs",
                    actor="system:worker",
                )
            session.commit()

    def test_two_concurrent_final_jobs_fold_the_operation_exactly_once(self):
        from app.platform.operations import OperationState

        self.assertEqual(self._race(lambda job_id: self._finish(job_id, lock=True)), [])
        self.assertEqual(self._operation_state(), OperationState.SUCCEEDED.value)
        self.assertEqual(self._terminal_event_count(), 1)

    def test_the_real_aggregator_folds_the_same_race_exactly_once(self):
        """The shipped function, not a reconstruction of it."""
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import JobRecord
        from app.platform.operations import (
            OperationState,
            aggregate_operation_from_jobs,
        )
        from app.platform.tenancy import apply_to_session

        def finish(job_id: str) -> None:
            import time

            with Session(self.engine, future=True) as session:
                apply_to_session(session, self.context)
                job = session.get(JobRecord, job_id)
                job.state = "succeeded"
                job.finished_at = datetime.now(timezone.utc)
                session.flush()
                time.sleep(0.5)
                aggregate_operation_from_jobs(
                    session,
                    self.operation_id,
                    actor="system:worker",
                    context=self.context,
                )
                session.commit()

        self.assertEqual(self._race(finish), [])
        self.assertEqual(self._operation_state(), OperationState.SUCCEEDED.value)
        self.assertEqual(self._terminal_event_count(), 1)

    def test_without_the_row_lock_the_same_race_strands_the_operation(self):
        """The negative control the positive tests need to mean anything.

        Identical to `_finish(lock=True)` but for the missing
        ``with_for_update()``: both sessions read the other's Job before it
        commits, both decline to fold, and the Operation is stranded with
        every Job terminal -- the bug the lock exists to prevent.
        """
        from app.platform.operations import OperationState

        self.assertEqual(
            self._race(lambda job_id: self._finish(job_id, lock=False)), []
        )
        self.assertEqual(
            self._operation_state(),
            OperationState.ADMITTED.value,
            "without the row lock neither Worker folds, so the Operation is "
            "stranded -- if this ever reads a terminal state, the negative "
            "control has stopped reproducing the race and the positive tests "
            "above no longer prove the lock does anything",
        )
        self.assertEqual(self._terminal_event_count(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
