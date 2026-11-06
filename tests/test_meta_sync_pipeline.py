"""Sync pipeline tests: sync run creation, page checkpoints, completion,
schedule dispatch (spec §6.2-§6.5, §6.13).

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
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.credential_resolution import create_call_intent, resolve_adapter
from app.meta.models import (
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    ObservationContractGenerationRecord,
    ScenarioVersionRecord,
)
from app.meta.observation import create_contract_generation
from app.meta.sync_pipeline import (
    SyncPipelineError,
    complete_sync_run,
    create_sync_run,
    dispatch_schedules,
    record_page,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-sync-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_sync_pipeline.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            WorkspaceRecord.__table__,
            CredentialVersionRecord.__table__,
            OperationRecord.__table__,
            MetaConnectionRecord.__table__,
            MetaDataSourceBindingRecord.__table__,
            ScenarioVersionRecord.__table__,
            ObservationContractGenerationRecord.__table__,
            MetaObservationSyncRunRecord.__table__,
            MetaObservationSyncScheduleRecord.__table__,
            MetaCallIntentRecord.__table__,
            MetaCallOutcomeRecord.__table__,
            MetaEvidencePageRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pipeline_setup(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    session.add(ws)
    session.flush()
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ws.id,
        meta_mode="customer_sandbox", ad_account_id="act_1",
    )
    session.add(conn)
    session.flush()
    binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        ad_account_id="act_1",
    )
    session.add(binding)
    scen = ScenarioVersionRecord(
        id=new_id("scen"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        scenario_config={}, scenario_hash="h", is_default=True,
    )
    session.add(scen)
    session.flush()
    gen = create_contract_generation(
        session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, contract_type="daily_insights",
        ad_account_id="act_1", fields=["impressions"],
        attribution_window={}, breakdowns=[], time_window={},
        local_timezone="UTC",
    )
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="running",
    )
    session.add(op)
    session.flush()
    return ws, conn, binding, scen, gen, op


def test_create_sync_run(db_session):
    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    run = create_sync_run(
        db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        occurrence_id="occ_test_1",
        operation_id=op.id,
        executor="system:worker",
    )
    assert run.state == "queued"
    assert run.run_generation == 1


def test_create_sync_run_idempotent(db_session):
    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    run1 = create_sync_run(
        db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        occurrence_id="occ_same",
        operation_id=op.id,
        executor="system:worker",
    )
    run2 = create_sync_run(
        db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        occurrence_id="occ_same",
        operation_id=op.id,
        executor="system:worker",
    )
    assert run1.id == run2.id


def _make_page_evidence(session, ws, conn, binding, scen, op):
    """Build a MetaCallIntentRecord + outcome pair to back a page fetch."""
    resolved = resolve_adapter(
        session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection_id=conn.id, ad_account_id="act_1",
    )
    intent = create_call_intent(
        session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=resolved.binding,
        operation_id=op.id,
        meta_api_method="GET", meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={}, call_type="page_fetch",
        scenario_version_id=resolved.scenario_version_id,
        executor="system:worker",
    )
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id, meta_mode="customer_sandbox",
        intent_id=intent.id, operation_id=op.id,
        network_attempted=True, http_status=200,
        executor="system:worker",
    )
    session.add(outcome)
    session.flush()
    return intent, outcome


def test_record_page_updates_checkpoint(db_session):
    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    run = create_sync_run(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_page_1",
        operation_id=op.id, executor="system:worker",
    )
    intent, outcome = _make_page_evidence(db_session, ws, conn, binding, scen, op)
    page = record_page(
        db_session,
        sync_run_id=run.id, page_order=1,
        payload_checksum="deadbeef", payload_compressed=b"payload",
        row_count=10, intent_id=intent.id, outcome_id=outcome.id,
        output_cursor="cursor_1",
    )
    assert page.page_order == 1
    assert run.state == "running"
    assert run.last_completed_page_order == 1
    assert run.output_cursor == "cursor_1"


def test_complete_sync_run_terminal_atomicity(db_session):
    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    run = create_sync_run(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_page_2",
        operation_id=op.id, executor="system:worker",
    )
    intent, outcome = _make_page_evidence(db_session, ws, conn, binding, scen, op)
    record_page(
        db_session,
        sync_run_id=run.id, page_order=1,
        payload_checksum="deadbeef", payload_compressed=b"payload",
        row_count=10, intent_id=intent.id, outcome_id=outcome.id,
        is_terminal=True,
    )
    completed = complete_sync_run(
        db_session, sync_run_id=run.id, total_pages=1, total_rows=10,
    )
    assert completed.state == "succeeded"
    assert completed.completed_at is not None


def test_complete_sync_run_idempotent(db_session):
    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    run = create_sync_run(
        db_session,
        workspace_id=ws.id, meta_mode="customer_sandbox",
        contract_generation_id=gen.id, occurrence_id="occ_page_3",
        operation_id=op.id, executor="system:worker",
    )
    intent, outcome = _make_page_evidence(db_session, ws, conn, binding, scen, op)
    record_page(
        db_session,
        sync_run_id=run.id, page_order=1,
        payload_checksum="deadbeef", payload_compressed=b"payload",
        row_count=10, intent_id=intent.id, outcome_id=outcome.id,
        is_terminal=True,
    )
    first = complete_sync_run(db_session, sync_run_id=run.id, total_pages=1, total_rows=10)
    second = complete_sync_run(db_session, sync_run_id=run.id, total_pages=1, total_rows=10)
    assert first.completed_at == second.completed_at


def test_record_page_unknown_run_raises(db_session):
    with pytest.raises(SyncPipelineError):
        record_page(
            db_session,
            sync_run_id="msync_missing", page_order=1,
            payload_checksum="x", payload_compressed=b"x",
            row_count=1, intent_id="mci_missing", outcome_id="mco_missing",
        )


def test_dispatch_schedules_claims_due_active_schedules(db_session):
    from datetime import timedelta

    from app.adapters.persistence.models import utcnow

    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    now = utcnow()
    due = MetaObservationSyncScheduleRecord(
        id=new_id("msched"), workspace_id=ws.id, meta_mode="customer_sandbox",
        meta_connection_id=conn.id, contract_generation_id=gen.id,
        cron_expression="*/15 * * * *", state="active",
        next_fire_at=now - timedelta(minutes=1),
    )
    not_due = MetaObservationSyncScheduleRecord(
        id=new_id("msched"), workspace_id=ws.id, meta_mode="customer_sandbox",
        meta_connection_id=conn.id, contract_generation_id=gen.id,
        cron_expression="*/15 * * * *", state="active",
        next_fire_at=now + timedelta(hours=1),
    )
    paused = MetaObservationSyncScheduleRecord(
        id=new_id("msched"), workspace_id=ws.id, meta_mode="customer_sandbox",
        meta_connection_id=conn.id, contract_generation_id=gen.id,
        cron_expression="*/15 * * * *", state="paused",
        next_fire_at=now - timedelta(minutes=1),
    )
    db_session.add_all([due, not_due, paused])
    db_session.flush()

    claimed = dispatch_schedules(db_session, batch_size=10)
    claimed_ids = {s.id for s in claimed}
    assert claimed_ids == {due.id}


def test_dispatch_schedules_respects_batch_size(db_session):
    from datetime import timedelta

    from app.adapters.persistence.models import utcnow

    ws, conn, binding, scen, gen, op = _pipeline_setup(db_session)
    now = utcnow()
    for i in range(3):
        db_session.add(MetaObservationSyncScheduleRecord(
            id=new_id("msched"), workspace_id=ws.id, meta_mode="customer_sandbox",
            meta_connection_id=conn.id, contract_generation_id=gen.id,
            cron_expression="*/15 * * * *", state="active",
            next_fire_at=now - timedelta(minutes=i + 1),
        ))
    db_session.flush()

    claimed = dispatch_schedules(db_session, batch_size=2)
    assert len(claimed) == 2
