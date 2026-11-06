"""Launch Provisioning Saga (spec §5.3-§5.7).

Flat in ``tests/`` with a local scratch SQLite engine/session -- mirroring
``tests/test_launch_multi_candidate.py`` -- since no shared ``db_session``
fixture exists for this suite. ``WorkspaceRecord`` has a ``name`` field (not
``display_name``/``slug``). The preview record class is
``MetaLaunchPreviewRecord`` (``app.launch.models``), not
``LaunchPreviewRecord`` -- that name is the pre-existing single-candidate
canary record in ``app.adapters.persistence.models``.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    LaunchConfirmationRecord,
    OperationRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.entitlements.credentials import CredentialVersionRecord
from app.launch.models import (
    LaunchCandidateRecord,
    LaunchParityCheckRecord,
    LaunchProvisioningRecord,
    LaunchProvisioningStatusHistoryRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from app.launch.saga import ProvisioningError, ProvisioningUncertain, execute_provisioning
from app.meta.models import (
    FakeMetaObjectRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)
from app.platform.tenancy import TenantContext


def _allow_isolated_saga_test(_session, _context, _preview) -> None:
    """The saga unit suite isolates Meta call ordering from Experiment gates."""


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-launch-saga-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "test.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            WorkspaceRecord.__table__,
            CredentialVersionRecord.__table__,
            OperationRecord.__table__,
            LaunchConfirmationRecord.__table__,
            MetaConnectionRecord.__table__,
            MetaDataSourceBindingRecord.__table__,
            ScenarioVersionRecord.__table__,
            MetaCallIntentRecord.__table__,
            MetaCallOutcomeRecord.__table__,
            FakeMetaObjectRecord.__table__,
            MetaLaunchPreviewRecord.__table__,
            LaunchCandidateRecord.__table__,
            LaunchProvisioningRecord.__table__,
            LaunchProvisioningStatusHistoryRecord.__table__,
            LaunchSagaStepAttemptRecord.__table__,
            LaunchParityCheckRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    # LaunchConfirmationRecord.preview_id has an FK onto the legacy
    # single-candidate launch_previews table, which this suite does not
    # create/populate (see module docstring). Must run before any
    # statement opens an implicit transaction -- SQLite ignores this
    # pragma once a transaction is active.
    session.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _saga_env(session):
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
    session.flush()
    scen = ScenarioVersionRecord(
        id=new_id("scen"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        scenario_config={}, scenario_hash="h", is_default=True,
    )
    session.add(scen)
    session.flush()
    preview = MetaLaunchPreviewRecord(
        id=new_id("lprev"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id, meta_binding_id=binding.id,
        experiment_id="exp_1", experiment_version_id="expv_1",
        candidate_count=1, campaign_config={"objective": "OUTCOME_TRAFFIC"},
        daily_budget_minor=500, currency="USD",
        preview_hash="ph_1", frozen_snapshot={},
        state="confirmed", confirmed_at=utcnow(),
    )
    session.add(preview)
    session.flush()
    cand = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=0, experiment_version_candidate_id="evc_1",
        asset_version_id="av_1", creative_config={"headline": "Test"},
        adset_idempotency_key="ik_adset_0", ad_idempotency_key="ik_ad_0",
    )
    session.add(cand)
    session.flush()
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="running",
    )
    session.add(op)
    session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash="ph_1", token_hash="th_123",
        actor_id="usr_test", expires_at=utcnow(),
    )
    session.add(conf)
    session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        preview_id=preview.id, confirmation_id=conf.id,
        operation_id=op.id, campaign_idempotency_key="cik_1",
        provisioning_state="pending",
    )
    session.add(prov)
    session.flush()
    return ws, conn, prov, preview, cand


def test_execute_provisioning_creates_campaign_and_candidate(db_session):
    ws, conn, prov, preview, cand = _saga_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    execute_provisioning(
        db_session, context=ctx, provisioning_id=prov.id,
        eligibility_validator=_allow_isolated_saga_test,
    )
    db_session.refresh(prov)
    assert prov.provisioning_state == "provisioned"
    assert prov.remote_campaign_id is not None
    assert prov.fencing_token == 1
    # Verify saga steps were recorded
    step_count = db_session.execute(
        select(func.count()).select_from(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.provisioning_id == prov.id,
        )
    ).scalar()
    assert step_count >= 3  # create_campaign + create_adset + create_ad minimum
    # All attempts reached the terminal 'applied' state.
    unapplied = db_session.execute(
        select(func.count()).select_from(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.provisioning_id == prov.id,
            LaunchSagaStepAttemptRecord.state != "applied",
        )
    ).scalar()
    assert unapplied == 0
    # Every call is write-prohibited, even though it's a sandbox mock.
    write_permitted_count = db_session.execute(
        select(func.count()).select_from(MetaCallIntentRecord).where(
            MetaCallIntentRecord.write_permitted == True,  # noqa: E712
        )
    ).scalar()
    assert write_permitted_count == 0
    # Verify parity check
    parity = db_session.execute(
        select(LaunchParityCheckRecord).where(
            LaunchParityCheckRecord.provisioning_id == prov.id,
        )
    ).scalar_one()
    assert parity.passed is True
    assert parity.all_candidates_present is True
    assert parity.budget_parity_verified is True
    assert parity.status_parity_verified is True


def test_fencing_token_prevents_stale_worker(db_session):
    ws, conn, prov, preview, cand = _saga_env(db_session)
    # Simulate a newer worker having incremented the fencing token
    prov.fencing_token = 5
    db_session.flush()
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    with pytest.raises(ProvisioningError, match="stale fencing token"):
        execute_provisioning(
            db_session, context=ctx, provisioning_id=prov.id,
            expected_fencing_token=3,  # stale
            eligibility_validator=_allow_isolated_saga_test,
        )
    db_session.refresh(prov)
    # A rejected CAS never touched provisioning_state or the fencing token.
    assert prov.provisioning_state == "pending"
    assert prov.fencing_token == 5


def test_execute_provisioning_rejects_non_resumable_state(db_session):
    ws, conn, prov, preview, cand = _saga_env(db_session)
    prov.provisioning_state = "provisioned"
    db_session.flush()
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    with pytest.raises(ProvisioningError, match="not resumable"):
        execute_provisioning(
            db_session, context=ctx, provisioning_id=prov.id,
            eligibility_validator=_allow_isolated_saga_test,
        )
    db_session.refresh(prov)
    # A terminal-state rejection must not consume a fencing token: the
    # resumability check runs before the CAS increment.
    assert prov.fencing_token == 0


def test_execute_provisioning_multi_candidate(db_session):
    ws, conn, prov, preview, cand = _saga_env(db_session)
    second = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=1, experiment_version_candidate_id="evc_2",
        asset_version_id="av_2", creative_config={"headline": "Test2"},
        adset_idempotency_key="ik_adset_1", ad_idempotency_key="ik_ad_1",
    )
    db_session.add(second)
    preview.candidate_count = 2
    db_session.flush()

    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    execute_provisioning(
        db_session, context=ctx, provisioning_id=prov.id,
        eligibility_validator=_allow_isolated_saga_test,
    )
    db_session.refresh(prov)
    assert prov.provisioning_state == "provisioned"

    adset_attempts = db_session.execute(
        select(func.count()).select_from(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.provisioning_id == prov.id,
            LaunchSagaStepAttemptRecord.step_type == "create_adset",
        )
    ).scalar()
    assert adset_attempts == 2

    # The campaign is shared -- only one create_campaign attempt exists.
    campaign_attempts = db_session.execute(
        select(func.count()).select_from(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.provisioning_id == prov.id,
            LaunchSagaStepAttemptRecord.step_type == "create_campaign",
        )
    ).scalar()
    assert campaign_attempts == 1


# ---------------------------------------------------------------------------
# Durable parity failure (task-6-brief.md Step 3)
# ---------------------------------------------------------------------------

def _saga_env_controlled_mock(session, *, scenario_config=None):
    """Like _saga_env but with controlled_mock mode for fault injection."""
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    session.add(ws)
    session.flush()
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ws.id,
        meta_mode="controlled_mock", ad_account_id="act_1",
    )
    session.add(conn)
    session.flush()
    binding = MetaDataSourceBindingRecord(
        id=new_id("mbind"), workspace_id=ws.id,
        meta_mode="controlled_mock", meta_connection_id=conn.id,
        ad_account_id="act_1",
    )
    session.add(binding)
    session.flush()
    scen = ScenarioVersionRecord(
        id=new_id("scen"), workspace_id=ws.id,
        meta_mode="controlled_mock", meta_connection_id=conn.id,
        scenario_config=scenario_config or {}, scenario_hash="h", is_default=True,
    )
    session.add(scen)
    session.flush()
    preview = MetaLaunchPreviewRecord(
        id=new_id("lprev"), workspace_id=ws.id,
        meta_mode="controlled_mock",
        meta_connection_id=conn.id, meta_binding_id=binding.id,
        experiment_id="exp_1", experiment_version_id="expv_1",
        candidate_count=1, campaign_config={"objective": "OUTCOME_TRAFFIC"},
        daily_budget_minor=500, currency="USD",
        preview_hash="ph_1", frozen_snapshot={},
        state="confirmed", confirmed_at=utcnow(),
    )
    session.add(preview)
    session.flush()
    cand = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="controlled_mock", preview_id=preview.id,
        candidate_index=0, experiment_version_candidate_id="evc_1",
        asset_version_id="av_1", creative_config={"headline": "Test"},
        adset_idempotency_key="ik_adset_0", ad_idempotency_key="ik_ad_0",
    )
    session.add(cand)
    session.flush()
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="running",
    )
    session.add(op)
    session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash="ph_1", token_hash="th_fault_" + new_id("x"),
        actor_id="usr_test", expires_at=utcnow(),
    )
    session.add(conf)
    session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="controlled_mock", meta_connection_id=conn.id,
        preview_id=preview.id, confirmation_id=conf.id,
        operation_id=op.id, campaign_idempotency_key="cik_1",
        provisioning_state="pending",
    )
    session.add(prov)
    session.flush()
    return ws, conn, prov, preview, cand


class TestDurableParityFailure:
    """Spec scenario 9: parity failure committed before ProvisioningError."""

    def test_failed_parity_persisted_before_exception(self, db_session):
        """A return_state_patch fault on the adset introduces a budget mismatch.

        The saga detects the mismatch in its parity check, transitions to
        ``failed``, durably commits (Task 6's _durable_commit call), and then
        raises ProvisioningError.  The parity record with ``passed=False``
        survives the exception because the commit happened first.
        """
        fault_config = {
            "faults": {
                "rules": [{
                    "object_type": "adset",
                    "match_ordinal": 1,
                    "effect": "return_state_patch",
                    "state_patch": {"daily_budget_minor": 999},
                }],
            },
        }
        ws, conn, prov, preview, cand = _saga_env_controlled_mock(
            db_session, scenario_config=fault_config,
        )
        ctx = TenantContext(
            workspace_id=ws.id, role="system", executor="system:worker",
            meta_mode="controlled_mock",
        )
        with pytest.raises(ProvisioningError, match="parity check"):
            execute_provisioning(
                db_session, context=ctx, provisioning_id=prov.id,
                eligibility_validator=_allow_isolated_saga_test,
            )
        db_session.refresh(prov)
        assert prov.provisioning_state == "failed"

        parity = db_session.execute(
            select(LaunchParityCheckRecord).where(
                LaunchParityCheckRecord.provisioning_id == prov.id,
            )
        ).scalar_one()
        assert parity.passed is False
        assert parity.budget_parity_verified is False
        assert parity.all_candidates_present is True
        assert parity.status_parity_verified is True
        assert parity.config_parity_verified is True


# ---------------------------------------------------------------------------
# Fault ordinals across saga steps (#279)
# ---------------------------------------------------------------------------

def _controlled_mock_two_candidates(session, rules):
    ws, conn, prov, preview, cand = _saga_env_controlled_mock(
        session, scenario_config={"faults": {"rules": rules}},
    )
    second = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="controlled_mock", preview_id=preview.id,
        candidate_index=1, experiment_version_candidate_id="evc_2",
        asset_version_id="av_2", creative_config={"headline": "Test2"},
        adset_idempotency_key="ik_adset_1", ad_idempotency_key="ik_ad_1",
    )
    session.add(second)
    preview.candidate_count = 2
    session.flush()
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="controlled_mock",
    )
    return ctx, conn, prov, [cand, second]


def _fake_object_state(session, conn, object_type, idempotency_key):
    scen = session.execute(
        select(ScenarioVersionRecord).where(
            ScenarioVersionRecord.meta_connection_id == conn.id,
        )
    ).scalar_one()
    adapter = FakeMetaAdapter(
        session=session, workspace_id=conn.workspace_id,
        meta_mode=conn.meta_mode, scenario_version_id=scen.id,
    )
    return adapter.read_object(
        object_type, adapter.deterministic_object_id(object_type, idempotency_key),
    )


class TestFaultOrdinalAcrossSagaSteps:
    """The saga resolves a fresh adapter per step; ``match_ordinal`` still
    counts per scenario version and object type (launch-recovery Scenario 4).
    """

    def test_second_ad_ordinal_leaves_first_candidate_applied(self, db_session):
        ctx, conn, prov, candidates = _controlled_mock_two_candidates(db_session, [
            {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"},
        ])
        with pytest.raises(ProvisioningUncertain, match="create_ad"):
            execute_provisioning(
                db_session, context=ctx, provisioning_id=prov.id,
                eligibility_validator=_allow_isolated_saga_test,
            )
        db_session.refresh(prov)
        assert prov.provisioning_state == "partially_provisioned"

        ad_attempts = {
            attempt.candidate_id: attempt.state
            for attempt in db_session.execute(
                select(LaunchSagaStepAttemptRecord).where(
                    LaunchSagaStepAttemptRecord.provisioning_id == prov.id,
                    LaunchSagaStepAttemptRecord.step_type == "create_ad",
                )
            ).scalars()
        }
        assert ad_attempts == {
            candidates[0].id: "applied",
            candidates[1].id: "intent_committed",
        }
        for cand in candidates:
            assert _fake_object_state(
                db_session, conn, "ad", cand.ad_idempotency_key,
            ) is not None

    @pytest.mark.parametrize("ordinal, hit_index", [(1, 0), (2, 1)])
    def test_ad_ordinal_hits_only_that_candidate(self, db_session, ordinal, hit_index):
        ctx, conn, prov, candidates = _controlled_mock_two_candidates(db_session, [
            {"object_type": "ad", "match_ordinal": ordinal,
             "effect": "return_state_patch", "state_patch": {"fault_marker": "hit"}},
        ])
        execute_provisioning(
            db_session, context=ctx, provisioning_id=prov.id,
            eligibility_validator=_allow_isolated_saga_test,
        )
        db_session.refresh(prov)
        assert prov.provisioning_state == "provisioned"

        markers = [
            _fake_object_state(
                db_session, conn, "ad", cand.ad_idempotency_key,
            ).get("fault_marker")
            for cand in candidates
        ]
        expected = [None, None]
        expected[hit_index] = "hit"
        assert markers == expected

    def test_first_adset_ordinal_patches_only_first_budget(self, db_session):
        """Scenario 9: only the first AdSet's budget diverges."""
        ctx, conn, prov, candidates = _controlled_mock_two_candidates(db_session, [
            {"object_type": "adset", "match_ordinal": 1,
             "effect": "return_state_patch", "state_patch": {"daily_budget_minor": 1}},
        ])
        with pytest.raises(ProvisioningError, match="parity check"):
            execute_provisioning(
                db_session, context=ctx, provisioning_id=prov.id,
                eligibility_validator=_allow_isolated_saga_test,
            )
        budgets = [
            _fake_object_state(
                db_session, conn, "adset", cand.adset_idempotency_key,
            )["daily_budget_minor"]
            for cand in candidates
        ]
        assert budgets == [1, 500]
