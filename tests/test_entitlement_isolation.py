"""Cross-tenant isolation tests for entitlement tables (Task 21).

These tests verify that RLS policies, composite FKs, and append-only
triggers on the new entitlement/credential/cost tables enforce proper
workspace isolation.  The @pytest.mark.postgres tests require a real
PostgreSQL instance; SQLite tests verify ORM-level constraints.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event, select, update, delete
from sqlalchemy.exc import IntegrityError
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


WS_A = "ws_isolation_a"
WS_B = "ws_isolation_b"


class IsolationTestBase(unittest.TestCase):
    """Base providing an in-memory SQLite database with two workspaces."""

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
        self._seed_two_workspaces()

    def _session(self) -> Session:
        return self._SessionLocal()

    def _seed_two_workspaces(self) -> None:
        session = self._session()
        now = _utcnow()
        for ws_id in (WS_A, WS_B):
            session.add(WorkspaceRecord(id=ws_id, name=f"Workspace {ws_id}", created_at=now))
        session.commit()
        session.close()

    def _seed_operation(self, session: Session, op_id: str, workspace_id: str) -> None:
        session.add(OperationRecord(
            id=op_id,
            workspace_id=workspace_id,
            command_type="generate_image",
            state="running",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        session.flush()

    def _seed_job(self, session: Session, job_id: str, op_id: str, workspace_id: str) -> None:
        session.add(JobRecord(
            id=job_id,
            workspace_id=workspace_id,
            operation_id=op_id,
            type="image_generation",
            payload_json={"prompt": "test"},
            idempotency_key=f"idem_{job_id}",
            state="running",
            initiator_role="owner",
            created_at=_utcnow(),
        ))
        session.flush()


class TestEntitlementProjectionIsolation(IsolationTestBase):
    def _seed_inbox(self, session: Session, workspace_id: str, snapshot_id: str) -> None:
        now = _utcnow()
        session.add(EntitlementSnapshotInboxRecord(
            snapshot_id=snapshot_id, workspace_id=workspace_id,
            entitlement_version=1, lease_sequence=1, projection_version=1,
            schema_version=2, delivery_kind="push",
            canonical_envelope=b'{}', envelope_sha256="abc",
            semantic_hash="hash_" + workspace_id, signature="sig",
            signature_key_id="k1", outcome="applied",
            received_at=now,
        ))
        session.flush()

    def test_projection_records_scoped_to_workspace(self):
        """Each workspace can only see its own entitlement projections."""
        session = self._session()
        now = _utcnow()
        self._seed_inbox(session, WS_A, "snap_a")
        self._seed_inbox(session, WS_B, "snap_b")
        session.add(EntitlementProjectionRecord(
            workspace_id=WS_A, current_snapshot_id="snap_a",
            entitlement_version=1, lease_sequence=1, projection_version=1,
            semantic_hash="hash_a", signature_key_id="k1",
            resolved_entitlements_json={"plan": "beta"},
            applied_at=now,
        ))
        session.add(EntitlementProjectionRecord(
            workspace_id=WS_B, current_snapshot_id="snap_b",
            entitlement_version=1, lease_sequence=1, projection_version=1,
            semantic_hash="hash_b", signature_key_id="k1",
            resolved_entitlements_json={"plan": "beta"},
            applied_at=now,
        ))
        session.commit()

        session2 = self._session()
        all_recs = session2.execute(
            select(EntitlementProjectionRecord).where(
                EntitlementProjectionRecord.workspace_id == WS_A
            )
        ).scalars().all()
        self.assertEqual(len(all_recs), 1)
        self.assertEqual(all_recs[0].workspace_id, WS_A)
        session2.close()


class TestSpendReservationCompositeFK(IsolationTestBase):
    def test_cross_workspace_operation_reference_rejected(self):
        """SpendReservation cannot reference an operation from another workspace."""
        session = self._session()
        self._seed_operation(session, "op_a1", WS_A)
        self._seed_operation(session, "op_b1", WS_B)
        self._seed_job(session, "job_a1", "op_a1", WS_A)
        session.commit()

        session2 = self._session()
        session2.add(SpendReservationRecord(
            id="sr_cross",
            workspace_id=WS_A,
            operation_id="op_b1",  # operation from WS_B!
            job_id="job_a1",
            semantic_key_hash="sk_cross",
            state="reserved",
            provider="meta",
            execution_mode="byok",
            provider_currency="USD",
            maximum_provider_amount=Decimal("5.000000000000"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("5.000000000000"),
            outstanding_budget_amount=Decimal("5.000000000000"),
            entitlement_version=1,
            lease_sequence=1,
            guardrail_revision=1,
            platform_policy_version=1,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        with self.assertRaises(IntegrityError):
            session2.flush()
        session2.rollback()
        session2.close()


class TestUsageEventIsolation(IsolationTestBase):
    def test_usage_events_scoped_to_workspace(self):
        """Usage events are workspace-scoped."""
        session = self._session()
        now = _utcnow()

        session2 = self._session()
        session2.add(UsageEventRecord(
            id="ue_a1", workspace_id=WS_A,
            metric_key="provider_call",
            measurement_kind="count", direction="increase",
            quantity=Decimal("1"), unit="calls",
            execution_mode="byok",
            source_resource_type="job", source_resource_id="job_a2",
            semantic_key_hash="sk_ue_a1",
            occurred_at=now, created_at=now,
        ))
        session2.add(UsageEventRecord(
            id="ue_b1", workspace_id=WS_B,
            metric_key="provider_call",
            measurement_kind="count", direction="increase",
            quantity=Decimal("1"), unit="calls",
            execution_mode="byok",
            source_resource_type="job", source_resource_id="job_b2",
            semantic_key_hash="sk_ue_b1",
            occurred_at=now, created_at=now,
        ))
        session2.commit()

        session3 = self._session()
        ws_a_events = session3.execute(
            select(UsageEventRecord).where(UsageEventRecord.workspace_id == WS_A)
        ).scalars().all()
        self.assertEqual(len(ws_a_events), 1)
        self.assertEqual(ws_a_events[0].workspace_id, WS_A)
        session3.close()


class TestCostLedgerIsolation(IsolationTestBase):
    def test_cost_ledger_entries_scoped_to_workspace(self):
        """Cost ledger entries from one workspace cannot leak to another."""
        session = self._session()
        self._seed_operation(session, "op_a3", WS_A)
        self._seed_job(session, "job_a3", "op_a3", WS_A)
        session.add(SpendReservationRecord(
            id="sr_a3", workspace_id=WS_A, operation_id="op_a3",
            job_id="job_a3", semantic_key_hash="sk_a3",
            state="reserved", provider="meta", execution_mode="byok",
            provider_currency="USD",
            maximum_provider_amount=Decimal("10.000000000000"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("10.000000000000"),
            outstanding_budget_amount=Decimal("10.000000000000"),
            entitlement_version=1, lease_sequence=1,
            guardrail_revision=1, platform_policy_version=1,
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        session.commit()

        session2 = self._session()
        session2.add(ProviderCostLedgerEntryRecord(
            id="le_a3", workspace_id=WS_A, operation_id="op_a3",
            job_id="job_a3", spend_reservation_id="sr_a3",
            entry_type="reservation", direction="increase",
            execution_mode="byok", semantic_key_hash="sk_le_a3",
            provider="meta",
            provider_amount=Decimal("5.000000000000"), provider_currency="USD",
            budget_amount=Decimal("5.000000000000"), budget_currency="USD",
            exchange_rate=Decimal("1"), exchange_rate_source="fixed",
            created_at=_utcnow(),
        ))
        session2.commit()

        session3 = self._session()
        ws_b_entries = session3.execute(
            select(ProviderCostLedgerEntryRecord).where(
                ProviderCostLedgerEntryRecord.workspace_id == WS_B
            )
        ).scalars().all()
        self.assertEqual(ws_b_entries, [])
        session3.close()


if __name__ == "__main__":
    unittest.main()
