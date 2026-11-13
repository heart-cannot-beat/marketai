"""Operation state machine and persistence (spec §5.1-5.2).

A dedicated, throwaway SQLite database is used here -- not the shared
``app.adapters.persistence.database.engine`` -- so these tests do not depend
on another test module's import-time environment variables or migration
ordering. ``Base.metadata`` (registered by importing
``app.adapters.persistence.models``) is created directly for exactly the
tables this module touches, which also proves the ORM models match what the
Alembic migration built (``alembic check`` covers the reverse direction).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, utcnow
from app.platform.operations import (
    InvalidOperationTransitionError,
    OperationNotFoundError,
    OperationRecord,
    OperationResultRef,
    OperationState,
    OperationStateEvent,
    VALID_TRANSITIONS,
    create_operation,
    get_operation,
    result_ref_of,
    transition_operation,
    validate_transition,
)
from app.platform.tenancy import TenantContext, apply_to_session
from tests.support.usage_tables import operation_usage_tables


class TestOperationStateMachine(unittest.TestCase):
    """The six transitions the plan's brief specifies (spec §5.2 table)."""

    def test_queued_to_running_is_valid(self):
        assert validate_transition(OperationState.QUEUED, OperationState.RUNNING) is True

    def test_running_to_succeeded_is_valid(self):
        assert validate_transition(OperationState.RUNNING, OperationState.SUCCEEDED) is True

    def test_succeeded_to_running_is_invalid(self):
        assert validate_transition(OperationState.SUCCEEDED, OperationState.RUNNING) is False

    def test_uncertain_to_running_is_valid(self):
        assert validate_transition(OperationState.UNCERTAIN, OperationState.RUNNING) is True

    def test_queued_to_cancelled_is_valid(self):
        assert validate_transition(OperationState.QUEUED, OperationState.CANCELLED) is True

    def test_running_to_uncertain_is_valid(self):
        assert validate_transition(OperationState.RUNNING, OperationState.UNCERTAIN) is True


class TestOperationStateMachineCompleteness(unittest.TestCase):
    """Every other pair spec §5.2 does not list must be rejected."""

    def test_terminal_states_accept_no_further_transitions(self):
        for terminal in (
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.PARTIALLY_SUCCEEDED,
            OperationState.CANCELLED,
        ):
            for target in OperationState:
                with self.subTest(frm=terminal, to=target):
                    self.assertFalse(validate_transition(terminal, target))

    def test_running_and_uncertain_are_the_only_reciprocal_pair(self):
        """``uncertain`` is the sole non-terminal isolation state (spec §5.2):
        only ``running <-> uncertain`` may be traversed in both directions.
        """
        reciprocal = [
            (frm, to)
            for frm, targets in VALID_TRANSITIONS.items()
            for to in targets
            if frm in VALID_TRANSITIONS.get(to, set())
        ]
        self.assertCountEqual(
            reciprocal,
            [
                (OperationState.RUNNING, OperationState.UNCERTAIN),
                (OperationState.UNCERTAIN, OperationState.RUNNING),
            ],
        )

    def test_cancellation_is_reachable_from_every_non_terminal_state(self):
        sources = {
            frm for frm, targets in VALID_TRANSITIONS.items() if OperationState.CANCELLED in targets
        }
        self.assertEqual(
            sources,
            {
                OperationState.ADMITTED,
                OperationState.QUEUED,
                OperationState.RUNNING,
                OperationState.UNCERTAIN,
            },
        )

    def test_admitted_reaches_running_and_the_no_dispatch_terminals(self):
        """``admitted`` is the paid entry state: a Job may start (``running``),
        or be cancelled/rejected before any Worker claims it. It must not
        reach a success directly -- that would claim work completed without
        ever having started."""
        self.assertEqual(
            VALID_TRANSITIONS[OperationState.ADMITTED],
            {
                OperationState.RUNNING,
                OperationState.CANCELLED,
                OperationState.FAILED,
                OperationState.UNCERTAIN,
            },
        )
        for forbidden in (
            OperationState.SUCCEEDED,
            OperationState.PARTIALLY_SUCCEEDED,
            OperationState.QUEUED,
        ):
            with self.subTest(to=forbidden):
                self.assertFalse(
                    validate_transition(OperationState.ADMITTED, forbidden)
                )

    def test_queued_reaches_running_and_the_no_dispatch_terminals(self):
        """``queued`` is the unpriced entry state: a Job may start, be
        cancelled, or be rejected before dispatch under a revoked
        authorization epoch (#22). Like ``admitted``, it never reaches a
        success or ``uncertain`` without first recording ``running``."""
        self.assertEqual(
            VALID_TRANSITIONS[OperationState.QUEUED],
            {OperationState.RUNNING, OperationState.CANCELLED, OperationState.FAILED},
        )

    def test_no_state_transitions_back_into_admitted(self):
        for frm, targets in VALID_TRANSITIONS.items():
            with self.subTest(frm=frm):
                self.assertNotIn(OperationState.ADMITTED, targets)

    def test_every_state_is_terminal_or_non_terminal_exactly_once(self):
        from app.platform.operations import NON_TERMINAL_STATES, TERMINAL_STATES

        self.assertEqual(
            set(OperationState), set(TERMINAL_STATES) | set(NON_TERMINAL_STATES)
        )
        self.assertEqual(set(TERMINAL_STATES) & set(NON_TERMINAL_STATES), set())


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


