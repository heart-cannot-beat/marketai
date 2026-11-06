"""Meta call intent, outcome, and reconciliation records (spec §3.5-§3.7, §4.3).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_connection_records.py`` -- since no shared ``db_session``
fixture exists in ``tests/conftest.py`` for this suite. ``WorkspaceRecord``
has a ``name`` field (not ``display_name``/``slug``), so the workspace
fixture below uses ``name``.
"""
from __future__ import annotations

import shutil
import tempfile
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
    MetaCallIntentBindingRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-call-evidence-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_call_evidence.db"),
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
            MetaCallReconciliationRecord.__table__,
            MetaCallIntentBindingRecord.__table__,
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
    ws = WorkspaceRecord(id=new_id("ws"), name="Test", created_at=utcnow())
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
    return ws, conn, binding, scen, op


def test_create_call_intent(db_session):
    ws, conn, binding, scen, op = _setup(db_session)
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
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
    db_session.add(intent)
    db_session.flush()
    assert intent.network_dispatched_at is None


def test_call_intent_write_permitted_false_enforced(db_session):
    """write_permitted = true is rejected by CHECK constraint."""
    ws, conn, binding, scen, op = _setup(db_session)
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={},
        request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="page_fetch",
        scenario_version_id=scen.id,
        write_permitted=True,
        executor="system:worker",
    )
    db_session.add(intent)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_call_outcome(db_session):
    ws, conn, binding, scen, op = _setup(db_session)
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={},
        request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="page_fetch",
        scenario_version_id=scen.id,
        write_permitted=False,
        executor="system:worker",
    )
    db_session.add(intent)
    db_session.flush()
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        intent_id=intent.id,
        operation_id=op.id,
        network_attempted=False,
        executor="system:worker",
    )
    db_session.add(outcome)
    db_session.flush()
    assert outcome.http_status is None


def test_call_outcome_no_fake_status_when_not_attempted(db_session):
    """network_attempted=false with a non-null http_status fabricates evidence."""
    ws, conn, binding, scen, op = _setup(db_session)
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={},
        request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="page_fetch",
        scenario_version_id=scen.id,
        write_permitted=False,
        executor="system:worker",
    )
    db_session.add(intent)
    db_session.flush()
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        intent_id=intent.id,
        operation_id=op.id,
        network_attempted=False,
        http_status=200,
        executor="system:worker",
    )
    db_session.add(outcome)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_call_outcome_network_evidence_check(db_session):
    """network_attempted=true requires http_status or error_category."""
    ws, conn, binding, scen, op = _setup(db_session)
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={},
        request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="page_fetch",
        scenario_version_id=scen.id,
        write_permitted=False,
        executor="system:worker",
    )
    db_session.add(intent)
    db_session.flush()
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        intent_id=intent.id,
        operation_id=op.id,
        network_attempted=True,
        executor="system:worker",
    )
    db_session.add(outcome)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_call_reconciliation(db_session):
    ws, conn, binding, scen, op = _setup(db_session)

    def _make_intent():
        intent = MetaCallIntentRecord(
            id=new_id("mci"),
            workspace_id=ws.id,
            meta_mode="customer_sandbox",
            meta_connection_id=conn.id,
            meta_binding_id=binding.id,
            ad_account_id="act_1",
            operation_id=op.id,
            meta_api_method="GET",
            meta_api_endpoint="/v21.0/act_1/insights",
            request_parameters={},
            request_fingerprint_hash="fp",
            connection_state_at_intent="active",
            binding_state_at_intent="active",
            call_type="page_fetch",
            scenario_version_id=scen.id,
            write_permitted=False,
            executor="system:worker",
        )
        db_session.add(intent)
        db_session.flush()
        return intent

    uncertain_intent = _make_intent()
    inspection_intent = _make_intent()
    inspection_outcome = MetaCallOutcomeRecord(
        id=new_id("mco"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        intent_id=inspection_intent.id,
        operation_id=op.id,
        network_attempted=True,
        http_status=200,
        executor="system:worker",
    )
    db_session.add(inspection_outcome)
    db_session.flush()

    reconciliation = MetaCallReconciliationRecord(
        id=new_id("mcr"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        uncertain_intent_id=uncertain_intent.id,
        inspection_intent_id=inspection_intent.id,
        inspection_outcome_id=inspection_outcome.id,
        resolution="confirmed_succeeded",
    )
    db_session.add(reconciliation)
    db_session.flush()
    assert reconciliation.resolution == "confirmed_succeeded"


def test_call_intent_binding_record(db_session):
    ws, conn, binding, scen, op = _setup(db_session)
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={},
        request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="page_fetch",
        scenario_version_id=scen.id,
        write_permitted=False,
        executor="system:worker",
    )
    db_session.add(intent)
    db_session.flush()
    intent_binding = MetaCallIntentBindingRecord(
        id=new_id("mcib"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        intent_id=intent.id,
        binding_id=binding.id,
    )
    db_session.add(intent_binding)
    db_session.flush()
    assert intent_binding.access_token_version_id is None
