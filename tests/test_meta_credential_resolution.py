"""Credential resolution and adapter factory (spec §4.1-§4.2).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_call_evidence.py`` -- since no shared ``db_session``
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
from sqlalchemy.orm import Session

from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    OperationRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.credential_resolution import (
    AdapterResolutionError,
    create_call_intent,
    resolve_adapter,
)
from app.meta.models import (
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaCallIntentRecord,
    ScenarioVersionRecord,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-credential-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_credential.db"),
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
        ],
    )
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _sandbox_setup(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="T", created_at=utcnow())
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
        scenario_config={}, scenario_hash="h", is_default=True,
    )
    session.add(scen)
    session.flush()
    return ws, conn, binding, scen


def test_resolve_sandbox_adapter(db_session):
    ws, conn, binding, scen = _sandbox_setup(db_session)
    result = resolve_adapter(
        db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        connection_id=conn.id,
        ad_account_id="act_1",
    )
    assert isinstance(result.adapter, FakeMetaAdapter)
    assert result.binding.id == binding.id
    assert result.access_token_version_id is None
    assert result.app_secret_version_id is None


def test_resolve_adapter_fails_for_retired_connection(db_session):
    ws, conn, binding, scen = _sandbox_setup(db_session)
    conn.state = "retired"
    conn.retired_at = utcnow()
    db_session.flush()
    with pytest.raises(AdapterResolutionError, match="not active"):
        resolve_adapter(
            db_session,
            workspace_id=ws.id,
            meta_mode="customer_sandbox",
            connection_id=conn.id,
            ad_account_id="act_1",
        )


def test_create_call_intent_with_for_share(db_session):
    ws, conn, binding, scen = _sandbox_setup(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="meta_sync", state="running",
    )
    db_session.add(op)
    db_session.flush()
    intent = create_call_intent(
        db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        connection=conn,
        binding=binding,
        operation_id=op.id,
        meta_api_method="GET",
        meta_api_endpoint="/v21.0/act_1/insights",
        request_parameters={"fields": ["impressions"]},
        call_type="page_fetch",
        scenario_version_id=scen.id,
        executor="system:worker",
    )
    assert intent.id.startswith("mci_")
    assert intent.write_permitted is False
    assert intent.request_fingerprint_hash  # non-empty
    assert intent.connection_state_at_intent == "active"
    assert intent.binding_state_at_intent == "active"
