"""Tests for cost evidence replay and mode isolation.

Task 25: verify that ProviderCostLedgerEntryRecord rows correctly track
balances, adjustment chains, and mock/BYOK mode isolation.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event
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
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401 (registers table)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


WORKSPACE_ID = "ws_cost_evidence"
OPERATION_ID = "op_cost_1"
JOB_ID = "job_cost_1"
RESERVATION_ID = "sr_cost_1"


class CostEvidenceTestBase(unittest.TestCase):
    """Base class providing an in-memory SQLite database with all tables."""

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

    def _session(self) -> Session:
        return self._SessionLocal()

    def _seed_prerequisites(
        self,
        session: Session,
        *,
        workspace_id: str = WORKSPACE_ID,
        operation_id: str = OPERATION_ID,
        job_id: str = JOB_ID,
        reservation_id: str = RESERVATION_ID,
        execution_mode: str = "byok",
    ) -> None:
        """Create workspace, operation, job, and spend reservation."""
        now = _utcnow()

        session.add(WorkspaceRecord(
            id=workspace_id,
            name="Test Workspace",
            created_at=now,
        ))
        session.flush()

        session.add(OperationRecord(
            id=operation_id,
            workspace_id=workspace_id,
            command_type="generate_image",
            state="running",
            created_at=now,
            updated_at=now,
        ))
        session.flush()

        session.add(JobRecord(
            id=job_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            type="image_generation",
            payload_json={"prompt": "test"},
            idempotency_key=f"idem_{job_id}",
            state="running",
            initiator_role="owner",
            created_at=now,
        ))
        session.flush()

        session.add(SpendReservationRecord(
            id=reservation_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            job_id=job_id,
            semantic_key_hash=f"sk_{reservation_id}",
            state="reserved",
            provider="meta",
            execution_mode=execution_mode,
            provider_currency="USD",
            maximum_provider_amount=Decimal("10.000000000000"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("10.000000000000"),
            outstanding_budget_amount=Decimal("10.000000000000"),
            entitlement_version=1,
            lease_sequence=1,
            guardrail_revision=1,
            platform_policy_version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()

    def _make_ledger_entry(
        self,
        entry_id: str,
        *,
        workspace_id: str = WORKSPACE_ID,
        operation_id: str = OPERATION_ID,
        job_id: str = JOB_ID,
        spend_reservation_id: str = RESERVATION_ID,
        entry_type: str = "reservation",
        direction: str = "increase",
        execution_mode: str = "byok",
        provider: str = "meta",
        provider_amount: Decimal = Decimal("5.000000000000"),
        budget_amount: Decimal = Decimal("5.000000000000"),
        simulated_amount: Decimal | None = None,
        adjusts_entry_id: str | None = None,
    ) -> ProviderCostLedgerEntryRecord:
        now = _utcnow()
        return ProviderCostLedgerEntryRecord(
            id=entry_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            job_id=job_id,
            spend_reservation_id=spend_reservation_id,
            entry_type=entry_type,
            direction=direction,
            execution_mode=execution_mode,
            semantic_key_hash=f"sk_ledger_{entry_id}",
            provider=provider,
            provider_amount=provider_amount,
            provider_currency="USD",
            budget_amount=budget_amount,
            budget_currency="USD",
            exchange_rate=Decimal("1"),
            exchange_rate_source="fixed",
            simulated_amount=simulated_amount,
            simulated_currency="USD" if simulated_amount is not None else None,
            adjusts_entry_id=adjusts_entry_id,
            created_at=now,
        )


class TestLedgerReplaysToSameBalance(CostEvidenceTestBase):
    def test_ledger_replays_to_same_balance(self):
        """Create increase/decrease entries and verify the replayed balance
        matches the expected outstanding amount."""
        session = self._session()
        self._seed_prerequisites(session)

        # Increase of 8.00 (reservation)
        session.add(self._make_ledger_entry(
            "le_replay_1",
            entry_type="reservation",
            direction="increase",
            provider_amount=Decimal("8.000000000000"),
            budget_amount=Decimal("8.000000000000"),
        ))
        # Decrease of 3.00 (release)
        session.add(self._make_ledger_entry(
            "le_replay_2",
            entry_type="release",
            direction="decrease",
            provider_amount=Decimal("3.000000000000"),
            budget_amount=Decimal("3.000000000000"),
        ))
        session.commit()

        # Replay: sum increases - sum decreases
        session2 = self._session()
        entries = session2.query(ProviderCostLedgerEntryRecord).filter_by(
            workspace_id=WORKSPACE_ID,
            spend_reservation_id=RESERVATION_ID,
        ).all()

        balance = Decimal("0")
        for e in entries:
            if e.direction == "increase":
                balance += e.budget_amount
            elif e.direction == "decrease":
                balance -= e.budget_amount

        expected = Decimal("8.000000000000") - Decimal("3.000000000000")
        self.assertEqual(balance, expected)
        session2.close()


class TestAdjustmentReferencesOriginalEntry(CostEvidenceTestBase):
    def test_adjustment_references_original_entry(self):
        """An adjustment entry must point back to the original entry via
        adjusts_entry_id."""
        session = self._session()
        self._seed_prerequisites(session)

        original = self._make_ledger_entry(
            "le_original",
            entry_type="estimated_settlement",
            direction="increase",
            provider_amount=Decimal("5.000000000000"),
            budget_amount=Decimal("5.000000000000"),
        )
        session.add(original)
        session.flush()

        adjustment = self._make_ledger_entry(
            "le_adjustment",
            entry_type="adjustment",
            direction="decrease",
            provider_amount=Decimal("1.000000000000"),
            budget_amount=Decimal("1.000000000000"),
            adjusts_entry_id="le_original",
        )
        session.add(adjustment)
        session.commit()

        session2 = self._session()
        adj = session2.get(ProviderCostLedgerEntryRecord, "le_adjustment")
        self.assertIsNotNone(adj)
        self.assertEqual(adj.adjusts_entry_id, "le_original")

        orig = session2.get(ProviderCostLedgerEntryRecord, "le_original")
        self.assertIsNotNone(orig)
        self.assertIsNone(orig.adjusts_entry_id)
        session2.close()


class TestMockAmountsAreZero(CostEvidenceTestBase):
    def test_mock_amounts_are_zero(self):
        """A mock-mode ledger entry must have provider_amount==0 and
        budget_amount==0."""
        session = self._session()
        self._seed_prerequisites(session, execution_mode="mock")

        session.add(self._make_ledger_entry(
            "le_mock_zero",
            entry_type="reservation",
            direction="increase",
            execution_mode="mock",
            provider_amount=Decimal("0"),
            budget_amount=Decimal("0"),
            simulated_amount=Decimal("2.500000000000"),
        ))
        session.commit()

        session2 = self._session()
        entry = session2.get(ProviderCostLedgerEntryRecord, "le_mock_zero")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.provider_amount, Decimal("0"))
        self.assertEqual(entry.budget_amount, Decimal("0"))
        self.assertEqual(entry.execution_mode, "mock")
        session2.close()


class TestMockSimulatedAmountInSeparateField(CostEvidenceTestBase):
    def test_mock_simulated_amount_in_separate_field(self):
        """Mock entries carry the hypothetical cost in simulated_amount, which
        must be > 0."""
        session = self._session()
        self._seed_prerequisites(session, execution_mode="mock")

        simulated = Decimal("3.750000000000")
        session.add(self._make_ledger_entry(
            "le_mock_sim",
            entry_type="reservation",
            direction="increase",
            execution_mode="mock",
            provider_amount=Decimal("0"),
            budget_amount=Decimal("0"),
            simulated_amount=simulated,
        ))
        session.commit()

        session2 = self._session()
        entry = session2.get(ProviderCostLedgerEntryRecord, "le_mock_sim")
        self.assertIsNotNone(entry)
        self.assertGreater(entry.simulated_amount, Decimal("0"))
        self.assertEqual(entry.simulated_amount, simulated)
        session2.close()


class TestByokAmountsArePositive(CostEvidenceTestBase):
    def test_byok_amounts_are_positive(self):
        """A BYOK-mode entry must have provider_amount > 0."""
        session = self._session()
        self._seed_prerequisites(session, execution_mode="byok")

        session.add(self._make_ledger_entry(
            "le_byok",
            entry_type="estimated_settlement",
            direction="increase",
            execution_mode="byok",
            provider_amount=Decimal("4.200000000000"),
            budget_amount=Decimal("4.200000000000"),
        ))
        session.commit()

        session2 = self._session()
        entry = session2.get(ProviderCostLedgerEntryRecord, "le_byok")
        self.assertIsNotNone(entry)
        self.assertGreater(entry.provider_amount, Decimal("0"))
        self.assertEqual(entry.execution_mode, "byok")
        session2.close()


if __name__ == "__main__":
    unittest.main()
