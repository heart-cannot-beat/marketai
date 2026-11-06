"""Observation contract, sync pipeline, and evidence records (spec
§3.8-§3.14, §3.22-§3.25).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_call_evidence.py`` -- since no shared ``db_session``
fixture exists in ``tests/conftest.py`` for this suite. ``WorkspaceRecord``
has a ``name`` field (not ``display_name``/``slug``), so the workspace
fixture below uses ``name``.
"""
from __future__ import annotations

import gzip
import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    OperationRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    MetaStatusHistoryRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
    ScaleVerdictTaintRecord,
    ScenarioVersionRecord,
    VerdictSupersessionRecord,
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
            CredentialVersionRecord.__table__,
            OperationRecord.__table__,
            MetaConnectionRecord.__table__,
            MetaDataSourceBindingRecord.__table__,
            ScenarioVersionRecord.__table__,
            MetaCallIntentRecord.__table__,
            MetaCallOutcomeRecord.__table__,
            ObservationContractGenerationRecord.__table__,
            MetaObservationSyncRunRecord.__table__,
            MetaEvidencePageRecord.__table__,
            MetaPageFetchAttemptRecord.__table__,
            MetaStatusHistoryRecord.__table__,
            EvidencePublicationRecord.__table__,
            EvidenceAttestationRecord.__table__,
            ObservationCoverageLedgerRecord.__table__,
            MetaObservationSyncScheduleRecord.__table__,
            ScaleVerdictTaintRecord.__table__,
            VerdictSupersessionRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _setup_contract(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    session.add(ws)
    session.flush()
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ws.id,
        meta_mode="customer_sandbox", ad_account_id="act_1",
    )
    session.add(conn)
    session.flush()
    gen = ObservationContractGenerationRecord(
        id=new_id("ocg"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        contract_type="daily_insights", generation_number=1,
        ad_account_id="act_1",
        fields=["impressions", "spend"],
        attribution_window={"click": "7d", "view": "1d"},
        breakdowns=[], time_window={"granularity": "daily"},
        contract_hash="ch_123",
        local_timezone="America/Los_Angeles",
    )
    session.add(gen)
    session.flush()
    return ws, conn, gen


def _setup_run(session, ws, gen, state="queued"):
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="queued",
    )
    session.add(op)
    session.flush()
    run = MetaObservationSyncRunRecord(
        id=new_id("msrun"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        occurrence_id=f"occ_{new_id('o')}",
        state=state,
        operation_id=op.id,
        executor="system:worker",
        completed_at=utcnow() if state == "succeeded" else None,
    )
    session.add(run)
    session.flush()
    return op, run


def _setup_intent_outcome(session, ws, conn):
    binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        ad_account_id="act_1",
    )
    session.add(binding)
    session.flush()
    scen = ScenarioVersionRecord(
        id=new_id("scen"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        scenario_config={}, scenario_hash="h",
    )
    session.add(scen)
    session.flush()
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="running",
    )
    session.add(op)
    session.flush()
    intent = MetaCallIntentRecord(
        id=new_id("mci"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={"fields": ["impressions"]},
        request_fingerprint_hash="fp_hash_123",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="page_fetch",
        scenario_version_id=scen.id,
        write_permitted=False,
        executor="system:worker",
    )
    session.add(intent)
    session.flush()
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        intent_id=intent.id,
        operation_id=op.id,
        network_attempted=True,
        http_status=200,
        executor="system:worker",
    )
    session.add(outcome)
    session.flush()
    return intent, outcome


def test_create_observation_contract_generation(db_session):
    ws, conn, gen = _setup_contract(db_session)
    assert gen.state == "active"
    assert gen.clock_started_at is None
    assert gen.local_timezone == "America/Los_Angeles"


def test_create_sync_run(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen)
    assert run.run_generation == 1


