"""Idempotency records + pending-reservation concurrency protocol (spec §5.3-5.4).

Flat in ``tests/`` (not ``tests/platform/``): a ``tests/platform/`` package
would shadow Python's stdlib ``platform`` module for every test module
imported afterward -- see the same note on ``tests/test_operations.py``.

A dedicated, throwaway SQLite database is used for the persistence tests --
not the shared ``app.adapters.persistence.database.engine`` -- so they do
not depend on another test module's import-time environment variables or
migration ordering, mirroring ``tests/test_operations.py``.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import IdempotencyRecord as IdempotencyRecordModel
from app.adapters.persistence.models import WorkspaceRecord, utcnow
from app.platform.idempotency import (
    IdempotencyKeyReusedError,
    IdempotencyReservationLostError,
    IdempotencyTimeoutError,
    compute_fingerprint,
    create_or_replay_intent,
    create_or_replay_operation,
    get_idempotency_record,
    _claim_reservation,
    _poll_until_committed,
)
from app.platform.operations import OperationRecord, OperationStateEvent
from app.platform.tenancy import TenantContext, apply_to_session


class TestFingerprint(unittest.TestCase):
    def test_same_inputs_produce_same_hash(self):
        h1 = compute_fingerprint(
            "POST", "create_upload_intent", "/api/v1/upload-intents", {}, {"file": "a.png"}, {}
        )
        h2 = compute_fingerprint(
            "POST", "create_upload_intent", "/api/v1/upload-intents", {}, {"file": "a.png"}, {}
        )
        assert h1 == h2

    def test_different_method_produces_different_hash(self):
        h1 = compute_fingerprint("POST", "op1", "/path", {}, None, {})
        h2 = compute_fingerprint("PUT", "op1", "/path", {}, None, {})
        assert h1 != h2

    def test_different_body_produces_different_hash(self):
        h1 = compute_fingerprint("POST", "op1", "/path", {}, {"a": 1}, {})
        h2 = compute_fingerprint("POST", "op1", "/path", {}, {"a": 2}, {})
        assert h1 != h2

    def test_different_operation_id_produces_different_hash(self):
        h1 = compute_fingerprint("POST", "op1", "/path", {}, None, {})
        h2 = compute_fingerprint("POST", "op2", "/path", {}, None, {})
        assert h1 != h2

    def test_different_query_produces_different_hash(self):
        h1 = compute_fingerprint("GET", "op1", "/path", {"a": "1"}, None, {})
        h2 = compute_fingerprint("GET", "op1", "/path", {"a": "2"}, None, {})
        assert h1 != h2

    def test_different_precondition_headers_produce_different_hash(self):
        h1 = compute_fingerprint("PUT", "op1", "/path", {}, None, {"if-match": '"a"'})
        h2 = compute_fingerprint("PUT", "op1", "/path", {}, None, {"if-match": '"b"'})
        assert h1 != h2

    def test_query_key_order_does_not_affect_hash(self):
        h1 = compute_fingerprint("GET", "op1", "/path", {"a": "1", "b": "2"}, None, {})
        h2 = compute_fingerprint("GET", "op1", "/path", {"b": "2", "a": "1"}, None, {})
        assert h1 == h2


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


class IdempotencyPersistenceTestCase(unittest.TestCase):
    """Real SQLAlchemy models + raw-SQL protocol against a scratch SQLite database."""

    def setUp(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-idempotency-"))
        self.engine = create_engine(
            _sqlite_url(self._tmp_dir / "idempotency.db"),
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                WorkspaceRecord.__table__,
                OperationRecord.__table__,
                OperationStateEvent.__table__,
                IdempotencyRecordModel.__table__,
            ],
        )

        self.session = Session(bind=self.engine)
        self.workspace_id = "ws_idempotency"
        self.session.add(
            WorkspaceRecord(id=self.workspace_id, name="Idempotency Test", created_at=utcnow())
        )
        self.session.commit()

        self.context = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_idem"
        )
        apply_to_session(self.session, self.context)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class TestCreateOrReplayOperationNoKey(IdempotencyPersistenceTestCase):
    def test_none_key_always_creates_a_fresh_operation(self):
        op1 = create_or_replay_operation(
            self.session,
            idempotency_key=None,
            command_type="finalize_upload",
            fingerprint_hash="irrelevant",
        )
        self.session.commit()
        op2 = create_or_replay_operation(
            self.session,
            idempotency_key=None,
            command_type="finalize_upload",
            fingerprint_hash="irrelevant",
        )
        self.session.commit()

        self.assertNotEqual(op1.id, op2.id)


class TestCreateOrReplayOperationWithKey(IdempotencyPersistenceTestCase):
    def test_first_call_wins_the_reservation_and_commits_an_outcome(self):
        operation = create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-1",
            command_type="finalize_upload",
            fingerprint_hash="fp-1",
        )
        self.session.commit()

        self.assertEqual(operation.state, "queued")

        record = get_idempotency_record(
            self.session, _hash("idem-key-1"), self.workspace_id
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.state, "committed")
        self.assertEqual(record.operation_id, operation.id)
        self.assertEqual(record.response_status, 202)
        self.assertIsNone(record.response_resource_type)
        self.assertIsNone(record.response_resource_id)

    def test_repeat_call_with_identical_request_replays_the_same_operation(self):
        first = create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-2",
            command_type="finalize_upload",
            fingerprint_hash="fp-2",
        )
        self.session.commit()

        second = create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-2",
            command_type="finalize_upload",
            fingerprint_hash="fp-2",
        )
        self.session.commit()

        self.assertEqual(first.id, second.id)

        # Exactly one Operation was created for the reused key.
        all_ops = self.session.query(OperationRecord).all()
        self.assertEqual(len(all_ops), 1)

    def test_different_command_type_with_same_key_is_rejected(self):
        create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-3",
            command_type="finalize_upload",
            fingerprint_hash="fp-3",
        )
        self.session.commit()

        with self.assertRaises(IdempotencyKeyReusedError):
            create_or_replay_operation(
                self.session,
                idempotency_key="idem-key-3",
                command_type="cancel_upload",
                fingerprint_hash="fp-3",
            )

    def test_different_fingerprint_with_same_key_is_rejected(self):
        create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-4",
            command_type="finalize_upload",
            fingerprint_hash="fp-4a",
        )
        self.session.commit()

        with self.assertRaises(IdempotencyKeyReusedError):
            create_or_replay_operation(
                self.session,
                idempotency_key="idem-key-4",
                command_type="finalize_upload",
                fingerprint_hash="fp-4b",
            )

    def test_different_actor_with_same_key_is_rejected(self):
        create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-5",
            command_type="finalize_upload",
            fingerprint_hash="fp-5",
        )
        self.session.commit()

        other_context = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_other"
        )
        apply_to_session(self.session, other_context)

        with self.assertRaises(IdempotencyKeyReusedError):
            create_or_replay_operation(
                self.session,
                idempotency_key="idem-key-5",
                command_type="finalize_upload",
                fingerprint_hash="fp-5",
            )

    def test_different_workspace_with_same_raw_key_does_not_collide(self):
        """The composite primary key is (key_hash, workspace_id) -- spec §5.3:
        the same raw Idempotency-Key is scoped per Workspace, never global."""
        create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-6",
            command_type="finalize_upload",
            fingerprint_hash="fp-6",
        )
        self.session.commit()

        other_workspace = "ws_idempotency_other"
        self.session.add(
            WorkspaceRecord(id=other_workspace, name="Other", created_at=utcnow())
        )
        self.session.commit()
        other_context = TenantContext(
            workspace_id=other_workspace, role="owner", initiator_id="user_other"
        )
        apply_to_session(self.session, other_context)

        # Same raw key, different Workspace -- must not raise, and must
        # create its own, independent Operation.
        other_operation = create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-6",
            command_type="finalize_upload",
            fingerprint_hash="fp-6",
        )
        self.session.commit()

        apply_to_session(self.session, self.context)
        first_record = get_idempotency_record(
            self.session, _hash("idem-key-6"), self.workspace_id
        )
        self.assertNotEqual(first_record.operation_id, other_operation.id)

    def test_pending_reservation_that_never_commits_times_out(self):
        """Simulates the losing side of a concurrent race: a pending row is
        claimed (e.g. by a crashed transaction) and never committed."""
        key_hash = _hash("idem-key-7")
        won = _claim_reservation(
            self.session,
            key_hash=key_hash,
            workspace_id=self.workspace_id,
            actor_id="user_idem",
            command_type="finalize_upload",
            fingerprint_hash="fp-7",
            expires_at=utcnow(),
        )
        self.session.commit()
        self.assertTrue(won)

        with self.assertRaises(IdempotencyTimeoutError):
            create_or_replay_operation(
                self.session,
                idempotency_key="idem-key-7",
                command_type="finalize_upload",
                fingerprint_hash="fp-7",
                max_wait_seconds=0.05,
                poll_interval=0.01,
            )


class TestPollUntilCommittedReservationLost(IdempotencyPersistenceTestCase):
    def test_reservation_disappearing_mid_wait_raises_reservation_lost(self):
        """A pending row that vanishes while a waiter polls it (e.g. reaped
        by expiry cleanup) must fail loudly, not hang or silently proceed."""
        key_hash = _hash("idem-key-8")
        _claim_reservation(
            self.session,
            key_hash=key_hash,
            workspace_id=self.workspace_id,
            actor_id="user_idem",
            command_type="finalize_upload",
            fingerprint_hash="fp-8",
            expires_at=utcnow(),
        )
        self.session.execute(
            IdempotencyRecordModel.__table__.delete().where(
                IdempotencyRecordModel.key_hash == key_hash
            )
        )
        self.session.commit()

        with self.assertRaises(IdempotencyReservationLostError):
            _poll_until_committed(
                self.session,
                key_hash=key_hash,
                workspace_id=self.workspace_id,
                actor_id="user_idem",
                command_type="finalize_upload",
                fingerprint_hash="fp-8",
                max_wait_seconds=0.05,
                poll_interval=0.01,
            )


class TestCreateOrReplayIntent(IdempotencyPersistenceTestCase):
    def test_first_call_invokes_create_fn_and_commits_the_resource_outcome(self):
        calls = []

        def create_fn(session):
            calls.append(session)
            return ("upload_intent", "ui_01J000", {"upload_url": "https://example/put"})

        result = create_or_replay_intent(
            self.session,
            idempotency_key="intent-key-1",
            command_type="create_upload_intent",
            fingerprint_hash="fp-i1",
            create_fn=create_fn,
        )
        self.session.commit()

        self.assertEqual(result, {"upload_url": "https://example/put"})
        self.assertEqual(len(calls), 1)

        record = get_idempotency_record(
            self.session, _hash("intent-key-1"), self.workspace_id
        )
        self.assertEqual(record.state, "committed")
        self.assertEqual(record.response_resource_type, "upload_intent")
        self.assertEqual(record.response_resource_id, "ui_01J000")
        self.assertEqual(record.response_status, 201)
        self.assertIsNone(record.operation_id)

    def test_repeat_call_replays_without_invoking_create_fn_again(self):
        calls = []

        def create_fn(session):
            calls.append(session)
            return ("upload_intent", "ui_01J111", {"upload_url": "https://example/put", "n": len(calls)})

        def replay_fn(resource_type, resource_id, session):
            return {"resource_type": resource_type, "resource_id": resource_id, "replayed": True}

        first = create_or_replay_intent(
            self.session,
            idempotency_key="intent-key-2",
            command_type="create_upload_intent",
            fingerprint_hash="fp-i2",
            create_fn=create_fn,
            replay_fn=replay_fn,
        )
        self.session.commit()

        second = create_or_replay_intent(
            self.session,
            idempotency_key="intent-key-2",
            command_type="create_upload_intent",
            fingerprint_hash="fp-i2",
            create_fn=create_fn,
            replay_fn=replay_fn,
        )
        self.session.commit()

        self.assertEqual(len(calls), 1)  # create_fn ran exactly once
        self.assertEqual(first["n"], 1)
        self.assertEqual(
            second,
            {"resource_type": "upload_intent", "resource_id": "ui_01J111", "replayed": True},
        )

    def test_replay_without_replay_fn_defaults_to_resource_type_and_id(self):
        def create_fn(session):
            return ("download_intent", "di_01J222", "irrelevant-first-result")

        create_or_replay_intent(
            self.session,
            idempotency_key="intent-key-3",
            command_type="create_download_intent",
            fingerprint_hash="fp-i3",
            create_fn=create_fn,
        )
        self.session.commit()

        second = create_or_replay_intent(
            self.session,
            idempotency_key="intent-key-3",
            command_type="create_download_intent",
            fingerprint_hash="fp-i3",
            create_fn=create_fn,
        )
        self.assertEqual(second, ("download_intent", "di_01J222"))

    def test_different_fingerprint_with_same_key_is_rejected(self):
        def create_fn(session):
            return ("upload_intent", "ui_01J333", None)

        create_or_replay_intent(
            self.session,
            idempotency_key="intent-key-4",
            command_type="create_upload_intent",
            fingerprint_hash="fp-i4a",
            create_fn=create_fn,
        )
        self.session.commit()

        with self.assertRaises(IdempotencyKeyReusedError):
            create_or_replay_intent(
                self.session,
                idempotency_key="intent-key-4",
                command_type="create_upload_intent",
                fingerprint_hash="fp-i4b",
                create_fn=create_fn,
            )


class TestIdempotencyRecordCheckConstraints(IdempotencyPersistenceTestCase):
    """Proves the DB-level CHECK constraints (spec §5.3), not just the Python
    protocol -- a partial or buggy write must be rejected by SQLite itself."""

    def test_pending_row_with_an_outcome_is_rejected(self):
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            self.session.execute(
                IdempotencyRecordModel.__table__.insert().values(
                    key_hash="bad1",
                    workspace_id=self.workspace_id,
                    actor_id="user_idem",
                    command_type="finalize_upload",
                    request_fingerprint_hash="fp",
                    state="pending",
                    response_status=202,
                    expires_at=utcnow(),
                )
            )

    def test_committed_row_with_no_outcome_is_rejected(self):
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            self.session.execute(
                IdempotencyRecordModel.__table__.insert().values(
                    key_hash="bad2",
                    workspace_id=self.workspace_id,
                    actor_id="user_idem",
                    command_type="finalize_upload",
                    request_fingerprint_hash="fp",
                    state="committed",
                    expires_at=utcnow(),
                )
            )

    def test_committed_row_with_both_operation_and_resource_is_rejected(self):
        from sqlalchemy.exc import IntegrityError

        operation = create_or_replay_operation(
            self.session,
            idempotency_key="idem-key-check",
            command_type="finalize_upload",
            fingerprint_hash="fp-check",
        )
        self.session.commit()

        with self.assertRaises(IntegrityError):
            self.session.execute(
                IdempotencyRecordModel.__table__.insert().values(
                    key_hash="bad3",
                    workspace_id=self.workspace_id,
                    actor_id="user_idem",
                    command_type="finalize_upload",
                    request_fingerprint_hash="fp",
                    state="committed",
                    response_status=201,
                    operation_id=operation.id,
                    response_resource_type="upload_intent",
                    response_resource_id="ui_x",
                    expires_at=utcnow(),
                )
            )

    def test_empty_actor_id_is_rejected(self):
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            self.session.execute(
                IdempotencyRecordModel.__table__.insert().values(
                    key_hash="bad4",
                    workspace_id=self.workspace_id,
                    actor_id="",
                    command_type="finalize_upload",
                    request_fingerprint_hash="fp",
                    state="pending",
                    expires_at=utcnow(),
                )
            )


def _hash(key: str) -> str:
    import hashlib

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
