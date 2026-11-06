"""Evidence publication and attestation ranking tests (spec §6.9-§6.11).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_observation.py`` -- since no shared ``db_session`` fixture
exists in ``tests/conftest.py`` for this suite. ``WorkspaceRecord`` has a
``name`` field (not ``display_name``/``slug``), so the workspace fixture
below uses ``name``.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import OperationRecord, WorkspaceRecord, new_id
from app.meta.evidence import (
    EvidenceError,
    create_attestation,
    current_attestation,
    invalidate_attestation,
    publish_evidence,
)
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaConnectionRecord,
    MetaObservationSyncRunRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
)
from app.meta.observation import create_contract_generation
from app.meta.sync_pipeline import create_sync_run


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-evidence-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_evidence_publication.db"),
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
    gen = create_contract_generation(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1", fields=["impressions"],
        attribution_window={}, breakdowns=[], time_window={},
        local_timezone="UTC",
    )
    return ws, conn, gen


def test_publish_creates_publication_record(db_session):
    ws, conn, gen = _setup(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    db_session.add(op)
    db_session.flush()
    run = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_pub_1",
        operation_id=op.id, executor="system:worker",
    )
    pub = publish_evidence(db_session, sync_run_id=run.id, operation_id=op.id)
    assert pub.state == "published"
    assert pub.publication_generation == 1


def test_publish_recovery_creates_new_generation(db_session):
    """A genuine recovery attempt -- a *new* Operation for the same sync
    run -- gets the next generation number, not a mutation of the first."""
    ws, conn, gen = _setup(db_session)
    op1 = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    op2 = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    db_session.add_all([op1, op2])
    db_session.flush()
    run = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_pub_recovery",
        operation_id=op1.id, executor="system:worker",
    )
    first = publish_evidence(db_session, sync_run_id=run.id, operation_id=op1.id)
    second = publish_evidence(db_session, sync_run_id=run.id, operation_id=op2.id)
    assert first.publication_generation == 1
    assert second.publication_generation == 2
    assert first.id != second.id


def test_publish_evidence_idempotent_on_operation_id(db_session):
    """Crash-retry with same operation_id returns existing publication."""
    ws, conn, gen = _setup(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    db_session.add(op)
    db_session.flush()
    run = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_pub_retry",
        operation_id=op.id, executor="system:worker",
    )
    pub1 = publish_evidence(db_session, sync_run_id=run.id, operation_id=op.id)
    pub2 = publish_evidence(db_session, sync_run_id=run.id, operation_id=op.id)
    assert pub1.id == pub2.id
    assert pub1.publication_generation == pub2.publication_generation


def test_publish_unknown_sync_run_raises(db_session):
    with pytest.raises(EvidenceError):
        publish_evidence(db_session, sync_run_id="msync_missing", operation_id="op_missing")


def test_attestation_ranking_by_watermark(db_session):
    """Higher evidence watermark wins regardless of publication time."""
    ws, conn, gen = _setup(db_session)
    op1 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    op2 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    db_session.add_all([op1, op2])
    db_session.flush()
    run1 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_1",
        operation_id=op1.id, executor="system:worker",
    )
    run2 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_2",
        operation_id=op2.id, executor="system:worker",
    )
    pub1 = publish_evidence(db_session, sync_run_id=run1.id, operation_id=op1.id)
    pub2 = publish_evidence(db_session, sync_run_id=run2.id, operation_id=op2.id)
    att1 = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub1.id, evidence_watermark="[2026-09-01,2026-09-03]",
    )
    att2 = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub2.id, evidence_watermark="[2026-09-01,2026-09-07]",
    )
    best = current_attestation(db_session, contract_generation_id=gen.id)
    assert best.id == att2.id  # Higher upper watermark wins


def test_attestation_ranking_ties_break_on_published_at(db_session):
    """Equal watermarks break the tie on published_at DESC."""
    ws, conn, gen = _setup(db_session)
    op1 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    op2 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    db_session.add_all([op1, op2])
    db_session.flush()
    run1 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_tie_1",
        operation_id=op1.id, executor="system:worker",
    )
    run2 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_tie_2",
        operation_id=op2.id, executor="system:worker",
    )
    pub1 = publish_evidence(db_session, sync_run_id=run1.id, operation_id=op1.id)
    att1 = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub1.id, evidence_watermark="[2026-09-01,2026-09-05]",
    )
    pub2 = publish_evidence(db_session, sync_run_id=run2.id, operation_id=op2.id)
    att2 = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub2.id, evidence_watermark="[2026-09-01,2026-09-05]",
    )
    best = current_attestation(db_session, contract_generation_id=gen.id)
    assert best.id == att2.id  # Published later, same watermark


def test_attestation_ranking_id_tiebreak(db_session):
    """Equal watermark AND published_at tie-break deterministically on id."""
    ws, conn, gen = _setup(db_session)
    op1 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    op2 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    db_session.add_all([op1, op2])
    db_session.flush()
    run1 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_idtie_1",
        operation_id=op1.id, executor="system:worker",
    )
    run2 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_idtie_2",
        operation_id=op2.id, executor="system:worker",
    )
    pub1 = publish_evidence(db_session, sync_run_id=run1.id, operation_id=op1.id)
    att1 = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub1.id, evidence_watermark="[2026-09-01,2026-09-05]",
    )
    pub2 = publish_evidence(db_session, sync_run_id=run2.id, operation_id=op2.id)
    att2 = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub2.id, evidence_watermark="[2026-09-01,2026-09-05]",
    )
    # Force identical published_at so only id breaks the tie.
    from app.adapters.persistence.models import utcnow
    fixed_time = utcnow()
    att1.published_at = fixed_time
    att2.published_at = fixed_time
    db_session.flush()
    best = current_attestation(db_session, contract_generation_id=gen.id)
    expected_winner = max(att1.id, att2.id)
    assert best.id == expected_winner


def test_invalidated_attestation_never_wins(db_session):
    """An invalidated attestation with a newer watermark loses to a valid older one."""
    ws, conn, gen = _setup(db_session)
    op1 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    op2 = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                          command_type="meta_sync", state="succeeded")
    db_session.add_all([op1, op2])
    db_session.flush()
    run1 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_older",
        operation_id=op1.id, executor="system:worker",
    )
    run2 = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_newer",
        operation_id=op2.id, executor="system:worker",
    )
    pub1 = publish_evidence(db_session, sync_run_id=run1.id, operation_id=op1.id)
    older = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub1.id, evidence_watermark="[2026-09-01,2026-09-03]",
    )
    pub2 = publish_evidence(db_session, sync_run_id=run2.id, operation_id=op2.id)
    newer = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub2.id, evidence_watermark="[2026-09-01,2026-09-09]",
    )
    invalidate_attestation(db_session, attestation_id=newer.id, reason="data_quality")
    best = current_attestation(db_session, contract_generation_id=gen.id)
    assert best.id == older.id


def test_current_attestation_no_evidence_is_none(db_session):
    ws, conn, gen = _setup(db_session)
    assert current_attestation(db_session, contract_generation_id=gen.id) is None


def test_invalidation_is_append_only(db_session):
    """Invalidated attestation cannot return to valid."""
    ws, conn, gen = _setup(db_session)
    op = OperationRecord(id=new_id("op"), workspace_id=ws.id,
                         command_type="meta_sync", state="succeeded")
    db_session.add(op)
    db_session.flush()
    run = create_sync_run(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_inv",
        operation_id=op.id, executor="system:worker",
    )
    pub = publish_evidence(db_session, sync_run_id=run.id, operation_id=op.id)
    att = create_attestation(
        db_session, contract_generation_id=gen.id,
        publication_id=pub.id, evidence_watermark="[2026-09-01,2026-09-05]",
    )
    assert att.state == "valid"
    invalidate_attestation(db_session, attestation_id=att.id, reason="data_quality")
    db_session.refresh(att)
    assert att.state == "invalidated"
    # Verify no way to revert
    with pytest.raises(Exception, match="already invalidated"):
        invalidate_attestation(db_session, attestation_id=att.id, reason="duplicate")


def test_invalidate_unknown_attestation_raises(db_session):
    with pytest.raises(EvidenceError):
        invalidate_attestation(db_session, attestation_id="eatt_missing", reason="x")