class OperationPersistenceTestCase(unittest.TestCase):
    """Real SQLAlchemy models against a scratch SQLite database."""

    def setUp(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-operations-"))
        self.engine = create_engine(
            _sqlite_url(self._tmp_dir / "operations.db"),
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                WorkspaceRecord.__table__,
                OperationRecord.__table__,
                OperationStateEvent.__table__,
                # A terminal transition records the Operation's Usage (#8 S2).
                *operation_usage_tables(),
            ],
        )
        # The migration's append-only guard is DB-level SQL, not covered by
        # create_all -- install the same trigger it does so the persistence
        # tests exercise the real invariant, not just the Python code path.
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TRIGGER operation_state_events_reject_update
                BEFORE UPDATE ON operation_state_events
                BEGIN
                    SELECT RAISE(ABORT, 'operation_state_events is append-only');
                END
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TRIGGER operation_state_events_reject_delete
                BEFORE DELETE ON operation_state_events
                BEGIN
                    SELECT RAISE(ABORT, 'operation_state_events is append-only');
                END
                """
            )

        self.session = Session(bind=self.engine)
        self.workspace_id = "ws_operations"
        self.session.add(
            WorkspaceRecord(id=self.workspace_id, name="Operations Test", created_at=utcnow())
        )
        self.session.commit()

        self.context = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_ops"
        )
        apply_to_session(self.session, self.context)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class TestCreateOperation(OperationPersistenceTestCase):
    def test_create_operation_starts_queued_with_one_creation_event(self):
        operation = create_operation(self.session, command_type="finalize_upload")
        self.session.commit()

        self.assertEqual(operation.state, OperationState.QUEUED.value)
        self.assertEqual(operation.workspace_id, self.workspace_id)
        self.assertEqual(operation.initiator_id, "user_ops")
        self.assertIsNone(operation.result_resource_type)

        events = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id)
            .all()
        )
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].from_state)
        self.assertEqual(events[0].to_state, OperationState.QUEUED.value)

    def test_create_operation_is_scoped_to_the_bound_workspace(self):
        other_workspace = "ws_other_operations"
        self.session.add(
            WorkspaceRecord(id=other_workspace, name="Other", created_at=utcnow())
        )
        self.session.commit()

        operation = create_operation(self.session, command_type="finalize_upload")
        self.session.commit()

        other_context = TenantContext(
            workspace_id=other_workspace, role="owner", initiator_id="user_other"
        )
        apply_to_session(self.session, other_context)

        with self.assertRaises(OperationNotFoundError):
            get_operation(self.session, operation.id)


class TestTransitionOperation(OperationPersistenceTestCase):
    def _create(self) -> OperationRecord:
        operation = create_operation(self.session, command_type="finalize_upload")
        self.session.commit()
        return operation

    def test_valid_transition_updates_projection_and_appends_event(self):
        operation = self._create()

        updated = transition_operation(
            self.session,
            operation.id,
            OperationState.RUNNING,
            reason="worker picked up job",
            actor="system:worker",
        )
        self.session.commit()

        self.assertEqual(updated.state, OperationState.RUNNING.value)
        self.assertGreaterEqual(updated.updated_at, updated.created_at)

        events = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id)
            .order_by(OperationStateEvent.created_at)
            .all()
        )
        self.assertEqual([e.to_state for e in events], [
            OperationState.QUEUED.value,
            OperationState.RUNNING.value,
        ])
        self.assertEqual(events[-1].from_state, OperationState.QUEUED.value)
        self.assertEqual(events[-1].actor, "system:worker")
        self.assertEqual(events[-1].reason, "worker picked up job")

    def test_succeeded_operation_accepts_a_committed_blob_key_result(self):
        operation = self._create()
        transition_operation(
            self.session,
            operation.id,
            OperationState.RUNNING,
            reason=None,
            actor="system:worker",
        )
        committed_key = (
            "committed/ws_operations/library_manual_fact_import/2026/09/23/"
            f"{'a' * 96}.json"
        )

        updated = transition_operation(
            self.session,
            operation.id,
            OperationState.SUCCEEDED,
            reason=None,
            actor="system:worker",
            result_ref=OperationResultRef(
                resource_type="committed_blob",
                resource_id=committed_key,
            ),
        )
        self.session.commit()

        self.assertGreater(len(committed_key), 100)
        self.assertEqual(
            OperationRecord.__table__.c.result_resource_id.type.length,
            1000,
        )
        self.assertEqual(updated.result_resource_id, committed_key)

    def test_invalid_transition_raises_and_leaves_state_untouched(self):
        operation = self._create()
        transition_operation(
            self.session, operation.id, OperationState.RUNNING, reason=None, actor="system:worker"
        )
        transition_operation(
            self.session, operation.id, OperationState.SUCCEEDED, reason=None, actor="system:worker"
        )
        self.session.commit()

        with self.assertRaises(InvalidOperationTransitionError):
            transition_operation(
                self.session, operation.id, OperationState.RUNNING, reason=None, actor="system:worker"
            )

        refreshed = get_operation(self.session, operation.id)
        self.assertEqual(refreshed.state, OperationState.SUCCEEDED.value)
        events = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id)
            .all()
        )
        # The rejected transition must not have appended a phantom event.
        self.assertEqual(len(events), 3)

    def test_history_is_never_rewritten_only_appended(self):
        operation = self._create()
        transition_operation(
            self.session, operation.id, OperationState.RUNNING, reason=None, actor="system:worker"
        )
        first_event_id = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id, to_state=OperationState.QUEUED.value)
            .one()
            .id
        )

        transition_operation(
            self.session, operation.id, OperationState.UNCERTAIN, reason="timeout", actor="system:worker"
        )
        transition_operation(
            self.session,
            operation.id,
            OperationState.SUCCEEDED,
            reason="reconciler confirmed",
            actor="system:reconciler",
        )
        self.session.commit()

        events = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id)
            .order_by(OperationStateEvent.created_at)
            .all()
        )
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0].id, first_event_id)
        self.assertEqual(
            [e.to_state for e in events],
            [
                OperationState.QUEUED.value,
                OperationState.RUNNING.value,
                OperationState.UNCERTAIN.value,
                OperationState.SUCCEEDED.value,
            ],
        )

    def test_result_ref_is_persisted_and_round_trips(self):
        operation = self._create()
        transition_operation(
            self.session, operation.id, OperationState.RUNNING, reason=None, actor="system:worker"
        )
        updated = transition_operation(
            self.session,
            operation.id,
            OperationState.SUCCEEDED,
            reason="done",
            actor="system:worker",
            result_ref=OperationResultRef(resource_type="asset_version", resource_id="av_1"),
        )
        self.session.commit()

        self.assertEqual(
            result_ref_of(updated),
            OperationResultRef(resource_type="asset_version", resource_id="av_1"),
        )

    def test_no_result_ref_round_trips_to_none(self):
        operation = self._create()
        self.assertIsNone(result_ref_of(operation))

    def test_transitioning_an_unknown_operation_raises_not_found(self):
        with self.assertRaises(OperationNotFoundError):
            transition_operation(
                self.session, "op_missing", OperationState.RUNNING, reason=None, actor="system:worker"
            )

    def test_the_database_itself_rejects_updating_a_history_event(self):
        """Defense in depth: even a direct write cannot rewrite history.

        This proves the append-only trigger the migration installs, not just
        that ``transition_operation`` behaves -- see
        ``h1a2b3c4d5e6_operation_tables.py``.
        """
        operation = self._create()
        event = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id)
            .one()
        )

        with self.assertRaises(IntegrityError):
            self.session.execute(
                OperationStateEvent.__table__.update()
                .where(OperationStateEvent.id == event.id)
                .values(reason="rewritten")
            )
        self.session.rollback()

    def test_the_database_itself_rejects_deleting_a_history_event(self):
        operation = self._create()
        event = (
            self.session.query(OperationStateEvent)
            .filter_by(operation_id=operation.id)
            .one()
        )

        with self.assertRaises(IntegrityError):
            self.session.execute(
                OperationStateEvent.__table__.delete().where(OperationStateEvent.id == event.id)
            )
        self.session.rollback()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
