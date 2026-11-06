"""Tests for ExternalCallCoordinator three-phase dispatch."""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    JobRecord,
    OperationRecord,
    WorkspaceRecord,
)
from app.entitlements.coordinator import (
    CoordinatorError,
    ExternalCallCoordinator,
    MissingCredentialBindingError,
    ReservationInvalidError,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    EncryptedEnvelope,
    LocalKmsAdapter,
)
from app.entitlements.credentials import (
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.entitlements.types import ExternalCallOutcome
from app.platform.tenancy import TenantContext
from app.providers.adapter import (
    ProviderCallResult,
    ProviderCapabilities,
    ProviderDispatchError,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

MASTER_SECRET = os.urandom(32)
WORKSPACE_ID = "ws_test_coord"
JOB_ID = "job_coord_1"
OPERATION_ID = "op_coord_1"
CALL_KEY_HASH = "hash_abc123"
PROVIDER = "meta"
CREDENTIAL_KEY = "META_ACCESS_TOKEN"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_context() -> TenantContext:
    return TenantContext(
        workspace_id=WORKSPACE_ID,
        role="system",
        executor="system:worker",
        initiator_id="user_test",
        initiator_role="owner",
    )


class FakeProviderAdapter:
    """A test double for UnifiedProviderAdapter."""

    def __init__(self, *, fail: bool = False, certainty: str = "not_dispatched"):
        self._fail = fail
        self._certainty = certainty
        self.calls: list[Any] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=True,
            supports_result_lookup_by_request_id=False,
        )

    async def execute(self, request) -> ProviderCallResult:
        self.calls.append(request)
        if self._fail:
            raise ProviderDispatchError(
                dispatch_certainty=self._certainty,
                message="test failure",
                error_code="test_error",
            )
        return ProviderCallResult(
            output_bytes=b"fake-image-data",
            provider_request_id="prov_req_123",
            idempotency_key_accepted=True,
        )


class CoordinatorTestBase(unittest.IsolatedAsyncioTestCase):
    """Base class that sets up an in-memory SQLite database with all tables."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        # Enable foreign keys for SQLite.
        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self._SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False,
        )

        self.protector = LocalKmsAdapter(MASTER_SECRET)
        self.adapter = FakeProviderAdapter()

        @contextmanager
        def session_factory(context: TenantContext) -> Iterator[Session]:
            session = self._SessionLocal()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        self.session_factory = session_factory
        self.coordinator = ExternalCallCoordinator(
            session_factory=self.session_factory,
            protector=self.protector,
            adapter=self.adapter,
        )

        # Seed the workspace and prerequisite records.
        self._seed_database()

    def _seed_database(self):
        """Create workspace, operation, job, reservation, credential records."""
        now = _utcnow()
        session = self._SessionLocal()

        try:
            # Workspace
            session.add(WorkspaceRecord(
                id=WORKSPACE_ID,
                name="Test Workspace",
                created_at=now,
            ))
            session.flush()

            # Operation
            session.add(OperationRecord(
                id=OPERATION_ID,
                workspace_id=WORKSPACE_ID,
                command_type="generate_image",
                state="running",
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            # Job
            session.add(JobRecord(
                id=JOB_ID,
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                type="image_generation",
                payload_json={"prompt": "test"},
                idempotency_key="idem_test_1",
                state="running",
                initiator_role="owner",
                created_at=now,
            ))
            session.flush()

            # Spend reservation
            session.add(SpendReservationRecord(
                id="sr_test_1",
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                job_id=JOB_ID,
                semantic_key_hash=CALL_KEY_HASH,
                state="reserved",
                provider=PROVIDER,
                execution_mode="byok",
                provider_currency="USD",
                maximum_provider_amount=Decimal("1.00"),
                budget_currency="USD",
                maximum_budget_amount=Decimal("1.00"),
                outstanding_budget_amount=Decimal("1.00"),
                entitlement_version=1,
                lease_sequence=1,
                guardrail_revision=1,
                platform_policy_version=1,
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            # Credential version (encrypted)
            plaintext = b"sk-test-credential-value"
            enc_ctx = CredentialEncryptionContext(
                workspace_id=WORKSPACE_ID,
                credential_version_id="cv_test_1",
                credential_key=CREDENTIAL_KEY,
                provider=PROVIDER,
                version_no=1,
            )
            envelope = self.protector.seal(plaintext, enc_ctx)

            session.add(CredentialVersionRecord(
                id="cv_test_1",
                workspace_id=WORKSPACE_ID,
                credential_key=CREDENTIAL_KEY,
                version_no=1,
                provider=PROVIDER,
                state="active",
                encrypted_secret=envelope.encrypted_value,
                encrypted_data_key=envelope.encrypted_data_key,
                encryption_key_id=envelope.encryption_key_id,
                encryption_algorithm=envelope.encryption_algorithm,
                nonce=envelope.nonce,
                aad_schema_version=envelope.aad_schema_version,
                created_by="test_user",
                created_at=now,
            ))
            session.flush()

            # Credential binding
            session.add(CredentialBindingRecord(
                id="cb_test_1",
                workspace_id=WORKSPACE_ID,
                binding_scope="default",
                provider=PROVIDER,
                credential_version_id="cv_test_1",
                revision=1,
                bound_by="test_user",
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSuccessfulDispatch(CoordinatorTestBase):
    """1. Successful dispatch settles reservation and returns success outcome."""

    async def test_success_settles_reservation(self):
        ctx = _make_context()
        outcome = await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.dispatch_certainty, "dispatched")
        self.assertEqual(outcome.provider_request_id, "prov_req_123")
        self.assertIsNone(outcome.error_code)
        self.assertIsNone(outcome.error_detail)

        # Verify reservation is settled.
        session = self._SessionLocal()
        reservation = session.query(SpendReservationRecord).filter_by(
            id="sr_test_1"
        ).one()
        self.assertEqual(reservation.state, "settled")
        self.assertEqual(reservation.outstanding_budget_amount, 0)
        session.close()


class TestFailedDispatchNotDispatched(CoordinatorTestBase):
    """2. Failed dispatch with 'not_dispatched' releases reservation."""

    def setUp(self):
        super().setUp()
        self.adapter = FakeProviderAdapter(fail=True, certainty="not_dispatched")
        self.coordinator = ExternalCallCoordinator(
            session_factory=self.session_factory,
            protector=self.protector,
            adapter=self.adapter,
        )

    async def test_not_dispatched_releases_reservation(self):
        ctx = _make_context()
        outcome = await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.dispatch_certainty, "not_dispatched")
        self.assertEqual(outcome.error_code, "test_error")

        # Verify reservation is released.
        session = self._SessionLocal()
        reservation = session.query(SpendReservationRecord).filter_by(
            id="sr_test_1"
        ).one()
        self.assertEqual(reservation.state, "released")
        self.assertEqual(reservation.outstanding_budget_amount, 0)
        session.close()


class TestFailedDispatchUncertain(CoordinatorTestBase):
    """3. Failed dispatch with 'may_have_dispatched' marks reservation uncertain."""

    def setUp(self):
        super().setUp()
        self.adapter = FakeProviderAdapter(
            fail=True, certainty="may_have_dispatched"
        )
        self.coordinator = ExternalCallCoordinator(
            session_factory=self.session_factory,
            protector=self.protector,
            adapter=self.adapter,
        )

    async def test_uncertain_marks_reservation(self):
        ctx = _make_context()
        outcome = await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.dispatch_certainty, "may_have_dispatched")

        # Verify reservation is uncertain.
        session = self._SessionLocal()
        reservation = session.query(SpendReservationRecord).filter_by(
            id="sr_test_1"
        ).one()
        self.assertEqual(reservation.state, "uncertain")
        session.close()


class TestMissingCredentialBinding(CoordinatorTestBase):
    """4. Missing credential binding raises MissingCredentialBindingError."""

    def _seed_database(self):
        """Same as parent but without the credential binding."""
        now = _utcnow()
        session = self._SessionLocal()
        try:
            session.add(WorkspaceRecord(
                id=WORKSPACE_ID, name="Test", created_at=now,
            ))
            session.flush()
            session.add(OperationRecord(
                id=OPERATION_ID, workspace_id=WORKSPACE_ID,
                command_type="generate_image", state="running",
                created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(JobRecord(
                id=JOB_ID, workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID, type="image_generation",
                payload_json={}, idempotency_key="idem_2",
                state="running", initiator_role="owner", created_at=now,
            ))
            session.flush()
            session.add(SpendReservationRecord(
                id="sr_test_1", workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID, job_id=JOB_ID,
                semantic_key_hash=CALL_KEY_HASH, state="reserved",
                provider=PROVIDER, execution_mode="byok",
                provider_currency="USD",
                maximum_provider_amount=Decimal("1"),
                budget_currency="USD",
                maximum_budget_amount=Decimal("1"),
                outstanding_budget_amount=Decimal("1"),
                entitlement_version=1, lease_sequence=1,
                guardrail_revision=1, platform_policy_version=1,
                created_at=now, updated_at=now,
            ))
            session.flush()
            # No credential binding or version!
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def test_missing_binding_raises(self):
        ctx = _make_context()
        with self.assertRaises(MissingCredentialBindingError):
            await self.coordinator.execute(
                ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
            )


class TestReservationInvalid(CoordinatorTestBase):
    """5. Reservation not in 'reserved' state raises ReservationInvalidError."""

    def _seed_database(self):
        now = _utcnow()
        session = self._SessionLocal()
        try:
            session.add(WorkspaceRecord(
                id=WORKSPACE_ID, name="Test", created_at=now,
            ))
            session.flush()
            session.add(OperationRecord(
                id=OPERATION_ID, workspace_id=WORKSPACE_ID,
                command_type="generate_image", state="running",
                created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(JobRecord(
                id=JOB_ID, workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID, type="image_generation",
                payload_json={}, idempotency_key="idem_3",
                state="running", initiator_role="owner", created_at=now,
            ))
            session.flush()
            # Reservation already settled (not "reserved")
            session.add(SpendReservationRecord(
                id="sr_test_1", workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID, job_id=JOB_ID,
                semantic_key_hash=CALL_KEY_HASH, state="settled",
                provider=PROVIDER, execution_mode="byok",
                provider_currency="USD",
                maximum_provider_amount=Decimal("1"),
                budget_currency="USD",
                maximum_budget_amount=Decimal("1"),
                outstanding_budget_amount=Decimal("0"),
                entitlement_version=1, lease_sequence=1,
                guardrail_revision=1, platform_policy_version=1,
                created_at=now, updated_at=now,
            ))
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def test_settled_reservation_raises(self):
        ctx = _make_context()
        with self.assertRaises(ReservationInvalidError):
            await self.coordinator.execute(
                ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
            )


class TestAttemptRecordCreated(CoordinatorTestBase):
    """6. Attempt record is created in prepare phase."""

    async def test_attempt_record_exists_after_execute(self):
        ctx = _make_context()
        outcome = await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        session = self._SessionLocal()
        attempts = session.query(ProviderCallAttemptRecord).filter_by(
            workspace_id=WORKSPACE_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
        ).all()

        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.id, outcome.attempt_id)
        self.assertEqual(attempt.job_id, JOB_ID)
        self.assertEqual(attempt.operation_id, OPERATION_ID)
        self.assertEqual(attempt.dispatch_state, "succeeded")
        self.assertEqual(attempt.provider, PROVIDER)
        self.assertEqual(attempt.attempt_no, 0)
        session.close()


class TestLedgerEntryCreated(CoordinatorTestBase):
    """7. Ledger entry is created in complete phase on success."""

    async def test_ledger_entry_on_success(self):
        ctx = _make_context()
        await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        session = self._SessionLocal()
        entries = session.query(ProviderCostLedgerEntryRecord).filter_by(
            workspace_id=WORKSPACE_ID,
        ).all()

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.entry_type, "estimated_settlement")
        self.assertEqual(entry.direction, "increase")
        self.assertEqual(entry.provider, PROVIDER)
        self.assertEqual(entry.spend_reservation_id, "sr_test_1")
        self.assertEqual(entry.operation_id, OPERATION_ID)
        self.assertEqual(entry.job_id, JOB_ID)
        session.close()


class TestNoLedgerEntryOnFailure(CoordinatorTestBase):
    """No ledger entry should be created when dispatch fails."""

    def setUp(self):
        super().setUp()
        self.adapter = FakeProviderAdapter(fail=True, certainty="not_dispatched")
        self.coordinator = ExternalCallCoordinator(
            session_factory=self.session_factory,
            protector=self.protector,
            adapter=self.adapter,
        )

    async def test_no_ledger_entry_on_failure(self):
        ctx = _make_context()
        await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        session = self._SessionLocal()
        entries = session.query(ProviderCostLedgerEntryRecord).filter_by(
            workspace_id=WORKSPACE_ID,
        ).all()
        self.assertEqual(len(entries), 0)
        session.close()


class TestProviderAdapterError(CoordinatorTestBase):
    """8. Provider adapter unexpected error is handled gracefully."""

    def setUp(self):
        super().setUp()

        class ExplodingAdapter:
            @property
            def capabilities(self):
                return ProviderCapabilities(
                    accepts_idempotency_key=True,
                    supports_result_lookup_by_request_id=False,
                )

            async def execute(self, request):
                raise RuntimeError("connection reset")

        self.adapter = ExplodingAdapter()
        self.coordinator = ExternalCallCoordinator(
            session_factory=self.session_factory,
            protector=self.protector,
            adapter=self.adapter,
        )

    async def test_unexpected_error_handled(self):
        ctx = _make_context()
        outcome = await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.dispatch_certainty, "may_have_dispatched")
        self.assertEqual(outcome.error_code, "unexpected_error")
        self.assertIn("connection reset", outcome.error_detail)

        # Reservation should be uncertain.
        session = self._SessionLocal()
        reservation = session.query(SpendReservationRecord).filter_by(
            id="sr_test_1"
        ).one()
        self.assertEqual(reservation.state, "uncertain")
        session.close()


class TestMissingReservation(CoordinatorTestBase):
    """No reservation found raises ReservationInvalidError."""

    async def test_missing_reservation_raises(self):
        ctx = _make_context()
        with self.assertRaises(ReservationInvalidError):
            await self.coordinator.execute(
                ctx, job_id="nonexistent_job", call_semantic_key_hash="nope",
            )


class TestOutcomeFields(CoordinatorTestBase):
    """Verify all ExternalCallOutcome fields are properly populated."""

    async def test_outcome_fields_on_success(self):
        ctx = _make_context()
        outcome = await self.coordinator.execute(
            ctx, job_id=JOB_ID, call_semantic_key_hash=CALL_KEY_HASH,
        )

        self.assertIsInstance(outcome, ExternalCallOutcome)
        self.assertTrue(outcome.attempt_id.startswith("pca_"))
        self.assertEqual(outcome.call_semantic_key_hash, CALL_KEY_HASH)
        self.assertEqual(outcome.dispatch_certainty, "dispatched")
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.provider_request_id, "prov_req_123")
        self.assertIsNone(outcome.error_code)
        self.assertIsNone(outcome.error_detail)


if __name__ == "__main__":
    unittest.main()
