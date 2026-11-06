"""Tests for idempotency key hash persistence and uncertain-attempt recovery.

Task 2: ``idempotency_key_hash`` is persisted on the
``ProviderCallAttemptRecord`` before dispatch.

Task 2.5: ``find_uncertain_attempts`` locates attempts stuck in
``uncertain`` state for SIGKILL recovery.
"""
from __future__ import annotations

import hashlib
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import JobRecord, OperationRecord, WorkspaceRecord
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    SpendReservationRecord,
)
from app.entitlements.credentials import (  # noqa: F401
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.platform.tenancy import TenantContext
from app.providers.adapter import ProviderCapabilities, ProviderResult
from app.providers.dispatch import (
    ExternalCallCoordinator,
    ExternalCallEvidenceRecorder,
)

WORKSPACE_ID = "ws_idem_test"
OPERATION_ID = "op_idem_1"
JOB_ID = "job_idem_1"
CALL_KEY_HASH = "hash_idem_abc"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_ctx() -> TenantContext:
    return TenantContext(
        workspace_id=WORKSPACE_ID,
        role="system",
        executor="system:worker",
        initiator_id="user_test",
        initiator_role="owner",
    )


class _NoOpEvidenceRecorder:
    def link_prepared_attempt(self, session, ctx, *, attempt):
        pass

    def materialize(self, ctx, *, attempt_id, output):
        return "ref"

    def persist_result(self, session, ctx, *, attempt, reference, output):
        pass


class IdempotencyKeyTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self._SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False,
        )

        @contextmanager
        def session_factory(ctx: TenantContext) -> Iterator[Session]:
            session = self._SessionLocal()
            session.info["tenant_context"] = ctx
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        self.session_factory = session_factory
        self.coordinator = ExternalCallCoordinator(session_factory=session_factory)
        self._seed_database()

    def _seed_database(self):
        now = _utcnow()
        session = self._SessionLocal()
        try:
            session.add(WorkspaceRecord(
                id=WORKSPACE_ID, name="Test Workspace", created_at=now,
            ))
            session.flush()
            session.add(OperationRecord(
                id=OPERATION_ID,
                workspace_id=WORKSPACE_ID,
                command_type="test",
                state="running",
                created_at=now,
                updated_at=now,
            ))
            session.flush()
            session.add(JobRecord(
                id=JOB_ID,
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                type="test",
                payload_json={},
                idempotency_key="test_idem",
                state="running",
                initiator_role="owner",
                lease_owner="worker-test",
                resource_type="test",
                resource_id="test",
                created_at=now,
            ))
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _attempt_rows(self) -> list[ProviderCallAttemptRecord]:
        with self._SessionLocal() as session:
            return (
                session.query(ProviderCallAttemptRecord)
                .filter_by(workspace_id=WORKSPACE_ID)
                .all()
            )


class TestIdempotencyKeyHashPersistence(IdempotencyKeyTestBase):
    """Task 2: idempotency_key_hash is persisted before dispatch."""

    async def test_prepare_persists_idempotency_key_hash(self):
        ctx = _make_ctx()
        recorder = _NoOpEvidenceRecorder()

        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=recorder,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )

        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]

        expected_hash = hashlib.sha256(
            prepared["idempotency_key"].encode()
        ).hexdigest()
        self.assertEqual(attempt.idempotency_key_hash, expected_hash)
        self.assertIsNotNone(attempt.idempotency_key_hash)

    async def test_hash_is_stable_for_same_key(self):
        ctx = _make_ctx()
        recorder = _NoOpEvidenceRecorder()

        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=recorder,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )

        key = prepared["idempotency_key"]
        expected = hashlib.sha256(key.encode()).hexdigest()
        attempt = self._attempt_rows()[0]
        self.assertEqual(attempt.idempotency_key_hash, expected)
        # Verify it's a 64-char hex string
        self.assertEqual(len(attempt.idempotency_key_hash), 64)


class TestFindUncertainAttempts(IdempotencyKeyTestBase):
    """Task 2.5: find_uncertain_attempts locates stuck attempts."""

    async def test_finds_uncertain_by_semantic_key(self):
        ctx = _make_ctx()
        recorder = _NoOpEvidenceRecorder()

        # Prepare an attempt
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=recorder,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        attempt_id = prepared["attempt_id"]

        # Mark it as dispatching then uncertain (simulating SIGKILL)
        self.coordinator._mark_dispatching(ctx, attempt_id=attempt_id)
        with self.session_factory(ctx) as session:
            attempt = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(id=attempt_id)
                .one()
            )
            attempt.dispatch_state = "uncertain"
            session.flush()

        results = self.coordinator.find_uncertain_attempts(ctx, CALL_KEY_HASH)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["attempt_id"], attempt_id)
        self.assertEqual(results[0]["dispatch_state"], "uncertain")
        self.assertIsNotNone(results[0]["idempotency_key_hash"])

    async def test_returns_empty_when_no_uncertain(self):
        ctx = _make_ctx()
        results = self.coordinator.find_uncertain_attempts(ctx, CALL_KEY_HASH)
        self.assertEqual(results, [])

    async def test_does_not_return_succeeded_attempts(self):
        ctx = _make_ctx()
        recorder = _NoOpEvidenceRecorder()

        # Create a succeeded attempt
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind="generation",
            execution_mode="controlled_mock",
            chargeability="free_sandbox",
            provider="controlled_mock",
            evidence_recorder=recorder,
            spend_reservation_id=None,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        attempt_id = prepared["attempt_id"]

        # Mark succeeded
        with self.session_factory(ctx) as session:
            attempt = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(id=attempt_id)
                .one()
            )
            attempt.dispatch_state = "succeeded"
            session.flush()

        results = self.coordinator.find_uncertain_attempts(ctx, CALL_KEY_HASH)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
