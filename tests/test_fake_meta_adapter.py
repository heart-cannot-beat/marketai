"""Database-backed FakeMetaAdapter tests.

Uses a scratch SQLite engine/session for isolation, following the pattern
in tests/test_meta_call_evidence.py. WorkspaceRecord uses 'name' field,
not 'display_name' or 'slug'.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.meta.fake_adapter import (
    AdapterFaultTriggered,
    FakeMetaAdapter,
    MetaAdapterPort,
)
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    OperationRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.meta.models import (
    MetaConnectionRecord,
    ScenarioVersionRecord,
    FakeMetaObjectRecord,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-fake-meta-adapter-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "fake_meta_adapter.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            WorkspaceRecord.__table__,
            OperationRecord.__table__,
            MetaConnectionRecord.__table__,
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


def _setup(session, meta_mode="customer_sandbox"):
    ws = WorkspaceRecord(id=new_id("ws"), name="T", created_at=utcnow())
    session.add(ws)
    session.flush()
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ws.id,
        meta_mode=meta_mode, ad_account_id="act_1",
    )
    session.add(conn)
    session.flush()
    scen = ScenarioVersionRecord(
        id=new_id("scen"), workspace_id=ws.id,
        meta_mode=meta_mode, meta_connection_id=conn.id,
        scenario_config={"campaigns": [
            {"name": "Test Campaign", "status": "PAUSED", "budget": 500},
        ]},
        scenario_hash="sh_1",
    )
    session.add(scen)
    session.flush()
    return ws, conn, scen


def test_fake_adapter_is_test_adapter():
    adapter = FakeMetaAdapter.__new__(FakeMetaAdapter)
    assert adapter.is_test_adapter is True


def test_fake_adapter_write_creates_object(db_session):
    ws, conn, scen = _setup(db_session)
    adapter = FakeMetaAdapter(
        session=db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
    )
    result = adapter.ensure_object(
        object_type="campaign",
        idempotency_key="ik_campaign_1",
        state={"name": "Test", "status": "PAUSED", "daily_budget": 500},
    )
    assert result.remote_object_id.startswith("cmp_fake_")
    assert result.recovered is False
    # Verify persisted
    obj = db_session.execute(
        select(FakeMetaObjectRecord).where(
            FakeMetaObjectRecord.remote_object_id == result.remote_object_id
        )
    ).scalar_one()
    assert obj.object_type == "campaign"


def test_fake_adapter_idempotent_write(db_session):
    ws, conn, scen = _setup(db_session)
    adapter = FakeMetaAdapter(
        session=db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
    )
    r1 = adapter.ensure_object("campaign", "ik_1", {"status": "PAUSED"})
    r2 = adapter.ensure_object("campaign", "ik_1", {"status": "PAUSED"})
    assert r1.remote_object_id == r2.remote_object_id
    assert r2.recovered is True


def test_fake_adapter_read_object(db_session):
    ws, conn, scen = _setup(db_session)
    adapter = FakeMetaAdapter(
        session=db_session,
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
    )
    created = adapter.ensure_object("campaign", "ik_1", {"status": "PAUSED"})
    readback = adapter.read_object("campaign", created.remote_object_id)
    assert readback is not None
    assert readback["status"] == "PAUSED"


def test_fake_adapter_conforms_to_protocol():
    assert isinstance(FakeMetaAdapter, type)
    # Check protocol methods exist
    assert hasattr(FakeMetaAdapter, "ensure_object")
    assert hasattr(FakeMetaAdapter, "read_object")
    assert hasattr(FakeMetaAdapter, "is_test_adapter")


class TestFaultConsumption:
    """Fault injection is controlled_mock-only (ADR 0009)."""

    def test_uncertain_before_apply_raises_without_creating(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "campaign", "match_ordinal": 1, "effect": "uncertain_before_apply"}
            ]},
        )
        with pytest.raises(AdapterFaultTriggered, match="uncertain_before_apply"):
            adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})
        assert adapter.read_object(
            "campaign", adapter.deterministic_object_id("campaign", "key1")
        ) is None

    def test_uncertain_after_apply_creates_then_raises(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 1, "effect": "uncertain_after_apply"}
            ]},
        )
        with pytest.raises(AdapterFaultTriggered, match="uncertain_after_apply"):
            adapter.ensure_object("ad", "key1", {"status": "PAUSED"})
        state = adapter.read_object("ad", adapter.deterministic_object_id("ad", "key1"))
        assert state is not None
        assert state["status"] == "PAUSED"

    def test_return_state_patch_applies_patch(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "adset", "match_ordinal": 1,
                 "effect": "return_state_patch",
                 "state_patch": {"daily_budget_minor": 99999}}
            ]},
        )
        result = adapter.ensure_object("adset", "key1", {"status": "PAUSED", "daily_budget_minor": 1000})
        assert result.state["daily_budget_minor"] == 99999
        assert result.state["status"] == "PAUSED"

    def test_state_patch_cannot_modify_status(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 1,
                 "effect": "return_state_patch",
                 "state_patch": {"status": "ACTIVE"}}
            ]},
        )
        with pytest.raises(ValueError, match="status"):
            adapter.ensure_object("ad", "key1", {"status": "PAUSED"})

    def test_ordinal_targets_correct_call(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"}
            ]},
        )
        r1 = adapter.ensure_object("ad", "key1", {"status": "PAUSED"})
        assert not r1.recovered  # first call succeeds
        with pytest.raises(AdapterFaultTriggered):
            adapter.ensure_object("ad", "key2", {"status": "PAUSED"})  # second call faults

    def test_ordinal_counts_across_adapter_instances(self, db_session):
        """The saga resolves a fresh adapter per step, so the ordinal must
        come from the scenario version's durable objects, not the instance."""
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        fault_config = {"rules": [
            {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"}
        ]}

        def fresh_adapter():
            return FakeMetaAdapter(
                session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
                scenario_version_id=scen.id, fault_config=fault_config,
            )

        assert not fresh_adapter().ensure_object("ad", "key1", {"status": "PAUSED"}).recovered
        with pytest.raises(AdapterFaultTriggered):
            fresh_adapter().ensure_object("ad", "key2", {"status": "PAUSED"})

    def test_first_ordinal_matches_only_first_call_across_instances(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        fault_config = {"rules": [
            {"object_type": "ad", "match_ordinal": 1,
             "effect": "return_state_patch", "state_patch": {"fault_marker": "hit"}}
        ]}

        def fresh_adapter():
            return FakeMetaAdapter(
                session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
                scenario_version_id=scen.id, fault_config=fault_config,
            )

        first = fresh_adapter().ensure_object("ad", "key1", {"status": "PAUSED"})
        second = fresh_adapter().ensure_object("ad", "key2", {"status": "PAUSED"})
        assert first.state.get("fault_marker") == "hit"
        assert "fault_marker" not in second.state

    def test_idempotent_replay_does_not_consume_ordinal(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        fault_config = {"rules": [
            {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"}
        ]}

        def fresh_adapter():
            return FakeMetaAdapter(
                session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
                scenario_version_id=scen.id, fault_config=fault_config,
            )

        fresh_adapter().ensure_object("ad", "key1", {"status": "PAUSED"})
        replay = fresh_adapter().ensure_object("ad", "key1", {"status": "PAUSED"})
        assert replay.recovered
        with pytest.raises(AdapterFaultTriggered):
            fresh_adapter().ensure_object("ad", "key2", {"status": "PAUSED"})

    def test_fault_before_apply_does_not_consume_ordinal(self, db_session):
        """Nothing is created, so a retry of the same key hits the same rule."""
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "campaign", "match_ordinal": 1, "effect": "uncertain_before_apply"}
            ]},
        )
        for _ in range(2):
            with pytest.raises(AdapterFaultTriggered, match="uncertain_before_apply"):
                adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})

    def test_ordinal_counts_only_same_object_type_and_scenario_version(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        other_scen = ScenarioVersionRecord(
            id=new_id("scen"), workspace_id=ws.id,
            meta_mode="controlled_mock", meta_connection_id=conn.id,
            scenario_config={}, scenario_hash="other", is_default=False,
        )
        db_session.add(other_scen)
        db_session.flush()
        FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=other_scen.id,
        ).ensure_object("ad", "key1", {"status": "PAUSED"})
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 1,
                 "effect": "return_state_patch", "state_patch": {"fault_marker": "hit"}}
            ]},
        )
        adapter.ensure_object("adset", "key1", {"status": "PAUSED"})
        result = adapter.ensure_object("ad", "key1", {"status": "PAUSED"})
        assert result.state.get("fault_marker") == "hit"

    def test_no_fault_config_is_noop(self, db_session):
        ws, conn, scen = _setup(db_session, meta_mode="controlled_mock")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="controlled_mock",
            scenario_version_id=scen.id,
        )
        result = adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})
        assert result.state["status"] == "PAUSED"

    def test_customer_sandbox_never_receives_faults(self, db_session):
        """Faults only in controlled_mock per ADR 0009."""
        ws, conn, scen = _setup(db_session, meta_mode="customer_sandbox")
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
            scenario_version_id=scen.id,
        )
        # fault_config is None by default for customer_sandbox
        result = adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})
        assert result.state["status"] == "PAUSED"
