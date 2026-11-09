"""Operation recovery and reconciliation (spec §8.3-§8.4).

Flat in ``tests/`` with a local scratch SQLite engine/session -- mirroring
``tests/test_launch_saga.py`` -- since no shared ``db_session`` fixture
exists for this suite. ``WorkspaceRecord`` has a ``name`` field (not
``display_name``/``slug``): the task-15 brief's literal test bodies are
adjusted for that here.
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
    OperationStateEventRecord,
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
    LaunchReconciliationRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from app.launch.recovery import (
    RecoveryError,
    create_recovery_operation,
    detect_uncertain_intents,
    execute_recovery,
)
from app.meta.credential_resolution import create_call_intent
from app.meta.models import (
    FakeMetaObjectRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)
from app.platform.tenancy import TenantContext
from tests.support.usage_tables import operation_usage_tables


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-launch-recovery-"))
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
            OperationStateEventRecord.__table__,
            LaunchConfirmationRecord.__table__,
            MetaConnectionRecord.__table__,
            MetaDataSourceBindingRecord.__table__,
            ScenarioVersionRecord.__table__,
            MetaCallIntentRecord.__table__,
            MetaCallOutcomeRecord.__table__,
            MetaCallReconciliationRecord.__table__,
            FakeMetaObjectRecord.__table__,
            MetaLaunchPreviewRecord.__table__,
            LaunchCandidateRecord.__table__,
            LaunchProvisioningRecord.__table__,
            LaunchProvisioningStatusHistoryRecord.__table__,
            LaunchSagaStepAttemptRecord.__table__,
            LaunchParityCheckRecord.__table__,
            LaunchReconciliationRecord.__table__,
            # A terminal Operation records its Usage (#8 S2).
            *operation_usage_tables(),
        ],
    )
    session = Session(bind=engine)
    # LaunchConfirmationRecord.preview_id has an FK onto the legacy
    # single-candidate launch_previews table, which this suite does not
    # create/populate (see module docstring / test_launch_saga.py). Must run
    # before any statement opens an implicit transaction -- SQLite ignores
    # this pragma once a transaction is active.
    session.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _connection_env(session):
    """Workspace + active Meta connection/binding/default scenario."""
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
    return ws, conn, binding, scen


# ---------------------------------------------------------------------------
# detect_uncertain_intents (task-15-brief.md Step 1, ws fixed to use `name`)
# ---------------------------------------------------------------------------

def test_detect_uncertain_intent(db_session):
    """Intent with network_dispatched_at set and no outcome is uncertain."""
    ws, conn, binding, scen = _connection_env(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="uncertain",
    )
    db_session.add(op)
    db_session.flush()
    intent = MetaCallIntentRecord(
        id=new_id("mci"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        meta_binding_id=binding.id, ad_account_id="act_1",
        operation_id=op.id, meta_api_method="POST",
        meta_api_endpoint="/v21.0/act_1/campaigns",
        request_parameters={}, request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="saga_step", scenario_version_id=scen.id,
        write_permitted=False, executor="system:worker",
        network_dispatched_at=utcnow(),
    )
    db_session.add(intent)
    db_session.flush()
    uncertain = detect_uncertain_intents(db_session, operation_id=op.id)
    assert len(uncertain) == 1
    assert uncertain[0].id == intent.id


def test_detect_no_uncertainty_when_outcome_exists(db_session):
    """Intent with both dispatch marker and outcome is not uncertain."""
    ws, conn, binding, scen = _connection_env(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="succeeded",
    )
    db_session.add(op)
    db_session.flush()
    intent = MetaCallIntentRecord(
        id=new_id("mci"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        meta_binding_id=binding.id, ad_account_id="act_1",
        operation_id=op.id, meta_api_method="POST",
        meta_api_endpoint="/v21.0/act_1/campaigns",
        request_parameters={}, request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="saga_step", scenario_version_id=scen.id,
        write_permitted=False, executor="system:worker",
        network_dispatched_at=utcnow(),
    )
    db_session.add(intent)
    db_session.flush()
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox", intent_id=intent.id,
        operation_id=op.id, network_attempted=False,
        executor="system:worker",
    )
    db_session.add(outcome)
    db_session.flush()
    uncertain = detect_uncertain_intents(db_session, operation_id=op.id)
    assert len(uncertain) == 0


# ---------------------------------------------------------------------------
# create_recovery_operation (task-15-brief.md Step 1, ws fixed to use `name`)
# ---------------------------------------------------------------------------

def test_create_recovery_operation_serialized(db_session):
    """Only one open reconciliation per original operation (spec §8.4)."""
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    db_session.add(ws)
    db_session.flush()
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="uncertain",
    )
    db_session.add(op)
    db_session.flush()
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    recon1 = create_recovery_operation(db_session, context=ctx,
                                       original_operation_id=op.id)
    assert recon1.id is not None
    assert recon1.original_operation_id == op.id
    assert recon1.state == "open"
    with pytest.raises(RecoveryError, match="already open"):
        create_recovery_operation(db_session, context=ctx,
                                  original_operation_id=op.id)


def test_create_recovery_operation_rejects_non_uncertain(db_session):
    """Recovery only applies to an Operation actually in 'uncertain'."""
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    db_session.add(ws)
    db_session.flush()
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="running",
    )
    db_session.add(op)
    db_session.flush()
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    with pytest.raises(RecoveryError, match="not 'uncertain'"):
        create_recovery_operation(db_session, context=ctx,
                                  original_operation_id=op.id)


# ---------------------------------------------------------------------------
# execute_recovery: reads back adapter state, never re-executes writes
# ---------------------------------------------------------------------------

def _uncertain_campaign_env(session):
    """A create_campaign saga step dispatched to Meta with no recorded outcome.

    Mirrors the first half of ``app.launch.saga._execute_step`` (intent
    committed, dispatch marker set) then stops -- exactly the gap a crash
    between "request sent" and "outcome recorded" would leave, which is what
    makes the resulting intent 'uncertain'.
    """
    ws, conn, binding, scen = _connection_env(session)
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
        command_type="launch_provisioning", state="uncertain",
    )
    session.add(op)
    session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash="ph_1", token_hash=new_id("th"),
        actor_id="usr_test", expires_at=utcnow(),
    )
    session.add(conf)
    session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        preview_id=preview.id, confirmation_id=conf.id,
        operation_id=op.id, campaign_idempotency_key="cik_1",
        provisioning_state="provisioning",
    )
    session.add(prov)
    session.flush()

    intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/campaigns",
        request_parameters={"daily_budget_minor": 500, "status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    intent.network_dispatched_at = utcnow()
    session.flush()
    attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id, meta_mode="customer_sandbox",
        provisioning_id=prov.id, intent_id=intent.id, candidate_id=None,
        step_type="create_campaign", attempt_number=1,
        fencing_token=prov.fencing_token, state="intent_committed",
        executor="system:worker",
    )
    session.add(attempt)
    session.flush()

    return ws, conn, binding, scen, prov, op, intent


def _fake_object_count(session) -> int:
    return session.execute(
        select(func.count()).select_from(FakeMetaObjectRecord)
    ).scalar()


def test_execute_recovery_confirms_succeeded_write(db_session):
    """The write actually reached the adapter before the crash: recovery
    finds it by reading back the deterministic id, and never calls
    ensure_object -- no new FakeMetaObjectRecord row is created."""
    ws, conn, binding, scen, prov, op, intent = _uncertain_campaign_env(db_session)

    # Simulate that ensure_object() *did* run before the crash: the fake
    # backend already holds the object the saga step would have created.
    adapter = FakeMetaAdapter(
        session=db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
    )
    remote_id = adapter.deterministic_object_id("campaign", prov.campaign_idempotency_key)
    db_session.add(FakeMetaObjectRecord(
        id=new_id("fmo"), workspace_id=ws.id, meta_mode="customer_sandbox",
        scenario_version_id=scen.id, object_type="campaign",
        remote_object_id=remote_id, state={"status": "PAUSED", "daily_budget_minor": 500},
    ))
    db_session.flush()
    count_before = _fake_object_count(db_session)

    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    recon = create_recovery_operation(db_session, context=ctx,
                                      original_operation_id=op.id)
    result = execute_recovery(db_session, ctx, recon.id)

    count_after = _fake_object_count(db_session)
    assert count_after == count_before  # never re-executed the write

    assert result.state == "resolved"
    # The campaign write succeeded, but the candidate AdSet/Ad manifest and
    # set-level parity check were never completed. Recovery must classify the
    # whole Launch, not promote one resolved intent to global success.
    assert result.resolution == "partially_succeeded"
    db_session.refresh(op)
    assert op.state == "partially_succeeded"
    db_session.refresh(prov)
    assert prov.provisioning_state == "partially_provisioned"

    mcr = db_session.execute(
        select(MetaCallReconciliationRecord).where(
            MetaCallReconciliationRecord.uncertain_intent_id == intent.id,
        )
    ).scalar_one()
    assert mcr.resolution == "confirmed_succeeded"
    # detect_uncertain_intents is a raw dispatch-without-outcome check on the
    # *original* intent (task-15-brief.md Step 3) -- it is not retroactively
    # cleared by recovery. The resolution record is MetaCallReconciliationRecord
    # (asserted above) plus the Operation's own state (asserted below); the
    # original intent legitimately still has no MetaCallOutcomeRecord of its own.
    assert intent.id in {i.id for i in detect_uncertain_intents(db_session, operation_id=op.id)}


def test_execute_recovery_confirms_failed_write(db_session):
    """The write never reached the adapter before the crash: recovery finds
    nothing at the deterministic id and resolves the operation as failed --
    still without ever creating the object itself."""
    ws, conn, binding, scen, prov, op, intent = _uncertain_campaign_env(db_session)
    count_before = _fake_object_count(db_session)
    assert count_before == 0

    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    recon = create_recovery_operation(db_session, context=ctx,
                                      original_operation_id=op.id)
    result = execute_recovery(db_session, ctx, recon.id)

    assert _fake_object_count(db_session) == 0  # never re-executed the write
    assert result.state == "resolved"
    assert result.resolution == "failed"
    db_session.refresh(op)
    assert op.state == "failed"

    mcr = db_session.execute(
        select(MetaCallReconciliationRecord).where(
            MetaCallReconciliationRecord.uncertain_intent_id == intent.id,
        )
    ).scalar_one()
    assert mcr.resolution == "confirmed_failed"


def test_execute_recovery_without_open_intents_never_guesses_global_success(db_session):
    """A lease may expire after one recorded write but before later steps."""
    ws, _conn, _binding, _scen, prov, op, intent = _uncertain_campaign_env(db_session)
    attempt = db_session.scalar(select(LaunchSagaStepAttemptRecord).where(
        LaunchSagaStepAttemptRecord.intent_id == intent.id,
    ))
    attempt.state = "applied"
    attempt.remote_object_id = "campaign_partial"
    attempt.completed_at = utcnow()
    db_session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox", intent_id=intent.id,
        operation_id=op.id, network_attempted=True,
        http_status=200, response_summary={"remote_object_id": "campaign_partial"},
        executor="system:worker",
    ))
    db_session.flush()
    assert detect_uncertain_intents(db_session, op.id) == []

    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    recon = create_recovery_operation(
        db_session, context=ctx, original_operation_id=op.id,
    )
    result = execute_recovery(db_session, ctx, recon.id)

    assert result.resolution == "partially_succeeded"
    db_session.refresh(op)
    assert op.state == "partially_succeeded"
    db_session.refresh(prov)
    assert prov.provisioning_state == "partially_provisioned"


def test_execute_recovery_leaves_reconciliation_open_when_inconclusive(db_session):
    """An uncertain intent with no matching saga step attempt cannot be
    safely resolved from adapter state -- the reconciliation stays open for
    manual review rather than guessing."""
    ws, conn, binding, scen = _connection_env(db_session)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="uncertain",
    )
    db_session.add(op)
    db_session.flush()
    intent = create_call_intent(
        db_session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/campaigns",
        request_parameters={}, call_type="saga_step",
        scenario_version_id=scen.id, executor="system:worker",
    )
    intent.network_dispatched_at = utcnow()
    db_session.flush()

    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    recon = create_recovery_operation(db_session, context=ctx,
                                      original_operation_id=op.id)
    result = execute_recovery(db_session, ctx, recon.id)

    assert result.state == "open"
    assert result.resolution is None
    db_session.refresh(op)
    assert op.state == "uncertain"  # left untouched, not guessed at


def test_execute_recovery_rejects_already_resolved(db_session):
    ws, conn, binding, scen, prov, op, intent = _uncertain_campaign_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="system", executor="system:worker",
        meta_mode="customer_sandbox",
    )
    recon = create_recovery_operation(db_session, context=ctx,
                                      original_operation_id=op.id)
    execute_recovery(db_session, ctx, recon.id)
    with pytest.raises(RecoveryError, match="already"):
        execute_recovery(db_session, ctx, recon.id)


# ---------------------------------------------------------------------------
# Recovery parity reconstruction (task-6-brief.md Step 4)
# ---------------------------------------------------------------------------

def _full_manifest_uncertain_ad_env(session, *, ad_object_exists=True):
    """A complete write manifest (campaign + adset + ad) where the final ad
    step is uncertain: the intent was dispatched but no outcome was recorded.

    Campaign and adset are fully applied with outcomes. The ad fake object
    may or may not exist depending on ``ad_object_exists`` -- this controls
    whether recovery classifies the manifest as complete or incomplete.
    """
    ws, conn, binding, scen = _connection_env(session)
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
        command_type="launch_provisioning", state="uncertain",
    )
    session.add(op)
    session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash="ph_1", token_hash=new_id("th"),
        actor_id="usr_test", expires_at=utcnow(),
    )
    session.add(conf)
    session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        preview_id=preview.id, confirmation_id=conf.id,
        operation_id=op.id, campaign_idempotency_key="cik_1",
        provisioning_state="provisioning",
    )
    session.add(prov)
    session.flush()

    adapter = FakeMetaAdapter(
        session=session, workspace_id=ws.id,
        meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
    )

    # -- Campaign: fully applied with outcome and fake object --
    campaign_remote_id = adapter.deterministic_object_id(
        "campaign", prov.campaign_idempotency_key
    )
    session.add(FakeMetaObjectRecord(
        id=new_id("fmo"), workspace_id=ws.id,
        meta_mode="customer_sandbox", scenario_version_id=scen.id,
        object_type="campaign", remote_object_id=campaign_remote_id,
        state={"status": "PAUSED", "daily_budget_minor": 500},
    ))
    campaign_intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/campaigns",
        request_parameters={"daily_budget_minor": 500, "status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    campaign_intent.network_dispatched_at = utcnow()
    session.flush()
    session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox", intent_id=campaign_intent.id,
        operation_id=op.id, network_attempted=True, http_status=200,
        response_summary={"remote_object_id": campaign_remote_id},
        executor="system:worker",
    ))
    campaign_attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=campaign_intent.id, candidate_id=None,
        step_type="create_campaign", attempt_number=1,
        fencing_token=prov.fencing_token, state="applied",
        remote_object_id=campaign_remote_id,
        executor="system:worker", completed_at=utcnow(),
    )
    session.add(campaign_attempt)
    session.flush()

    # -- Adset: fully applied with outcome and fake object --
    adset_remote_id = adapter.deterministic_object_id(
        "adset", cand.adset_idempotency_key
    )
    session.add(FakeMetaObjectRecord(
        id=new_id("fmo"), workspace_id=ws.id,
        meta_mode="customer_sandbox", scenario_version_id=scen.id,
        object_type="adset", remote_object_id=adset_remote_id,
        state={"status": "PAUSED", "daily_budget_minor": 500},
    ))
    adset_intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/adsets",
        request_parameters={"daily_budget_minor": 500, "status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    adset_intent.network_dispatched_at = utcnow()
    session.flush()
    session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox", intent_id=adset_intent.id,
        operation_id=op.id, network_attempted=True, http_status=200,
        response_summary={"remote_object_id": adset_remote_id},
        executor="system:worker",
    ))
    adset_attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=adset_intent.id, candidate_id=cand.id,
        step_type="create_adset", attempt_number=1,
        fencing_token=prov.fencing_token, state="applied",
        remote_object_id=adset_remote_id,
        executor="system:worker", completed_at=utcnow(),
    )
    session.add(adset_attempt)
    session.flush()

    # -- Ad: dispatched but no outcome (uncertain); object may or may not exist --
    ad_remote_id = adapter.deterministic_object_id(
        "ad", cand.ad_idempotency_key
    )
    if ad_object_exists:
        session.add(FakeMetaObjectRecord(
            id=new_id("fmo"), workspace_id=ws.id,
            meta_mode="customer_sandbox", scenario_version_id=scen.id,
            object_type="ad", remote_object_id=ad_remote_id,
            state={"status": "PAUSED"},
        ))
    ad_intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/ads",
        request_parameters={"status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    ad_intent.network_dispatched_at = utcnow()
    session.flush()
    ad_attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=ad_intent.id, candidate_id=cand.id,
        step_type="create_ad", attempt_number=1,
        fencing_token=prov.fencing_token, state="intent_committed",
        executor="system:worker",
    )
    session.add(ad_attempt)
    session.flush()

    return ws, conn, binding, scen, prov, op, cand, ad_intent


def _full_manifest_uncertain_campaign_env(session, *, campaign_object_exists=True):
    """A complete write manifest (campaign + adset + ad) where the *first*
    (campaign) step is uncertain: the intent was dispatched but no outcome
    was recorded, so ``attempt.state`` stays ``"intent_committed"`` rather
    than transitioning to ``"applied"``.

    AdSet and Ad are fully applied with outcomes. The campaign fake object
    may or may not exist depending on ``campaign_object_exists`` -- this
    controls whether recovery classifies the manifest as complete or
    incomplete. Mirrors ``_full_manifest_uncertain_ad_env`` with the
    uncertain step moved to the front of the manifest, which is the
    regression scenario for the ``_derive_manifest_outcome`` ``next()``
    fix: a filter requiring ``state == "applied"`` on the campaign attempt
    would raise ``StopIteration`` once reconciliation -- not the saga --
    is what confirms the campaign write succeeded.
    """
    ws, conn, binding, scen = _connection_env(session)
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
        command_type="launch_provisioning", state="uncertain",
    )
    session.add(op)
    session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash="ph_1", token_hash=new_id("th"),
        actor_id="usr_test", expires_at=utcnow(),
    )
    session.add(conf)
    session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="customer_sandbox", meta_connection_id=conn.id,
        preview_id=preview.id, confirmation_id=conf.id,
        operation_id=op.id, campaign_idempotency_key="cik_1",
        provisioning_state="provisioning",
    )
    session.add(prov)
    session.flush()

    adapter = FakeMetaAdapter(
        session=session, workspace_id=ws.id,
        meta_mode="customer_sandbox",
        scenario_version_id=scen.id,
    )

    # -- Campaign: dispatched but no outcome (uncertain); object may or
    # may not exist. state stays "intent_committed" -- never "applied" --
    # exactly the case the fragile next() filter mishandled. --
    campaign_remote_id = adapter.deterministic_object_id(
        "campaign", prov.campaign_idempotency_key
    )
    if campaign_object_exists:
        session.add(FakeMetaObjectRecord(
            id=new_id("fmo"), workspace_id=ws.id,
            meta_mode="customer_sandbox", scenario_version_id=scen.id,
            object_type="campaign", remote_object_id=campaign_remote_id,
            state={"status": "PAUSED", "daily_budget_minor": 500},
        ))
    campaign_intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/campaigns",
        request_parameters={"daily_budget_minor": 500, "status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    campaign_intent.network_dispatched_at = utcnow()
    session.flush()
    campaign_attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=campaign_intent.id, candidate_id=None,
        step_type="create_campaign", attempt_number=1,
        fencing_token=prov.fencing_token, state="intent_committed",
        executor="system:worker",
    )
    session.add(campaign_attempt)
    session.flush()

    # -- Adset: fully applied with outcome and fake object --
    adset_remote_id = adapter.deterministic_object_id(
        "adset", cand.adset_idempotency_key
    )
    session.add(FakeMetaObjectRecord(
        id=new_id("fmo"), workspace_id=ws.id,
        meta_mode="customer_sandbox", scenario_version_id=scen.id,
        object_type="adset", remote_object_id=adset_remote_id,
        state={"status": "PAUSED", "daily_budget_minor": 500},
    ))
    adset_intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/adsets",
        request_parameters={"daily_budget_minor": 500, "status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    adset_intent.network_dispatched_at = utcnow()
    session.flush()
    session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox", intent_id=adset_intent.id,
        operation_id=op.id, network_attempted=True, http_status=200,
        response_summary={"remote_object_id": adset_remote_id},
        executor="system:worker",
    ))
    adset_attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=adset_intent.id, candidate_id=cand.id,
        step_type="create_adset", attempt_number=1,
        fencing_token=prov.fencing_token, state="applied",
        remote_object_id=adset_remote_id,
        executor="system:worker", completed_at=utcnow(),
    )
    session.add(adset_attempt)
    session.flush()

    # -- Ad: fully applied with outcome and fake object --
    ad_remote_id = adapter.deterministic_object_id(
        "ad", cand.ad_idempotency_key
    )
    session.add(FakeMetaObjectRecord(
        id=new_id("fmo"), workspace_id=ws.id,
        meta_mode="customer_sandbox", scenario_version_id=scen.id,
        object_type="ad", remote_object_id=ad_remote_id,
        state={"status": "PAUSED"},
    ))
    ad_intent = create_call_intent(
        session, workspace_id=ws.id, meta_mode="customer_sandbox",
        connection=conn, binding=binding, operation_id=op.id,
        meta_api_method="POST", meta_api_endpoint="/ads",
        request_parameters={"status": "PAUSED"},
        call_type="saga_step", scenario_version_id=scen.id,
        executor="system:worker",
    )
    ad_intent.network_dispatched_at = utcnow()
    session.flush()
    session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=ws.id,
        meta_mode="customer_sandbox", intent_id=ad_intent.id,
        operation_id=op.id, network_attempted=True, http_status=200,
        response_summary={"remote_object_id": ad_remote_id},
        executor="system:worker",
    ))
    ad_attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=ad_intent.id, candidate_id=cand.id,
        step_type="create_ad", attempt_number=1,
        fencing_token=prov.fencing_token, state="applied",
        remote_object_id=ad_remote_id,
        executor="system:worker", completed_at=utcnow(),
    )
    session.add(ad_attempt)
    session.flush()

    return ws, conn, binding, scen, prov, op, cand, campaign_intent


class TestRecoveryParityReconstruction:
    """Parity reconstruction after reconciliation (task-6-brief.md Step 4)."""

    def test_complete_reconciled_manifest_gets_parity(self, db_session):
        """Spec scenario 4: all writes confirmed by reconciliation read-back
        -> recovery reconstructs a post_provisioning parity record with
        ``detail.source == 'reconciliation'`` and resolves as succeeded."""
        ws, conn, binding, scen, prov, op, cand, ad_intent = (
            _full_manifest_uncertain_ad_env(db_session, ad_object_exists=True)
        )
        ctx = TenantContext(
            workspace_id=ws.id, role="system", executor="system:worker",
            meta_mode="customer_sandbox",
        )

        recon = create_recovery_operation(
            db_session, context=ctx, original_operation_id=op.id,
        )
        result = execute_recovery(db_session, ctx, recon.id)

        assert result.state == "resolved"
        assert result.resolution == "succeeded"
        db_session.refresh(op)
        assert op.state == "succeeded"
        db_session.refresh(prov)
        assert prov.provisioning_state == "provisioned"

        # Verify the reconstructed parity record
        parity = db_session.execute(
            select(LaunchParityCheckRecord).where(
                LaunchParityCheckRecord.provisioning_id == prov.id,
                LaunchParityCheckRecord.check_type == "post_provisioning",
            )
        ).scalar_one()
        assert parity.passed is True
        assert parity.all_candidates_present is True
        assert parity.budget_parity_verified is True
        assert parity.config_parity_verified is True
        assert parity.status_parity_verified is True
        assert parity.detail["source"] == "reconciliation"

        # The uncertain intent was reconciled as confirmed_succeeded
        mcr = db_session.execute(
            select(MetaCallReconciliationRecord).where(
                MetaCallReconciliationRecord.uncertain_intent_id == ad_intent.id,
            )
        ).scalar_one()
        assert mcr.resolution == "confirmed_succeeded"

    def test_incomplete_manifest_is_partially_provisioned(self, db_session):
        """Missing writes -> partially_provisioned (terminal).

        When an uncertain write is confirmed to have failed (the object
        does not exist in the adapter), the manifest is incomplete and
        recovery classifies the outcome as partially_succeeded without
        attempting parity reconstruction."""
        ws, conn, binding, scen, prov, op, cand, ad_intent = (
            _full_manifest_uncertain_ad_env(db_session, ad_object_exists=False)
        )
        ctx = TenantContext(
            workspace_id=ws.id, role="system", executor="system:worker",
            meta_mode="customer_sandbox",
        )

        recon = create_recovery_operation(
            db_session, context=ctx, original_operation_id=op.id,
        )
        result = execute_recovery(db_session, ctx, recon.id)

        assert result.state == "resolved"
        assert result.resolution == "partially_succeeded"
        db_session.refresh(op)
        assert op.state == "partially_succeeded"
        db_session.refresh(prov)
        assert prov.provisioning_state == "partially_provisioned"

        # No parity record should exist -- reconstruction is not attempted
        parity_count = db_session.execute(
            select(func.count()).select_from(LaunchParityCheckRecord).where(
                LaunchParityCheckRecord.provisioning_id == prov.id,
            )
        ).scalar()
        assert parity_count == 0

        # The uncertain intent was reconciled as confirmed_failed
        mcr = db_session.execute(
            select(MetaCallReconciliationRecord).where(
                MetaCallReconciliationRecord.uncertain_intent_id == ad_intent.id,
            )
        ).scalar_one()
        assert mcr.resolution == "confirmed_failed"

    def test_uncertain_campaign_step_reconstructs_parity(self, db_session):
        """Regression for the ``_derive_manifest_outcome`` ``next()`` crash:
        when the *campaign* step (not the ad step) is the uncertain one,
        reconciliation confirms it succeeded but ``attempt.state`` stays
        ``"intent_committed"`` -- never ``"applied"``. A ``next()`` filtered
        on ``state == "applied"`` finds nothing and raises ``StopIteration``.
        Recovery must find the campaign attempt regardless of its recorded
        state, reconstruct parity, and resolve as succeeded without
        crashing."""
        ws, conn, binding, scen, prov, op, cand, campaign_intent = (
            _full_manifest_uncertain_campaign_env(
                db_session, campaign_object_exists=True
            )
        )
        ctx = TenantContext(
            workspace_id=ws.id, role="system", executor="system:worker",
            meta_mode="customer_sandbox",
        )

        recon = create_recovery_operation(
            db_session, context=ctx, original_operation_id=op.id,
        )
        result = execute_recovery(db_session, ctx, recon.id)

        assert result.state == "resolved"
        assert result.resolution == "succeeded"
        db_session.refresh(op)
        assert op.state == "succeeded"
        db_session.refresh(prov)
        assert prov.provisioning_state == "provisioned"

        # Verify the reconstructed parity record
        parity = db_session.execute(
            select(LaunchParityCheckRecord).where(
                LaunchParityCheckRecord.provisioning_id == prov.id,
                LaunchParityCheckRecord.check_type == "post_provisioning",
            )
        ).scalar_one()
        assert parity.passed is True
        assert parity.all_candidates_present is True
        assert parity.budget_parity_verified is True
        assert parity.config_parity_verified is True
        assert parity.status_parity_verified is True
        assert parity.detail["source"] == "reconciliation"

        # The uncertain campaign intent was reconciled as confirmed_succeeded
        mcr = db_session.execute(
            select(MetaCallReconciliationRecord).where(
                MetaCallReconciliationRecord.uncertain_intent_id == campaign_intent.id,
            )
        ).scalar_one()
        assert mcr.resolution == "confirmed_succeeded"
