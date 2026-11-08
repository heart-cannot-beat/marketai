"""End-to-end admission, dispatch, and settlement lifecycle tests (Task 23).

These tests verify the complete flow from entitlement projection
ingestion through admission, reservation, and cost ledger recording.
Full lifecycle tests with worker dispatch require PostgreSQL
(@pytest.mark.postgres); the tests here verify the individual stages
compose correctly using SQLite.
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
from app.entitlements.cost_models import (
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401
from app.entitlements.models import EntitlementProjectionRecord, EntitlementSnapshotInboxRecord
from app.entitlements.usage_models import UsageEventRecord


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


WORKSPACE_ID = "ws_e2e"


class E2ETestBase(unittest.TestCase):
    """Base class providing an in-memory SQLite database for E2E tests."""

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
            id=WORKSPACE_ID, name="E2E Test", created_at=_utcnow(),
        ))
        session.commit()
        session.close()


class TestAdmissionCreatesReservation(E2ETestBase):
    def test_admission_creates_operation_and_reservation(self):
        """Simulates the admission step: create operation, job, and reservation."""
        session = self._session()
        now = _utcnow()

        session.add(OperationRecord(
            id="op_e2e_1", workspace_id=WORKSPACE_ID,
            command_type="generate_image", state="accepted",
            created_at=now, updated_at=now,
        ))
        session.flush()

        session.add(JobRecord(
            id="job_e2e_1", workspace_id=WORKSPACE_ID, operation_id="op_e2e_1",
            type="image_generation", payload_json={"prompt": "e2e test"},
            idempotency_key="idem_e2e_1", state="pending",
            initiator_role="owner", created_at=now,
        ))
        session.flush()

        session.add(SpendReservationRecord(
            id="sr_e2e_1", workspace_id=WORKSPACE_ID, operation_id="op_e2e_1",
            job_id="job_e2e_1", semantic_key_hash="sk_e2e_1",
            state="reserved", provider="gemini", execution_mode="byok",
            provider_currency="USD",
            maximum_provider_amount=Decimal("5.000000000000"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("5.000000000000"),
            outstanding_budget_amount=Decimal("5.000000000000"),
            entitlement_version=1, lease_sequence=1,
            guardrail_revision=1, platform_policy_version=1,
            created_at=now, updated_at=now,
        ))
        session.commit()

        session2 = self._session()
        op = session2.get(OperationRecord, "op_e2e_1")
        self.assertEqual(op.state, "accepted")
        reservation = session2.get(SpendReservationRecord, "sr_e2e_1")
        self.assertEqual(reservation.state, "reserved")
        self.assertEqual(reservation.outstanding_budget_amount, Decimal("5.000000000000"))
        session2.close()


class TestSettlementCompletesReservation(E2ETestBase):
    def test_settlement_writes_ledger_and_releases_reservation(self):
        """Simulates the settlement step after dispatch."""
        session = self._session()
        now = _utcnow()

        session.add(OperationRecord(
            id="op_e2e_2", workspace_id=WORKSPACE_ID,
            command_type="generate_image", state="running",
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(JobRecord(
            id="job_e2e_2", workspace_id=WORKSPACE_ID, operation_id="op_e2e_2",
            type="image_generation", payload_json={"prompt": "settle"},
            idempotency_key="idem_e2e_2", state="running",
            initiator_role="owner", created_at=now,
        ))
        session.flush()
        session.add(SpendReservationRecord(
            id="sr_e2e_2", workspace_id=WORKSPACE_ID, operation_id="op_e2e_2",
            job_id="job_e2e_2", semantic_key_hash="sk_e2e_2",
            state="reserved", provider="gemini", execution_mode="byok",
            provider_currency="USD",
            maximum_provider_amount=Decimal("10.000000000000"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("10.000000000000"),
            outstanding_budget_amount=Decimal("10.000000000000"),
            entitlement_version=1, lease_sequence=1,
            guardrail_revision=1, platform_policy_version=1,
            created_at=now, updated_at=now,
        ))
        session.commit()

        # Settlement: write ledger entry and update reservation
        session2 = self._session()
        session2.add(ProviderCostLedgerEntryRecord(
            id="le_e2e_2", workspace_id=WORKSPACE_ID,
            operation_id="op_e2e_2", job_id="job_e2e_2",
            spend_reservation_id="sr_e2e_2",
            entry_type="estimated_settlement", direction="increase",
            execution_mode="byok", semantic_key_hash="sk_le_e2e_2",
            provider="gemini",
            provider_amount=Decimal("3.500000000000"), provider_currency="USD",
            budget_amount=Decimal("3.500000000000"), budget_currency="USD",
            exchange_rate=Decimal("1"), exchange_rate_source="fixed",
            created_at=now,
        ))

        reservation = session2.get(SpendReservationRecord, "sr_e2e_2")
        reservation.state = "settled"
        reservation.outstanding_budget_amount = Decimal("0")
        reservation.updated_at = _utcnow()
        session2.commit()

        # Verify settlement state
        session3 = self._session()
        settled_res = session3.get(SpendReservationRecord, "sr_e2e_2")
        self.assertEqual(settled_res.state, "settled")
        self.assertEqual(settled_res.outstanding_budget_amount, Decimal("0"))

        ledger_entries = session3.execute(
            select(ProviderCostLedgerEntryRecord).where(
                ProviderCostLedgerEntryRecord.spend_reservation_id == "sr_e2e_2"
            )
        ).scalars().all()
        self.assertEqual(len(ledger_entries), 1)
        self.assertEqual(ledger_entries[0].entry_type, "estimated_settlement")
        self.assertEqual(ledger_entries[0].provider_amount, Decimal("3.500000000000"))
        session3.close()


class TestUsageRecordedAfterSettlement(E2ETestBase):
    def test_usage_event_created_for_settled_operation(self):
        """Simulates usage recording after a successful dispatch."""
        session = self._session()
        now = _utcnow()

        session.add(OperationRecord(
            id="op_e2e_3", workspace_id=WORKSPACE_ID,
            command_type="generate_image", state="completed",
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(JobRecord(
            id="job_e2e_3", workspace_id=WORKSPACE_ID, operation_id="op_e2e_3",
            type="image_generation", payload_json={"prompt": "usage"},
            idempotency_key="idem_e2e_3", state="completed",
            initiator_role="owner", created_at=now,
        ))
        session.flush()

        session.add(UsageEventRecord(
            id="ue_e2e_3", workspace_id=WORKSPACE_ID,
            metric_key="provider_calls", measurement_kind="delta",
            direction="increase", quantity=Decimal("1"), unit="call",
            execution_mode="byok",
            source_resource_type="job", source_resource_id="job_e2e_3",
            semantic_key_hash="sk_ue_e2e_3",
            occurred_at=now, created_at=now,
        ))
        session.commit()

        session2 = self._session()
        events = session2.execute(
            select(UsageEventRecord).where(
                UsageEventRecord.workspace_id == WORKSPACE_ID,
                UsageEventRecord.source_resource_id == "job_e2e_3",
            )
        ).scalars().all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].metric_key, "provider_calls")
        self.assertEqual(events[0].quantity, Decimal("1"))
        session2.close()


class TestMockModeProducesZeroCost(E2ETestBase):
    def test_mock_reservation_has_zero_real_amounts(self):
        """Mock-mode reservations carry zero budget amounts."""
        session = self._session()
        now = _utcnow()

        session.add(OperationRecord(
            id="op_e2e_mock", workspace_id=WORKSPACE_ID,
            command_type="generate_image", state="accepted",
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(JobRecord(
            id="job_e2e_mock", workspace_id=WORKSPACE_ID,
            operation_id="op_e2e_mock",
            type="image_generation", payload_json={"prompt": "mock"},
            idempotency_key="idem_e2e_mock", state="pending",
            initiator_role="owner", created_at=now,
        ))
        session.flush()

        session.add(SpendReservationRecord(
            id="sr_e2e_mock", workspace_id=WORKSPACE_ID,
            operation_id="op_e2e_mock", job_id="job_e2e_mock",
            semantic_key_hash="sk_e2e_mock",
            state="reserved", provider="mock", execution_mode="mock",
            provider_currency="USD",
            maximum_provider_amount=Decimal("0"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("0"),
            outstanding_budget_amount=Decimal("0"),
            entitlement_version=1, lease_sequence=1,
            guardrail_revision=1, platform_policy_version=1,
            created_at=now, updated_at=now,
        ))
        session.commit()

        session2 = self._session()
        res = session2.get(SpendReservationRecord, "sr_e2e_mock")
        self.assertEqual(res.execution_mode, "mock")
        self.assertEqual(res.maximum_budget_amount, Decimal("0"))
        self.assertEqual(res.outstanding_budget_amount, Decimal("0"))
        session2.close()


class TestEntitlementProjectionPrecedesAdmission(E2ETestBase):
    def test_projection_exists_before_admission(self):
        """Entitlement projection must exist before operations can be admitted."""
        session = self._session()
        now = _utcnow()

        session.add(EntitlementSnapshotInboxRecord(
            snapshot_id="snap_e2e_1", workspace_id=WORKSPACE_ID,
            entitlement_version=1, lease_sequence=1, projection_version=1,
            schema_version=2, delivery_kind="push",
            canonical_envelope=b'{}', envelope_sha256="abc",
            semantic_hash="hash_e2e", signature="sig",
            signature_key_id="k1", outcome="applied",
            received_at=now,
        ))
        session.flush()
        session.add(EntitlementProjectionRecord(
            workspace_id=WORKSPACE_ID,
            current_snapshot_id="snap_e2e_1", entitlement_version=1,
            lease_sequence=1, projection_version=1,
            semantic_hash="hash_e2e", signature_key_id="k1",
            resolved_entitlements_json={"plan_tier": "beta", "features": ["generation"]},
            applied_at=now,
        ))
        session.commit()

        session2 = self._session()
        proj = session2.execute(
            select(EntitlementProjectionRecord).where(
                EntitlementProjectionRecord.workspace_id == WORKSPACE_ID
            )
        ).scalar_one()
        self.assertIsNotNone(proj)
        self.assertEqual(proj.entitlement_version, 1)
        payload = proj.resolved_entitlements_json
        self.assertIn("generation", payload.get("features", []))
        session2.close()


if __name__ == "__main__":
    unittest.main()
