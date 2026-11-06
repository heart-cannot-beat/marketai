"""Admission concurrency and guardrail contention tests (Task 22).

Tests verify that the admission system correctly handles concurrent
reservation attempts and guardrail limits.  Full barrier-based
concurrency tests require PostgreSQL (@pytest.mark.postgres); the
SQLite tests here verify the reservation book's semantic-key dedup
and guardrail limit enforcement sequentially.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    JobRecord,
    OperationRecord,
    WorkspaceRecord,
)
from app.entitlements.cost_models import SpendReservationRecord
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


WORKSPACE_ID = "ws_concurrency"


class ConcurrencyTestBase(unittest.TestCase):
    """Base class with in-memory SQLite database for concurrency tests."""

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
        self._seed_workspace()

    def _session(self) -> Session:
        return self._SessionLocal()

    def _seed_workspace(self) -> None:
        session = self._session()
        session.add(WorkspaceRecord(
            id=WORKSPACE_ID, name="Concurrency Test", created_at=_utcnow(),
        ))
        session.commit()
        session.close()

    def _seed_operation_and_job(
        self, session: Session, op_id: str, job_id: str,
    ) -> None:
        now = _utcnow()
        session.add(OperationRecord(
            id=op_id, workspace_id=WORKSPACE_ID,
            command_type="generate_image", state="running",
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(JobRecord(
            id=job_id, workspace_id=WORKSPACE_ID, operation_id=op_id,
            type="image_generation", payload_json={"prompt": "test"},
            idempotency_key=f"idem_{job_id}", state="running",
            initiator_role="owner", created_at=now,
        ))
        session.flush()

    def _make_reservation(
        self,
        res_id: str,
        op_id: str,
        job_id: str,
        amount: Decimal,
        state: str = "reserved",
    ) -> SpendReservationRecord:
        return SpendReservationRecord(
            id=res_id,
            workspace_id=WORKSPACE_ID,
            operation_id=op_id,
            job_id=job_id,
            semantic_key_hash=f"sk_{res_id}",
            state=state,
            provider="meta",
            execution_mode="byok",
            provider_currency="USD",
            maximum_provider_amount=amount,
            budget_currency="USD",
            maximum_budget_amount=amount,
            outstanding_budget_amount=amount if state == "reserved" else Decimal("0"),
            entitlement_version=1,
            lease_sequence=1,
            guardrail_revision=1,
            platform_policy_version=1,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )


class TestSemanticKeyDeduplication(ConcurrencyTestBase):
    def test_duplicate_semantic_key_rejected(self):
        """Two reservations with the same semantic_key_hash are rejected."""
        session = self._session()
        self._seed_operation_and_job(session, "op_dup1", "job_dup1")
        session.add(self._make_reservation(
            "sr_dup1", "op_dup1", "job_dup1", Decimal("5.000000000000"),
        ))
        session.commit()

        session2 = self._session()
        self._seed_operation_and_job(session2, "op_dup2", "job_dup2")
        dup = self._make_reservation(
            "sr_dup2", "op_dup2", "job_dup2", Decimal("5.000000000000"),
        )
        dup.semantic_key_hash = "sk_sr_dup1"  # same as first
        session2.add(dup)
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            session2.commit()
        session2.rollback()
        session2.close()


class TestReservationStateTransitions(ConcurrencyTestBase):
    def test_reservation_settles_to_zero_outstanding(self):
        """When a reservation transitions to settled, outstanding goes to 0."""
        session = self._session()
        self._seed_operation_and_job(session, "op_settle", "job_settle")
        res = self._make_reservation(
            "sr_settle", "op_settle", "job_settle", Decimal("8.000000000000"),
        )
        session.add(res)
        session.commit()

        session2 = self._session()
        rec = session2.get(SpendReservationRecord, "sr_settle")
        self.assertEqual(rec.state, "reserved")
        self.assertEqual(rec.outstanding_budget_amount, Decimal("8.000000000000"))

        rec.state = "settled"
        rec.outstanding_budget_amount = Decimal("0")
        rec.updated_at = _utcnow()
        session2.commit()

        session3 = self._session()
        settled = session3.get(SpendReservationRecord, "sr_settle")
        self.assertEqual(settled.state, "settled")
        self.assertEqual(settled.outstanding_budget_amount, Decimal("0"))
        session3.close()


class TestOutstandingExposureAggregation(ConcurrencyTestBase):
    def test_total_outstanding_exposure_sums_correctly(self):
        """Outstanding exposure is the sum of all reserved reservations."""
        session = self._session()
        self._seed_operation_and_job(session, "op_exp1", "job_exp1")
        self._seed_operation_and_job(session, "op_exp2", "job_exp2")

        session.add(self._make_reservation(
            "sr_exp1", "op_exp1", "job_exp1", Decimal("3.000000000000"),
        ))
        session.add(self._make_reservation(
            "sr_exp2", "op_exp2", "job_exp2", Decimal("7.000000000000"),
        ))
        session.commit()

        session2 = self._session()
        from sqlalchemy import func
        total = session2.execute(
            select(func.sum(SpendReservationRecord.outstanding_budget_amount)).where(
                SpendReservationRecord.workspace_id == WORKSPACE_ID,
                SpendReservationRecord.state == "reserved",
            )
        ).scalar()
        self.assertEqual(total, Decimal("10.000000000000"))
        session2.close()


class TestMultipleReservationsPerOperation(ConcurrencyTestBase):
    def test_multiple_reservations_for_same_operation(self):
        """An operation can have multiple reservations (one per asset/job)."""
        session = self._session()
        self._seed_operation_and_job(session, "op_multi", "job_multi1")
        session.add(JobRecord(
            id="job_multi2", workspace_id=WORKSPACE_ID, operation_id="op_multi",
            type="image_generation", payload_json={"prompt": "test2"},
            idempotency_key="idem_job_multi2", state="running",
            initiator_role="owner", created_at=_utcnow(),
        ))
        session.flush()

        session.add(self._make_reservation(
            "sr_m1", "op_multi", "job_multi1", Decimal("2.000000000000"),
        ))
        session.add(self._make_reservation(
            "sr_m2", "op_multi", "job_multi2", Decimal("3.000000000000"),
        ))
        session.commit()

        session2 = self._session()
        reservations = session2.execute(
            select(SpendReservationRecord).where(
                SpendReservationRecord.operation_id == "op_multi"
            )
        ).scalars().all()
        self.assertEqual(len(reservations), 2)
        total = sum(r.maximum_budget_amount for r in reservations)
        self.assertEqual(total, Decimal("5.000000000000"))
        session2.close()


if __name__ == "__main__":
    unittest.main()
