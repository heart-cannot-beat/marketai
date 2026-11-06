# tests/test_meta_connection_lifecycle.py
"""Meta connection lifecycle service tests (spec §8.7).

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

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import OperationRecord, WorkspaceRecord, new_id, utcnow
from app.entitlements.credentials import CredentialVersionRecord
from app.launch.models import LaunchProvisioningRecord
from app.meta import connections
from app.meta.connections import (
    ConnectionLifecycleError,
    create_binding,
    create_connection,
    retire_connection,
    supersede_binding,
)
from app.meta.models import (
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaObservationSyncScheduleRecord,
    ScenarioVersionRecord,
)
from app.platform.tenancy import TenantContext


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-meta-connection-lifecycle-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "meta_connection_lifecycle.db"),
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
            MetaObservationSyncScheduleRecord.__table__,
            LaunchProvisioningRecord.__table__,
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
        id=new_id("ws"), name="T", is_platform_workspace=platform,
    )
    session.add(ws)
    session.flush()
    return ws


def test_create_sandbox_connection(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(
        db_session, context=ctx, ad_account_id="act_123",
    )
    assert conn.state == "active"
    assert conn.meta_mode == "customer_sandbox"


def test_create_sandbox_connection_has_default_scenario(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    scenario = db_session.query(ScenarioVersionRecord).filter_by(
        meta_connection_id=conn.id,
    ).one()
    assert scenario.is_default is True


def test_create_connection_with_scenario_config(db_session):
    """``scenario_config`` (e.g. controlled_mock fault rules, ADR 0009) is
    persisted verbatim on the default ``ScenarioVersionRecord``."""
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="controlled_mock")
    faults = {"faults": {"rules": [
        {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"},
    ]}}
    conn = create_connection(
        db_session, context=ctx, ad_account_id="act_123", scenario_config=faults,
    )
    scenario = db_session.query(ScenarioVersionRecord).filter_by(
        meta_connection_id=conn.id,
    ).one()
    assert scenario.scenario_config == faults


def test_create_connection_without_scenario_config_defaults_empty(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_456")
    scenario = db_session.query(ScenarioVersionRecord).filter_by(
        meta_connection_id=conn.id,
    ).one()
    assert scenario.scenario_config == {}


def test_create_internal_validation_requires_platform_workspace(db_session):
    ws = _workspace(db_session, platform=False)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="internal_validation")
    with pytest.raises(ConnectionLifecycleError, match="platform workspace"):
        create_connection(db_session, context=ctx, ad_account_id="act_123")


def test_create_internal_validation_on_platform_workspace(db_session):
    ws = _workspace(db_session, platform=True)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="internal_validation")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    assert conn.meta_mode == "internal_validation"


def test_retire_connection_no_provisionings(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    retired = retire_connection(db_session, context=ctx, connection_id=conn.id)
    assert retired.state == "retired"
    assert retired.retired_at is not None


def test_retire_connection_rejects_non_active(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    retire_connection(db_session, context=ctx, connection_id=conn.id)
    with pytest.raises(ConnectionLifecycleError):
        retire_connection(db_session, context=ctx, connection_id=conn.id)


def test_create_binding_sandbox_rejects_credential_versions(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    with pytest.raises(ConnectionLifecycleError):
        create_binding(
            db_session, context=ctx, connection_id=conn.id,
            ad_account_id="act_123", access_token_version_id="cred_fake",
        )


def test_create_binding_internal_validation_requires_credentials(db_session):
    ws = _workspace(db_session, platform=True)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="internal_validation")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    with pytest.raises(ConnectionLifecycleError):
        create_binding(
            db_session, context=ctx, connection_id=conn.id, ad_account_id="act_123",
        )


def test_create_binding_internal_validation_with_valid_credentials(db_session):
    ws = _workspace(db_session, platform=True)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="internal_validation")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")

    token_cred = CredentialVersionRecord(
        id=new_id("cred"), workspace_id=ws.id, credential_key="meta_access_token",
        version_no=1, provider="meta", state="active",
        encrypted_secret=b"x", encrypted_data_key=b"y", encryption_key_id="k1",
        nonce=b"n", aad_schema_version=1,
        created_by="platform:credential-admin", created_at=utcnow(),
    )
    secret_cred = CredentialVersionRecord(
        id=new_id("cred"), workspace_id=ws.id, credential_key="meta_app_secret",
        version_no=1, provider="meta", state="active",
        encrypted_secret=b"x", encrypted_data_key=b"y", encryption_key_id="k1",
        nonce=b"n", aad_schema_version=1,
        created_by="platform:credential-admin", created_at=utcnow(),
    )
    db_session.add_all([token_cred, secret_cred])
    db_session.flush()

    binding = create_binding(
        db_session, context=ctx, connection_id=conn.id, ad_account_id="act_123",
        access_token_version_id=token_cred.id, app_secret_version_id=secret_cred.id,
    )
    assert binding.state == "active"
    assert binding.access_token_version_id == token_cred.id


def test_supersede_binding(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    old_binding = create_binding(
        db_session, context=ctx, connection_id=conn.id,
        ad_account_id="act_123",
    )
    new_binding = supersede_binding(
        db_session, context=ctx,
        old_binding_id=old_binding.id, connection_id=conn.id,
        ad_account_id="act_123",
    )
    db_session.refresh(old_binding)
    assert old_binding.state == "superseded"
    assert old_binding.superseded_by_id == new_binding.id
    assert new_binding.state == "active"


def test_supersede_binding_scope_mismatch(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_a")
    binding = create_binding(
        db_session, context=ctx, connection_id=conn.id, ad_account_id="act_a",
    )
    with pytest.raises(ConnectionLifecycleError):
        supersede_binding(
            db_session, context=ctx, old_binding_id=binding.id,
            connection_id="mcon_does_not_belong", ad_account_id="act_a",
        )


def test_supersede_binding_rejects_different_ad_account(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(workspace_id=ws.id, role="owner", initiator_id="usr_a",
                        meta_mode="customer_sandbox")
    conn = create_connection(db_session, context=ctx, ad_account_id="act_123")
    old_binding = create_binding(db_session, context=ctx, connection_id=conn.id, ad_account_id="act_123")
    with pytest.raises(ConnectionLifecycleError, match="ad_account_id mismatch"):
        supersede_binding(db_session, context=ctx, old_binding_id=old_binding.id,
                         connection_id=conn.id, ad_account_id="act_DIFFERENT")


def test_create_connection_member_forbidden(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    with pytest.raises(connections.MetaForbidden):
        connections.create_connection(db_session, context=ctx, ad_account_id="act_test_123")


def test_retire_connection_member_forbidden(db_session):
    ws = _workspace(db_session)
    owner_ctx = TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    conn = connections.create_connection(
        db_session, context=owner_ctx, ad_account_id="act_test_retire",
    )
    member_ctx = TenantContext(
        workspace_id=ws.id, role="member",
        initiator_id="usr_b", meta_mode="customer_sandbox",
    )
    with pytest.raises(connections.MetaForbidden):
        connections.retire_connection(db_session, context=member_ctx, connection_id=conn.id)


def test_create_connection_duplicate_rejected(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    connections.create_connection(db_session, context=ctx, ad_account_id="act_first")
    with pytest.raises(connections.ConnectionAlreadyExists):
        connections.create_connection(db_session, context=ctx, ad_account_id="act_second")


def test_create_connection_sandbox_generates_ad_account_id(db_session):
    ws = _workspace(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="owner",
        initiator_id="usr_a", meta_mode="customer_sandbox",
    )
    conn = connections.create_connection(db_session, context=ctx)
    assert conn.ad_account_id.startswith("act_sandbox_")
    assert len(conn.ad_account_id) == len("act_sandbox_") + 16
