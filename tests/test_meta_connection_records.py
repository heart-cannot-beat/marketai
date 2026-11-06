"""Meta-domain connection, binding, scenario, and fake-object records (spec §3.1, §3.2, §3.4, §3.26).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_foundation_migration.py`` -- since no shared ``db_session``
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

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.models import (
    FakeMetaObjectRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-connection-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_connection.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            WorkspaceRecord.__table__,
            CredentialVersionRecord.__table__,
            MetaConnectionRecord.__table__,
            MetaDataSourceBindingRecord.__table__,
            ScenarioVersionRecord.__table__,
            FakeMetaObjectRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _workspace(session, *, platform=False):
    ws = WorkspaceRecord(
        id=new_id("ws"), name="Test", created_at=utcnow(),
        is_platform_workspace=platform,
    )
    session.add(ws)
    session.flush()
    return ws


def test_create_meta_connection(db_session):
    ws = _workspace(db_session)
    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_123456",
    )
    db_session.add(conn)
    db_session.flush()
    assert conn.state == "active"
    assert conn.retired_at is None


def test_connection_composite_unique_key(db_session):
    ws = _workspace(db_session)
    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_123456",
    )
    db_session.add(conn)
    db_session.flush()
    # The (workspace_id, meta_mode, id) composite key exists
    assert conn.workspace_id == ws.id
    assert conn.meta_mode == "customer_sandbox"


def test_create_binding_with_null_credentials(db_session):
    ws = _workspace(db_session)
    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_123",
    )
    db_session.add(conn)
    db_session.flush()
    binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        ad_account_id="act_123",
        access_token_version_id=None,
        app_secret_version_id=None,
    )
    db_session.add(binding)
    db_session.flush()
    assert binding.state == "active"


def test_create_scenario_version(db_session):
    ws = _workspace(db_session)
    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_123",
    )
    db_session.add(conn)
    db_session.flush()
    scen = ScenarioVersionRecord(
        id=new_id("scen"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        scenario_config={"datasets": []},
        scenario_hash="abc123",
    )
    db_session.add(scen)
    db_session.flush()
    assert scen.is_default is False


def test_create_fake_meta_object(db_session):
    ws = _workspace(db_session)
    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_123",
    )
    db_session.add(conn)
    db_session.flush()
    scen = ScenarioVersionRecord(
        id=new_id("scen"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        scenario_config={},
        scenario_hash="h",
    )
    db_session.add(scen)
    db_session.flush()
    obj = FakeMetaObjectRecord(
        id=new_id("fmo"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
        object_type="campaign",
        remote_object_id="cmp_fake_1",
        state={"status": "PAUSED", "name": "Test Campaign"},
    )
    db_session.add(obj)
    db_session.flush()
    assert obj.object_type == "campaign"
