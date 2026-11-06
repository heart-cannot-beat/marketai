"""Tests for create_run with PaidOperationAdmission (Task 17).

Verifies both the new admission-gated path (MARKETAI_ADMISSION_CONTRACT_VERSION >= 1)
and the legacy path (version == 0), ensuring both persist runs and assets correctly
and that the admission path creates OperationRecords, JobRecords, and
SpendReservationRecords while skipping the old CostLedgerRecord writes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    AssetRecord,
    CostLedgerRecord,
    GenerationRunRecord,
    JobRecord,
    OperationRecord,
    PlanEntryRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.cost_models import (
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401 -- FK target
from app.entitlements.models import EntitlementProjectionRecord
from app.platform.tenancy import TenantContext
from app.schemas import Asset, Run
from app import store, config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WS_ID = "ws_gen_admission_test"
INITIATOR = "user_gen_test"
# Keep the entitlement lease valid for the duration of this test regardless of
# when CI runs it; a fixed historical timestamp silently expires over time.
NOW = datetime.now(timezone.utc)
PRODUCT_ID = "prod_ga_test"
PLAN_ID = "plan_ga_test"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _owner_context() -> TenantContext:
    return TenantContext(
        workspace_id=WS_ID,
        role="owner",
        initiator_id=INITIATOR,
        initiator_role="owner",
    )


def _make_run(
    *,
    provider: str = "gemini",
    model: str = "gemini-3.1-flash-image",
    asset_count: int = 2,
    est_cost: Decimal | None = None,
    run_id: str | None = None,
) -> Run:
    """Create a Run schema with pending assets."""
    rid = run_id or _new_id("run")
    if est_cost is None:
        est_cost = Decimal("0.04") * asset_count
    assets = [
        Asset(
            id=_new_id("asset"),
            run_id=rid,
            combo_id="combo_1",
            combo_code="A1",
            tags={},
            filename=f"img_{i}.png",
            provider=provider,
            model=model,
            prompt="a test prompt",
            fingerprint=_new_id("fp"),
            status="pending",
        )
        for i in range(asset_count)
    ]
    return Run(
        id=rid,
        product_id=PRODUCT_ID,
        plan_id=PLAN_ID,
        provider=provider,
        model=model,
        aspect_ratio="1:1",
        image_size="1024x1024",
        variants=1,
        use_ref=False,
        status="queued",
        total=asset_count,
        est_cost=est_cost,
        currency="USD",
        assets=assets,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    from sqlalchemy import event as sa_event

    eng = create_engine("sqlite://", echo=False)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    sess = Session(bind=engine, expire_on_commit=False)
    yield sess
    sess.close()


@pytest.fixture()
def seeded_session(session, monkeypatch):
    """Seed workspace, product, plan, entitlement projection, and guardrail."""
    # The entitlement lease and ledger fixtures use NOW. Pin admission's
    # clock to that same instant so these tests do not expire with the calendar.
    monkeypatch.setattr("app.entitlements.admission._utcnow", lambda: NOW)
    monkeypatch.setattr("app.entitlements.reservation_book._utcnow", lambda: NOW)
    # Workspace -- must come first due to FK dependencies.
    session.add(WorkspaceRecord(
        id=WS_ID,
        name="Gen Admission Test",
        created_at=NOW,
        lifecycle="active",
        budget_currency="USD",
    ))
    session.flush()

    # Product
    session.add(ProductRecord(
        id=PRODUCT_ID,
        workspace_id=WS_ID,
        name="Test Product",
        created_at=NOW,
        updated_at=NOW,
    ))
    session.flush()

    # Plan
    session.add(PlanRecord(
        id=PLAN_ID,
        workspace_id=WS_ID,
        product_id=PRODUCT_ID,
        mode="full",
        cap=100,
        total_full=10,
        snapshot_hash="abc",
        created_at=NOW,
    ))
    session.flush()

    # Plan entry for combo_1
    session.add(PlanEntryRecord(
        id=_new_id("pe"),
        plan_id=PLAN_ID,
        combo_id="combo_1",
        combo_code="A1",
        combo_fingerprint="fp_combo_1",
        values_json={"style": "modern"},
        tags_json={},
        position=0,
    ))
    session.flush()

    # Entitlement projection
    session.add(EntitlementProjectionRecord(
        workspace_id=WS_ID,
        entitlement_version=1,
        lease_sequence=1,
        projection_version=1,
        schema_version=2,
        semantic_hash="hash123",
        valid_until=NOW + timedelta(days=30),
        issued_at=NOW - timedelta(days=1),
        period_start=NOW - timedelta(days=1),
        period_end=NOW + timedelta(days=29),
        resolved_entitlements_json={
            "module.generate.enabled": True,
        },
    ))
    session.flush()

    # Spend guardrail
    session.add(SpendGuardrailRecord(
        workspace_id=WS_ID,
        revision=1,
        budget_currency="USD",
        per_operation_limit=Decimal("100.00"),
        rolling_24h_limit=Decimal("500.00"),
        period_limit=Decimal("5000.00"),
        outstanding_exposure_limit=Decimal("1000.00"),
        created_at=NOW,
        updated_at=NOW,
        updated_by=INITIATOR,
    ))

    session.commit()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateRunAdmission:
    """Tests for the admission-gated path (version >= 1)."""

    def test_create_run_with_admission_creates_operation_and_reservations(
        self, seeded_session,
    ):
        """Happy path: admission creates OperationRecord, JobRecords,
        SpendReservationRecords and still persists the Run + Assets."""
        ctx = _owner_context()
        run = _make_run(asset_count=2)

        with mock.patch.object(config, "MARKETAI_ADMISSION_CONTRACT_VERSION", 1):
            with mock.patch(
                "app.store.tenant_session",
                return_value=_session_context_manager(seeded_session, ctx),
            ):
                    store.create_run(ctx, run)

        # Run was created.
        run_rec = seeded_session.get(GenerationRunRecord, run.id)
        assert run_rec is not None
        assert run_rec.provider == "gemini"

        # Assets were created.
        assets = seeded_session.scalars(
            select(AssetRecord).where(AssetRecord.run_id == run.id)
        ).all()
        assert len(assets) == 2

        # OperationRecord was created by admission.
        ops = seeded_session.scalars(
            select(OperationRecord).where(
                OperationRecord.workspace_id == WS_ID,
                OperationRecord.command_type == "generate_image",
            )
        ).all()
        assert len(ops) == 1
        assert ops[0].state == "admitted"

        # JobRecords were created by admission (one per pending asset).
        jobs = seeded_session.scalars(
            select(JobRecord).where(
                JobRecord.workspace_id == WS_ID,
                JobRecord.operation_id == ops[0].id,
            )
        ).all()
        assert len(jobs) == 2

        # SpendReservationRecords were created (one per billable call, non-mock).
        reservations = seeded_session.scalars(
            select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == WS_ID,
                SpendReservationRecord.operation_id == ops[0].id,
            )
        ).all()
        assert len(reservations) == 2

        # Old CostLedgerRecord should NOT have been written.
        cost_entries = seeded_session.scalars(
            select(CostLedgerRecord).where(
                CostLedgerRecord.workspace_id == WS_ID,
                CostLedgerRecord.run_id == run.id,
            )
        ).all()
        assert len(cost_entries) == 0

    def test_create_run_mock_skips_spend_reservation(self, seeded_session):
        """Mock provider creates OperationRecord + JobRecords but no SpendReservation."""
        ctx = _owner_context()
        run = _make_run(provider="mock", model="mock", est_cost=Decimal("0"))

        with mock.patch.object(config, "MARKETAI_ADMISSION_CONTRACT_VERSION", 1):
            with mock.patch(
                "app.store.tenant_session",
                return_value=_session_context_manager(seeded_session, ctx),
            ):
                    store.create_run(ctx, run)

        # Run was created.
        run_rec = seeded_session.get(GenerationRunRecord, run.id)
        assert run_rec is not None

        # OperationRecord exists.
        ops = seeded_session.scalars(
            select(OperationRecord).where(
                OperationRecord.workspace_id == WS_ID,
            )
        ).all()
        assert len(ops) == 1

        # No SpendReservationRecords should exist.
        reservations = seeded_session.scalars(
            select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == WS_ID,
            )
        ).all()
        assert len(reservations) == 0

    def test_create_run_admission_replay_is_idempotent(self, seeded_session):
        """Calling create_run twice with the same Run returns without error
        and does not duplicate the Run record."""
        ctx = _owner_context()
        run = _make_run(asset_count=1)

        with mock.patch.object(config, "MARKETAI_ADMISSION_CONTRACT_VERSION", 1):
            with mock.patch(
                "app.store.tenant_session",
                return_value=_session_context_manager(seeded_session, ctx),
            ):
                    store.create_run(ctx, run)

        # Second call with same run id should be a no-op.
        with mock.patch.object(config, "MARKETAI_ADMISSION_CONTRACT_VERSION", 1):
            with mock.patch(
                "app.store.tenant_session",
                return_value=_session_context_manager(seeded_session, ctx),
            ):
                    store.create_run(ctx, run)

        # Only one Run.
        runs = seeded_session.scalars(
            select(GenerationRunRecord).where(
                GenerationRunRecord.workspace_id == WS_ID,
            )
        ).all()
        assert len(runs) == 1


class TestCreateRunLegacy:
    """Tests for the legacy path (version == 0)."""

    def test_create_run_legacy_path_still_works(self, seeded_session):
        """Legacy path (version 0) creates Run, Assets, enqueues jobs, writes CostLedger."""
        ctx = _owner_context()
        run = _make_run(asset_count=1, est_cost=Decimal("0.04"))

        with mock.patch.object(config, "MARKETAI_ADMISSION_CONTRACT_VERSION", 0):
            with mock.patch(
                "app.store.tenant_session",
                return_value=_session_context_manager(seeded_session, ctx),
            ):
                    store.create_run(ctx, run)

        # Run was created.
        run_rec = seeded_session.get(GenerationRunRecord, run.id)
        assert run_rec is not None

        # Assets were created.
        assets = seeded_session.scalars(
            select(AssetRecord).where(AssetRecord.run_id == run.id)
        ).all()
        assert len(assets) == 1

        # Legacy CostLedgerRecord should have been written.
        cost_entries = seeded_session.scalars(
            select(CostLedgerRecord).where(
                CostLedgerRecord.run_id == run.id,
                CostLedgerRecord.entry_type == "reserved",
            )
        ).all()
        assert len(cost_entries) == 1
        assert cost_entries[0].amount == Decimal("0.04")

        # Legacy path should create jobs via enqueue_in_session.
        jobs = seeded_session.scalars(
            select(JobRecord).where(
                JobRecord.workspace_id == WS_ID,
                JobRecord.resource_id == run.id,
            )
        ).all()
        assert len(jobs) == 1

        # No OperationRecord from the legacy path.
        ops = seeded_session.scalars(
            select(OperationRecord).where(
                OperationRecord.workspace_id == WS_ID,
            )
        ).all()
        assert len(ops) == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _session_context_manager:
    """Mimic tenant_session() context manager for testing.

    Binds the TenantContext to the session's info dict so that
    ``current_context(session)`` works as in production.
    """

    def __init__(self, session: Session, context: TenantContext | None = None) -> None:
        self._session = session
        self._context = context

    def __enter__(self) -> Session:
        if self._context is not None:
            self._session.info["tenant_context"] = self._context
        return self._session

    def __exit__(self, *args):
        # Don't close or commit -- test controls the session lifecycle.
        self._session.flush()
