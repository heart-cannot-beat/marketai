"""Tests for the legacy CostLedgerRecord -> LegacyCostEvidence compatibility view.

Task 20: verify that query_compatibility_view maps old CostLedgerRecord rows
into the new ProviderCostLedgerEntryRecord-compatible schema correctly.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    CostLedgerRecord,
    GenerationRunRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
)
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401 (registers table)
from app.entitlements.legacy_bridge import LegacyCostEvidence, query_compatibility_view


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


WORKSPACE_ID = "ws_legacy_test"
OTHER_WORKSPACE_ID = "ws_other_test"


class LegacyBridgeTestBase(unittest.TestCase):
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

    def _seed_workspace(self, session: Session, workspace_id: str = WORKSPACE_ID) -> None:
        session.add(WorkspaceRecord(
            id=workspace_id,
            name="Test Workspace",
            created_at=_utcnow(),
        ))
        session.flush()

    def _seed_product(self, session: Session, product_id: str = "prod_1") -> None:
        session.add(ProductRecord(
            id=product_id,
            workspace_id=WORKSPACE_ID,
            name="Test Product",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        session.flush()

    def _seed_plan(self, session: Session, plan_id: str = "plan_1", product_id: str = "prod_1") -> None:
        session.add(PlanRecord(
            id=plan_id,
            workspace_id=WORKSPACE_ID,
            product_id=product_id,
            mode="full",
            snapshot_hash="abc123",
            created_at=_utcnow(),
        ))
        session.flush()

    def _seed_run(
        self,
        session: Session,
        run_id: str = "run_1",
        provider: str = "meta",
        product_id: str = "prod_1",
        plan_id: str = "plan_1",
    ) -> None:
        session.add(GenerationRunRecord(
            id=run_id,
            workspace_id=WORKSPACE_ID,
            product_id=product_id,
            plan_id=plan_id,
            provider=provider,
            model="test-model",
            aspect_ratio="1:1",
            image_size="1024x1024",
            variants=1,
            use_ref=False,
            status="completed",
            currency="USD",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        session.flush()

    def _seed_cost_entry(
        self,
        session: Session,
        entry_id: str,
        entry_type: str,
        amount: Decimal,
        run_id: str | None = "run_1",
        workspace_id: str = WORKSPACE_ID,
    ) -> None:
        session.add(CostLedgerRecord(
            id=entry_id,
            workspace_id=workspace_id,
            run_id=run_id,
            entry_type=entry_type,
            amount=amount,
            currency="USD",
            created_at=_utcnow(),
        ))
        session.flush()


class TestCompatibilityViewMapsReservedToReservation(LegacyBridgeTestBase):
    def test_compatibility_view_maps_reserved_to_reservation(self):
        session = self._session()
        self._seed_workspace(session)
        self._seed_product(session)
        self._seed_plan(session)
        self._seed_run(session, provider="meta")
        self._seed_cost_entry(session, "cl_1", "reserved", Decimal("1.50"))
        session.commit()

        session2 = self._session()
        results = query_compatibility_view(session2, WORKSPACE_ID)

        self.assertEqual(len(results), 1)
        ev = results[0]
        self.assertIsInstance(ev, LegacyCostEvidence)
        self.assertEqual(ev.original_id, "legacy_cl_1")
        self.assertEqual(ev.entry_type, "reservation")
        self.assertEqual(ev.direction, "increase")
        self.assertEqual(ev.provider, "meta")
        self.assertEqual(ev.provider_amount, Decimal("1.50"))
        self.assertEqual(ev.budget_amount, Decimal("1.50"))
        self.assertEqual(ev.exchange_rate, Decimal("1"))
        self.assertEqual(ev.exchange_rate_source, "legacy_single_currency")
        session2.close()


class TestCompatibilityViewMapsActualToEstimatedSettlement(LegacyBridgeTestBase):
    def test_compatibility_view_maps_actual_to_estimated_settlement(self):
        session = self._session()
        self._seed_workspace(session)
        self._seed_product(session)
        self._seed_plan(session)
        self._seed_run(session, provider="openai")
        self._seed_cost_entry(session, "cl_2", "actual", Decimal("2.00"))
        session.commit()

        session2 = self._session()
        results = query_compatibility_view(session2, WORKSPACE_ID)

        self.assertEqual(len(results), 1)
        ev = results[0]
        self.assertEqual(ev.entry_type, "estimated_settlement")
        self.assertEqual(ev.direction, "increase")
        self.assertEqual(ev.provider, "openai")
        self.assertEqual(ev.provider_amount, Decimal("2.00"))
        self.assertIsNone(ev.simulated_amount)
        session2.close()


class TestCompatibilityViewAssignsMockExecutionMode(LegacyBridgeTestBase):
    def test_compatibility_view_assigns_mock_execution_mode(self):
        session = self._session()
        self._seed_workspace(session)
        self._seed_product(session)
        self._seed_plan(session)
        self._seed_run(session, provider="mock")
        self._seed_cost_entry(session, "cl_3", "reserved", Decimal("0.75"))
        session.commit()

        session2 = self._session()
        results = query_compatibility_view(session2, WORKSPACE_ID)

        self.assertEqual(len(results), 1)
        ev = results[0]
        self.assertEqual(ev.execution_mode, "mock")
        # Mock entries have zero real amounts; original amount goes to simulated_amount
        self.assertEqual(ev.provider_amount, Decimal("0"))
        self.assertEqual(ev.budget_amount, Decimal("0"))
        self.assertEqual(ev.simulated_amount, Decimal("0.75"))
        session2.close()


class TestCompatibilityViewAssignsLegacyPlatformForRealProvider(LegacyBridgeTestBase):
    def test_compatibility_view_assigns_legacy_platform_for_real_provider(self):
        session = self._session()
        self._seed_workspace(session)
        self._seed_product(session)
        self._seed_plan(session)
        self._seed_run(session, provider="meta")
        self._seed_cost_entry(session, "cl_4", "released", Decimal("0.50"))
        session.commit()

        session2 = self._session()
        results = query_compatibility_view(session2, WORKSPACE_ID)

        self.assertEqual(len(results), 1)
        ev = results[0]
        self.assertEqual(ev.execution_mode, "legacy_platform")
        self.assertEqual(ev.direction, "decrease")
        self.assertEqual(ev.entry_type, "release")
        session2.close()


class TestCompatibilityViewDoesNotDuplicateRows(LegacyBridgeTestBase):
    def test_compatibility_view_does_not_duplicate_rows(self):
        """If a CostLedgerRecord has a run_id, the join must not produce
        duplicate rows (e.g. from a bad many-to-many pattern)."""
        session = self._session()
        self._seed_workspace(session)
        self._seed_product(session)
        self._seed_plan(session)
        self._seed_run(session, run_id="run_a", provider="meta")
        self._seed_cost_entry(session, "cl_5a", "reserved", Decimal("1.00"), run_id="run_a")
        self._seed_cost_entry(session, "cl_5b", "actual", Decimal("0.80"), run_id="run_a")
        session.commit()

        session2 = self._session()
        results = query_compatibility_view(session2, WORKSPACE_ID)

        self.assertEqual(len(results), 2)
        ids = {ev.original_id for ev in results}
        self.assertEqual(ids, {"legacy_cl_5a", "legacy_cl_5b"})
        session2.close()


class TestCompatibilityViewReturnsEmptyForUnknownWorkspace(LegacyBridgeTestBase):
    def test_compatibility_view_returns_empty_for_unknown_workspace(self):
        session = self._session()
        self._seed_workspace(session)
        self._seed_product(session)
        self._seed_plan(session)
        self._seed_run(session, provider="meta")
        self._seed_cost_entry(session, "cl_6", "reserved", Decimal("1.00"))
        session.commit()

        session2 = self._session()
        results = query_compatibility_view(session2, "ws_nonexistent")

        self.assertEqual(results, [])
        session2.close()


if __name__ == "__main__":
    unittest.main()