def test_sync_run_succeeded_requires_completed_at(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    db_session.add(op)
    db_session.flush()
    run = MetaObservationSyncRunRecord(
        id=new_id("msrun"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        occurrence_id=f"occ_{new_id('o')}",
        state="succeeded",
        operation_id=op.id,
        executor="system:worker",
        # completed_at intentionally omitted
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_evidence_page(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen)
    intent, outcome = _setup_intent_outcome(db_session, ws, conn)
    payload = gzip.compress(b'{"data": []}')
    page = MetaEvidencePageRecord(
        id=new_id("mpage"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        sync_run_id=run.id,
        contract_generation_id=gen.id,
        page_order=1,
        payload_checksum="sha256:abc",
        payload_compressed=payload,
        row_count=0,
        is_terminal=True,
        intent_id=intent.id,
        outcome_id=outcome.id,
    )
    db_session.add(page)
    db_session.flush()
    assert page.is_terminal is True
    assert page.output_cursor is None


def test_evidence_page_terminal_requires_null_output_cursor(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen)
    intent, outcome = _setup_intent_outcome(db_session, ws, conn)
    page = MetaEvidencePageRecord(
        id=new_id("mpage"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        sync_run_id=run.id,
        contract_generation_id=gen.id,
        page_order=1,
        output_cursor="cursor_1",
        payload_checksum="sha256:abc",
        payload_compressed=gzip.compress(b"{}"),
        row_count=0,
        is_terminal=True,
        intent_id=intent.id,
        outcome_id=outcome.id,
    )
    db_session.add(page)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_page_fetch_attempt(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen)
    intent, _outcome = _setup_intent_outcome(db_session, ws, conn)
    attempt = MetaPageFetchAttemptRecord(
        id=new_id("mpfa"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        sync_run_id=run.id,
        intent_id=intent.id,
        page_order=1,
        attempt_number=1,
        state="succeeded",
        completed_at=utcnow(),
    )
    db_session.add(attempt)
    db_session.flush()
    assert attempt.state == "succeeded"


def test_create_status_history(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen)
    intent, outcome = _setup_intent_outcome(db_session, ws, conn)
    page = MetaEvidencePageRecord(
        id=new_id("mpage"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        sync_run_id=run.id,
        contract_generation_id=gen.id,
        page_order=1,
        payload_checksum="sha256:abc",
        payload_compressed=gzip.compress(b"{}"),
        row_count=1,
        is_terminal=True,
        intent_id=intent.id,
        outcome_id=outcome.id,
    )
    db_session.add(page)
    db_session.flush()
    status = MetaStatusHistoryRecord(
        id=new_id("mstat"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        entity_type="campaign",
        entity_id="remote_campaign_1",
        observed_status="PAUSED",
        sync_run_id=run.id,
        evidence_page_id=page.id,
        observed_at=utcnow(),
    )
    db_session.add(status)
    db_session.flush()
    assert status.previous_status is None


def _setup_attestation(session):
    """Build a contract, succeeded sync run, published publication, and
    a valid attestation. Shared by test_create_evidence_attestation and
    test_create_observation_coverage_ledger (the ledger needs a valid
    attestation to back its coverage entry, per spec §3.13).
    """
    ws, conn, gen = _setup_contract(session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="succeeded",
    )
    session.add(op)
    session.flush()
    run = MetaObservationSyncRunRecord(
        id=new_id("msrun"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        occurrence_id=f"occ_{new_id('o')}",
        state="succeeded",
        operation_id=op.id,
        executor="system:worker",
        completed_at=utcnow(),
    )
    session.add(run)
    session.flush()
    pub = EvidencePublicationRecord(
        id=new_id("epub"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        sync_run_id=run.id,
        publication_generation=1,
        state="published",
        operation_id=op.id,
        published_at=utcnow(),
    )
    session.add(pub)
    session.flush()
    att = EvidenceAttestationRecord(
        id=new_id("eatt"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        publication_id=pub.id,
        evidence_watermark="[2026-09-01,2026-09-07]",
        published_at=utcnow(),
    )
    session.add(att)
    session.flush()
    return ws, conn, gen, run, pub, att


def test_create_evidence_attestation(db_session):
    ws, conn, gen, run, pub, att = _setup_attestation(db_session)
    assert att.state == "valid"


def test_evidence_publication_published_requires_timestamp(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen, state="succeeded")
    pub = EvidencePublicationRecord(
        id=new_id("epub"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        sync_run_id=run.id,
        publication_generation=1,
        state="published",
        operation_id=op.id,
        # published_at intentionally omitted
    )
    db_session.add(pub)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_observation_coverage_ledger(db_session):
    ws, conn, gen, run, pub, att = _setup_attestation(db_session)
    ledger = ObservationCoverageLedgerRecord(
        id=new_id("ocl"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        contract_generation_id=gen.id,
        coverage_date=date(2026, 9, 1),
        sync_run_id=run.id,
        attestation_id=att.id,
    )
    db_session.add(ledger)
    db_session.flush()
    assert ledger.coverage_date == date(2026, 9, 1)


def test_create_sync_schedule(db_session):
    ws, conn, gen = _setup_contract(db_session)
    sched = MetaObservationSyncScheduleRecord(
        id=new_id("msched"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        contract_generation_id=gen.id,
        cron_expression="0 3 * * *",
    )
    db_session.add(sched)
    db_session.flush()
    assert sched.state == "active"
    assert sched.next_fire_at is None


def test_create_scale_verdict_taint(db_session):
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen, state="succeeded")
    taint = ScaleVerdictTaintRecord(
        id=new_id("svt"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        verdict_id=new_id("sv"),
        taint_source_type="evidence_invalidation",
        taint_source_id=new_id("eatt"),
        sync_run_id=run.id,
        contract_generation_id=gen.id,
    )
    db_session.add(taint)
    db_session.flush()
    assert taint.state == "active"
    assert taint.resolved_at is None


def test_scale_verdict_taint_lineage_mismatch_rejected(db_session):
    """A taint record's sync_run_id must actually belong to the same
    contract_generation_id it references (spec §3.24 lineage FK)."""
    ws, conn, gen = _setup_contract(db_session)
    op, run = _setup_run(db_session, ws, gen, state="succeeded")

    # A second, distinct contract generation.
    other_gen = ObservationContractGenerationRecord(
        id=new_id("ocg"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        contract_type="delivery_config", generation_number=1,
        ad_account_id="act_1",
        fields=[], attribution_window={}, breakdowns=[],
        time_window={"granularity": "daily"},
        contract_hash="ch_other",
        local_timezone="America/Los_Angeles",
    )
    db_session.add(other_gen)
    db_session.flush()

    taint = ScaleVerdictTaintRecord(
        id=new_id("svt"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        verdict_id=new_id("sv"),
        taint_source_type="evidence_invalidation",
        taint_source_id=new_id("eatt"),
        sync_run_id=run.id,  # belongs to `gen`, not `other_gen`
        contract_generation_id=other_gen.id,
    )
    db_session.add(taint)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_verdict_supersession(db_session):
    ws, conn, gen = _setup_contract(db_session)
    supersession = VerdictSupersessionRecord(
        id=new_id("vsup"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        superseded_verdict_id=new_id("sv"),
        superseding_verdict_id=new_id("sv"),
        reason="newer_evidence",
    )
    db_session.add(supersession)
    db_session.flush()
    assert supersession.reason == "newer_evidence"


def test_verdict_supersession_unique_per_superseded_verdict(db_session):
    ws, conn, gen = _setup_contract(db_session)
    superseded_id = new_id("sv")
    first = VerdictSupersessionRecord(
        id=new_id("vsup"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        superseded_verdict_id=superseded_id,
        superseding_verdict_id=new_id("sv"),
        reason="newer_evidence",
    )
    db_session.add(first)
    db_session.flush()
    second = VerdictSupersessionRecord(
        id=new_id("vsup"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        superseded_verdict_id=superseded_id,
        superseding_verdict_id=new_id("sv"),
        reason="manual_withdrawal",
    )
    db_session.add(second)
    with pytest.raises(IntegrityError):
        db_session.flush()
