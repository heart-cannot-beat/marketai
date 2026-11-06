"""Cross-tenant and cross-mode isolation negative tests.

These tests verify that the combination of composite foreign keys and RLS
policies prevents all forms of cross-boundary data access.
"""
import pytest
import shutil
import tempfile
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    WorkspaceRecord,
    OperationRecord,
    new_id,
)
from app.meta.models import (
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaCallIntentRecord,
    ScenarioVersionRecord,
)
from app.entitlements.credentials import CredentialVersionRecord
from app.platform.tenancy import TenantContext, apply_to_session


@pytest.fixture()
def db_session():
    """SQLite in-memory test fixture with foreign key enforcement."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-isolation-"))
    engine = create_engine(
        f"sqlite:///{tmp_dir / 'isolation.db'}",
        connect_args={"check_same_thread": False},
    )
    # Create all tables needed for these tests
    Base.metadata.create_all(
        bind=engine,
        tables=[
            WorkspaceRecord.__table__,
            CredentialVersionRecord.__table__,
            MetaConnectionRecord.__table__,
            MetaDataSourceBindingRecord.__table__,
            ScenarioVersionRecord.__table__,
            OperationRecord.__table__,
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


# --- SQLite tests (constraint enforcement, no RLS) ---


def test_composite_fk_prevents_cross_workspace_binding(db_session):
    """A binding cannot reference a connection from another workspace."""
    ws_a = WorkspaceRecord(id=new_id("ws"), name="A")
    ws_b = WorkspaceRecord(id=new_id("ws"), name="B")
    db_session.add_all([ws_a, ws_b])
    db_session.flush()

    conn_a = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws_a.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_1",
    )
    db_session.add(conn_a)
    db_session.flush()

    # Try to create a binding in workspace B referencing workspace A's connection
    bad_binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"),
        workspace_id=ws_b.id,  # Different workspace!
        meta_mode="customer_sandbox",
        meta_connection_id=conn_a.id,
        ad_account_id="act_1",
    )
    db_session.add(bad_binding)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_composite_fk_prevents_cross_mode_binding(db_session):
    """A binding cannot reference a connection with a different meta_mode."""
    ws = WorkspaceRecord(id=new_id("ws"), name="A")
    db_session.add(ws)
    db_session.flush()

    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_1",
    )
    db_session.add(conn)
    db_session.flush()

    bad_binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"),
        workspace_id=ws.id,
        meta_mode="controlled_mock",  # Different mode!
        meta_connection_id=conn.id,
        ad_account_id="act_1",
    )
    db_session.add(bad_binding)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_write_permitted_constraint_enforced(db_session):
    """MetaCallIntentRecord rejects write_permitted=true."""
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    db_session.add(ws)
    db_session.flush()

    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_1",
    )
    db_session.add(conn)
    db_session.flush()

    binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        ad_account_id="act_1",
    )
    db_session.add(binding)
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

    op = OperationRecord(
        id=new_id("op"),
        workspace_id=ws.id,
        command_type="meta_sync",
        state="running",
    )
    db_session.add(op)
    db_session.flush()

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
        write_permitted=True,  # This should be rejected!
        executor="system:worker",
    )
    db_session.add(intent)
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- PostgreSQL tests (RLS enforcement) ---


@pytest.mark.postgres
def test_rls_cross_workspace_meta_read_blocked(pg_session):
    """Workspace A's Meta evidence is invisible to Workspace B."""
    ws_a = WorkspaceRecord(id=new_id("ws"), name="A")
    ws_b = WorkspaceRecord(id=new_id("ws"), name="B")
    pg_session.add_all([ws_a, ws_b])
    pg_session.flush()

    ctx_a = TenantContext(
        workspace_id=ws_a.id,
        role="member",
        initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    apply_to_session(pg_session, ctx_a)

    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws_a.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_1",
    )
    pg_session.add(conn)
    pg_session.flush()
    pg_session.commit()

    # Switch to workspace B
    pg_session.begin()
    ctx_b = TenantContext(
        workspace_id=ws_b.id,
        role="member",
        initiator_id="usr_b",
        meta_mode="customer_sandbox",
    )
    apply_to_session(pg_session, ctx_b)

    rows = pg_session.execute(select(MetaConnectionRecord)).scalars().all()
    assert len(rows) == 0


@pytest.mark.postgres
def test_rls_cross_mode_meta_read_blocked(pg_session):
    """customer_sandbox rows are invisible to a controlled_mock session."""
    ws = WorkspaceRecord(id=new_id("ws"), name="A")
    pg_session.add(ws)
    pg_session.flush()

    ctx_sandbox = TenantContext(
        workspace_id=ws.id,
        role="member",
        initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    apply_to_session(pg_session, ctx_sandbox)

    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_1",
    )
    pg_session.add(conn)
    pg_session.flush()
    pg_session.commit()

    # Switch to controlled_mock mode
    pg_session.begin()
    ctx_mock = TenantContext(
        workspace_id=ws.id,
        role="member",
        initiator_id="usr_a",
        meta_mode="controlled_mock",
    )
    apply_to_session(pg_session, ctx_mock)

    rows = pg_session.execute(select(MetaConnectionRecord)).scalars().all()
    assert len(rows) == 0


@pytest.mark.postgres
def test_rls_cross_workspace_meta_write_blocked(pg_session):
    """Workspace B cannot INSERT into Workspace A's Meta tables."""
    ws_a = WorkspaceRecord(id=new_id("ws"), name="A")
    ws_b = WorkspaceRecord(id=new_id("ws"), name="B")
    pg_session.add_all([ws_a, ws_b])
    pg_session.flush()

    # Set context for workspace B
    ctx_b = TenantContext(
        workspace_id=ws_b.id,
        role="member",
        initiator_id="usr_b",
        meta_mode="customer_sandbox",
    )
    apply_to_session(pg_session, ctx_b)

    # Try to insert a connection for workspace A (should fail RLS)
    conn = MetaConnectionRecord(
        id=new_id("mcon"),
        workspace_id=ws_a.id,
        meta_mode="customer_sandbox",
        ad_account_id="act_1",
    )
    pg_session.add(conn)
    with pytest.raises(Exception):  # RLS WITH CHECK failure
        pg_session.flush()
