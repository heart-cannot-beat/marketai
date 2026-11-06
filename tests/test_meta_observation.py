"""Observation contract generation and coverage ledger tests (spec §6.1, §6.6-§6.7).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_call_evidence.py`` -- since no shared ``db_session``
fixture exists in ``tests/conftest.py`` for this suite. ``WorkspaceRecord``
has a ``name`` field (not ``display_name``/``slug``), so the workspace
fixture below uses ``name``.

The domain's ``PRAGMA foreign_keys=ON`` (registered globally on the
SQLAlchemy ``Engine`` class by ``app.adapters.persistence.database``, which
this module imports for ``Base``) applies to this scratch engine too, so
``record_coverage``'s ``sync_run_id``/``attestation_id`` must reference real
rows -- ``_backing_evidence`` below builds the minimal valid
Operation -> sync run -> publication -> attestation chain those composite
foreign keys require.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import OperationRecord, WorkspaceRecord, new_id, utcnow
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaConnectionRecord,
    MetaObservationSyncRunRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
)
from app.meta.observation import (
    ContractError,
    calculate_tenure,
    create_contract_generation,
    record_coverage,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-observation-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_observation.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            WorkspaceRecord.__table__,
            OperationRecord.__table__,
            MetaConnectionRecord.__table__,
            ObservationContractGenerationRecord.__table__,
            MetaObservationSyncRunRecord.__table__,
            EvidencePublicationRecord.__table__,
            EvidenceAttestationRecord.__table__,
            ObservationCoverageLedgerRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _setup(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    session.add(ws)
    session.flush()
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ws.id,
        meta_mode="customer_sandbox", ad_account_id="act_1",
    )
    session.add(conn)
    session.flush()
    return ws, conn


def _backing_evidence(session, ws, gen, seq: int) -> tuple[str, str]:
    """Build a minimal valid Operation -> sync run -> publication -> attestation
    chain and return ``(sync_run_id, attestation_id)`` for use in
    ``record_coverage``, satisfying its composite foreign keys.
    """
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    session.add(op)
    session.flush()
    run = MetaObservationSyncRunRecord(
        id=new_id("msync"), workspace_id=ws.id, meta_mode=gen.meta_mode,
        contract_generation_id=gen.id, occurrence_id=f"occ_{seq}",
        run_generation=1, state="succeeded", operation_id=op.id,
        executor="system:worker", completed_at=utcnow(),
    )
    session.add(run)
    session.flush()
    pub = EvidencePublicationRecord(
        id=new_id("epub"), workspace_id=ws.id, meta_mode=gen.meta_mode,
        sync_run_id=run.id, publication_generation=1,
        state="published", operation_id=op.id, published_at=utcnow(),
    )
    session.add(pub)
    session.flush()
    att = EvidenceAttestationRecord(
        id=new_id("eatt"), workspace_id=ws.id, meta_mode=gen.meta_mode,
        contract_generation_id=gen.id, publication_id=pub.id,
        state="valid", evidence_watermark=f"[seq-{seq}]",
        published_at=utcnow(),
    )
    session.add(att)
    session.flush()
    return run.id, att.id


def test_create_first_generation(db_session):
    ws, conn = _setup(db_session)
    gen = create_contract_generation(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1",
        fields=["impressions", "spend"],
        attribution_window={"click": "7d"},
        breakdowns=[], time_window={"granularity": "daily"},
        local_timezone="America/Los_Angeles",
    )
    assert gen.generation_number == 1
    assert gen.state == "active"
    assert gen.clock_started_at is None


def test_duplicate_active_generation_raises(db_session):
    ws, conn = _setup(db_session)
    create_contract_generation(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1",
        fields=["impressions"],
        attribution_window={}, breakdowns=[],
        time_window={}, local_timezone="UTC",
    )
    with pytest.raises(ContractError, match="active generation already exists"):
        create_contract_generation(
            db_session,
            workspace_id=ws.id, meta_mode="customer_sandbox",
            connection_id=conn.id, contract_type="daily_insights",
            ad_account_id="act_1",
            fields=["impressions"],
            attribution_window={}, breakdowns=[],
            time_window={}, local_timezone="UTC",
        )


def test_calculate_tenure_consecutive_dates(db_session):
    ws, conn = _setup(db_session)
    gen = create_contract_generation(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1",
        fields=["impressions"],
        attribution_window={}, breakdowns=[],
        time_window={}, local_timezone="UTC",
    )
    # Record 5 consecutive days of coverage
    for i in range(5):
        sync_run_id, attestation_id = _backing_evidence(db_session, ws, gen, i)
        record_coverage(
            db_session,
            generation_id=gen.id,
            coverage_date=date(2026, 9, 1 + i),
            sync_run_id=sync_run_id,
            attestation_id=attestation_id,
        )
    tenure = calculate_tenure(db_session, gen.id)
    assert tenure == 5


def test_record_coverage_starts_clock_on_first_entry(db_session):
    ws, conn = _setup(db_session)
    gen = create_contract_generation(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1",
        fields=["impressions"],
        attribution_window={}, breakdowns=[],
        time_window={}, local_timezone="UTC",
    )
    assert gen.clock_started_at is None
    sync_run_id, attestation_id = _backing_evidence(db_session, ws, gen, 0)
    record_coverage(
        db_session,
        generation_id=gen.id,
        coverage_date=date(2026, 9, 1),
        sync_run_id=sync_run_id,
        attestation_id=attestation_id,
    )
    assert gen.clock_started_at is not None
    first_clock_start = gen.clock_started_at

    sync_run_id, attestation_id = _backing_evidence(db_session, ws, gen, 1)
    record_coverage(
        db_session,
        generation_id=gen.id,
        coverage_date=date(2026, 9, 2),
        sync_run_id=sync_run_id,
        attestation_id=attestation_id,
    )
    assert gen.clock_started_at == first_clock_start


def test_calculate_tenure_gap_breaks_streak(db_session):
    ws, conn = _setup(db_session)
    gen = create_contract_generation(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1",
        fields=["impressions"],
        attribution_window={}, breakdowns=[],
        time_window={}, local_timezone="UTC",
    )
    # Days 1-3, then gap, then days 5-7
    for seq, d in enumerate([1, 2, 3, 5, 6, 7]):
        sync_run_id, attestation_id = _backing_evidence(db_session, ws, gen, seq)
        record_coverage(
            db_session,
            generation_id=gen.id,
            coverage_date=date(2026, 9, d),
            sync_run_id=sync_run_id,
            attestation_id=attestation_id,
        )
    tenure = calculate_tenure(db_session, gen.id)
    assert tenure == 3  # Longest consecutive streak is 3


def test_calculate_tenure_no_coverage_is_zero(db_session):
    ws, conn = _setup(db_session)
    gen = create_contract_generation(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1",
        fields=["impressions"],
        attribution_window={}, breakdowns=[],
        time_window={}, local_timezone="UTC",
    )
    assert calculate_tenure(db_session, gen.id) == 0


def test_record_coverage_unknown_generation_raises(db_session):
    with pytest.raises(ContractError):
        record_coverage(
            db_session,
            generation_id="ocg_missing",
            coverage_date=date(2026, 9, 1),
            sync_run_id="msync_missing",
            attestation_id="eatt_missing",
        )
