"""Launch domain records: multi-candidate preview, provisioning saga (spec §3.15-§3.21).

Flat in ``tests/`` and using a scratch SQLite engine/session -- mirroring
``tests/test_meta_call_evidence.py`` -- since no shared ``db_session``
fixture exists in ``tests/conftest.py`` for this suite. ``WorkspaceRecord``
has a ``name`` field (not ``display_name``/``slug``), so the workspace
fixture below uses ``name``.

SQLite foreign-key enforcement is disabled for this fixture's connection
(see the ``db_session`` fixture below): the pre-existing
``LaunchConfirmationRecord.preview_id`` column (from
``app.adapters.persistence.models``, reused as-is per this task's brief)
carries a hard single-column ``ForeignKey("launch_previews.id")`` pointing
at the *old* single-candidate preview table -- not the new
``MetaLaunchPreviewRecord`` (table ``launch_preview_records``) this task
introduces. Re-creating that legacy table's full dependency chain
(Experiment/ExperimentEntry/AssetVersion rows) just to satisfy a stale FK
is out of scope for these model-shape tests, and PostgreSQL's real RLS +
FK enforcement is exercised by later integration tasks, not this SQLite
compatibility path. CHECK and UNIQUE/partial-unique-index enforcement --
the constraints this suite actually cares about (spec §3.17, §3.19,
§3.21) -- are unaffected by the ``foreign_keys`` pragma.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
    LaunchReconciliationRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from app.meta.models import (
    MetaCallIntentRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-launch-records-"))
    engine = create_engine(
        _sqlite_url(tmp_dir / "launch_records.db"),
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
            MetaCallIntentRecord.__table__,
            MetaLaunchPreviewRecord.__table__,
            LaunchCandidateRecord.__table__,
            LaunchProvisioningRecord.__table__,
            LaunchProvisioningStatusHistoryRecord.__table__,
            LaunchSagaStepAttemptRecord.__table__,
            LaunchParityCheckRecord.__table__,
            LaunchReconciliationRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    # See module docstring: disables enforcement of the legacy
    # LaunchConfirmationRecord.preview_id -> launch_previews.id FK, which
    # this suite does not create/populate. Must run before any statement
    # opens an implicit transaction -- SQLite ignores this pragma once a
    # transaction is active.
    session.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _launch_setup(session):
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
    return ws, conn, binding


def _make_preview(session, ws, conn, binding, candidate_count=3):
    preview = MetaLaunchPreviewRecord(
        id=new_id("lprev"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        experiment_id="exp_1",
        experiment_version_id="expv_1",
        candidate_count=candidate_count,
        campaign_config={"objective": "OUTCOME_TRAFFIC"},
        daily_budget_minor=500,
        currency="USD",
        preview_hash="ph_abc",
        frozen_snapshot={"candidates": []},
    )
    session.add(preview)
    session.flush()
    return preview


def _make_provisioning(session, ws, conn, preview, *, provisioning_state="pending"):
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="queued",
    )
    session.add(op)
    session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash=preview.preview_hash, token_hash=new_id("th"),
        actor_id="usr_test",
        expires_at=utcnow(),
    )
    session.add(conf)
    session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        preview_id=preview.id,
        confirmation_id=conf.id,
        operation_id=op.id,
        campaign_idempotency_key=new_id("cik"),
        provisioning_state=provisioning_state,
    )
    session.add(prov)
    session.flush()
    return prov, op


def test_create_multi_candidate_preview(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=3)
    assert preview.state == "draft"
    assert preview.candidate_count == 3


def test_preview_confirmed_requires_confirmed_at(db_session):
    """CHECK: (state = 'confirmed') = (confirmed_at IS NOT NULL)."""
    ws, conn, binding = _launch_setup(db_session)
    preview = MetaLaunchPreviewRecord(
        id=new_id("lprev"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id, meta_binding_id=binding.id,
        experiment_id="exp_1", experiment_version_id="expv_1",
        candidate_count=1, campaign_config={},
        daily_budget_minor=500, currency="USD",
        preview_hash="ph", frozen_snapshot={},
        state="confirmed",
    )
    db_session.add(preview)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_launch_candidate(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=2)
    cand = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=0,
        experiment_version_candidate_id="evc_1",
        asset_version_id="av_1",
        creative_config={"headline": "Test"},
        adset_idempotency_key="ik_adset_0",
        ad_idempotency_key="ik_ad_0",
    )
    db_session.add(cand)
    db_session.flush()
    assert cand.candidate_index == 0


def test_launch_candidate_unique_preview_index(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=2)
    db_session.add(LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=0,
        experiment_version_candidate_id="evc_1",
        asset_version_id="av_1",
        creative_config={},
        adset_idempotency_key="ik_adset_0",
        ad_idempotency_key="ik_ad_0",
    ))
    db_session.flush()
    db_session.add(LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=0,
        experiment_version_candidate_id="evc_2",
        asset_version_id="av_2",
        creative_config={},
        adset_idempotency_key="ik_adset_0b",
        ad_idempotency_key="ik_ad_0b",
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_provisioning_with_fencing(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, _op = _make_provisioning(db_session, ws, conn, preview)
    assert prov.fencing_token == 0
    assert prov.activation_state == "paused"


def test_provisioning_activation_requires_provisioned(db_session):
    """CHECK: activation_state != 'paused' -> provisioning_state = 'provisioned'."""
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type="launch_provisioning", state="queued",
    )
    db_session.add(op)
    db_session.flush()
    conf = LaunchConfirmationRecord(
        id=new_id("lconf"), preview_id=preview.id,
        preview_hash=preview.preview_hash, token_hash=new_id("th"),
        actor_id="usr_test", expires_at=utcnow(),
    )
    db_session.add(conf)
    db_session.flush()
    prov = LaunchProvisioningRecord(
        id=new_id("lprov"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        preview_id=preview.id,
        confirmation_id=conf.id,
        operation_id=op.id,
        campaign_idempotency_key=new_id("cik"),
        provisioning_state="pending",
        activation_state="active",
    )
    db_session.add(prov)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_provisioning_status_history(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, _op = _make_provisioning(db_session, ws, conn, preview)
    event = LaunchProvisioningStatusHistoryRecord(
        id=new_id("lpsh"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        field="provisioning_state", from_state="pending", to_state="provisioning",
        executor="system:worker",
    )
    db_session.add(event)
    db_session.flush()
    assert event.reason is None


def test_provisioning_status_history_field_check(db_session):
    """CHECK: field IN ('provisioning_state', 'activation_state')."""
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, _op = _make_provisioning(db_session, ws, conn, preview)
    event = LaunchProvisioningStatusHistoryRecord(
        id=new_id("lpsh"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        field="bogus_field", from_state="pending", to_state="provisioning",
        executor="system:worker",
    )
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.flush()


def _make_intent(session, ws, conn, binding, op):
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=ws.id,
        meta_mode="customer_sandbox",
        meta_connection_id=conn.id,
        meta_binding_id=binding.id,
        ad_account_id="act_1",
        operation_id=op.id,
        meta_api_method="POST",
        meta_api_endpoint="/v21.0/act_1/campaigns",
        request_parameters={},
        request_fingerprint_hash="fp",
        connection_state_at_intent="active",
        binding_state_at_intent="active",
        call_type="saga_step",
        scenario_version_id="scen_1",
        write_permitted=False,
        executor="system:worker",
    )
    session.add(intent)
    session.flush()
    return intent


def test_create_campaign_level_saga_step_attempt(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, op = _make_provisioning(db_session, ws, conn, preview)
    intent = _make_intent(db_session, ws, conn, binding, op)
    attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent.id, candidate_id=None,
        step_type="create_campaign", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    )
    db_session.add(attempt)
    db_session.flush()
    assert attempt.candidate_id is None
    assert attempt.remote_object_id is None


def test_create_candidate_level_saga_step_attempt(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, op = _make_provisioning(db_session, ws, conn, preview)
    intent = _make_intent(db_session, ws, conn, binding, op)
    cand = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=0,
        experiment_version_candidate_id="evc_1",
        asset_version_id="av_1",
        creative_config={},
        adset_idempotency_key="ik_adset_0",
        ad_idempotency_key="ik_ad_0",
    )
    db_session.add(cand)
    db_session.flush()
    attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent.id, candidate_id=cand.id,
        step_type="create_adset", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    )
    db_session.add(attempt)
    db_session.flush()
    assert attempt.candidate_id == cand.id


def test_saga_step_attempt_campaign_level_unique_index(db_session):
    """UNIQUE(provisioning_id, step_type, attempt_number) WHERE candidate_id IS NULL."""
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, op = _make_provisioning(db_session, ws, conn, preview)
    intent1 = _make_intent(db_session, ws, conn, binding, op)
    intent2 = _make_intent(db_session, ws, conn, binding, op)
    db_session.add(LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent1.id, candidate_id=None,
        step_type="create_campaign", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    ))
    db_session.flush()
    db_session.add(LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent2.id, candidate_id=None,
        step_type="create_campaign", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_saga_step_attempt_candidate_level_unique_index(db_session):
    """UNIQUE(provisioning_id, step_type, candidate_id, attempt_number) WHERE candidate_id IS NOT NULL."""
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, op = _make_provisioning(db_session, ws, conn, preview)
    intent1 = _make_intent(db_session, ws, conn, binding, op)
    intent2 = _make_intent(db_session, ws, conn, binding, op)
    cand = LaunchCandidateRecord(
        id=new_id("lcand"), workspace_id=ws.id,
        meta_mode="customer_sandbox", preview_id=preview.id,
        candidate_index=0,
        experiment_version_candidate_id="evc_1",
        asset_version_id="av_1",
        creative_config={},
        adset_idempotency_key="ik_adset_0",
        ad_idempotency_key="ik_ad_0",
    )
    db_session.add(cand)
    db_session.flush()
    db_session.add(LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent1.id, candidate_id=cand.id,
        step_type="create_adset", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    ))
    db_session.flush()
    # Same (provisioning, step_type, candidate) but campaign-level slot
    # (candidate_id NULL) does not collide -- different partial index.
    db_session.add(LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent2.id, candidate_id=None,
        step_type="create_adset", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    ))
    db_session.flush()
    # But a second attempt_number=1 for the same candidate DOES collide.
    intent3 = _make_intent(db_session, ws, conn, binding, op)
    db_session.add(LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        intent_id=intent3.id, candidate_id=cand.id,
        step_type="create_adset", attempt_number=1,
        fencing_token=prov.fencing_token,
        state="started", executor="system:worker",
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_create_parity_check(db_session):
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, _op = _make_provisioning(db_session, ws, conn, preview)
    check = LaunchParityCheckRecord(
        id=new_id("lpc"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        check_type="post_provisioning",
        all_candidates_present=True,
        budget_parity_verified=True,
        config_parity_verified=True,
        status_parity_verified=True,
        detail={"candidates": []},
        passed=True,
        executor="system:worker",
    )
    db_session.add(check)
    db_session.flush()
    assert check.passed is True


def test_parity_check_type_check(db_session):
    """CHECK: check_type IN ('post_provisioning', 'post_activation', 'periodic')."""
    ws, conn, binding = _launch_setup(db_session)
    preview = _make_preview(db_session, ws, conn, binding, candidate_count=1)
    prov, _op = _make_provisioning(db_session, ws, conn, preview)
    check = LaunchParityCheckRecord(
        id=new_id("lpc"), workspace_id=ws.id,
        meta_mode="customer_sandbox", provisioning_id=prov.id,
        check_type="bogus_check",
        all_candidates_present=True,
        budget_parity_verified=True,
        config_parity_verified=True,
        status_parity_verified=True,
        detail={},
        passed=True,
        executor="system:worker",
    )
    db_session.add(check)
    with pytest.raises(IntegrityError):
        db_session.flush()


def _make_operation(session, ws, command_type="launch_reconciliation_source"):
    op = OperationRecord(
        id=new_id("op"), workspace_id=ws.id,
        command_type=command_type, state="queued",
    )
    session.add(op)
    session.flush()
    return op


def test_create_reconciliation(db_session):
    ws, _conn, _binding = _launch_setup(db_session)
    original_op = _make_operation(db_session, ws)
    recovery_op = _make_operation(db_session, ws, command_type="launch_reconciliation_recovery")
    rec = LaunchReconciliationRecord(
        id=new_id("lrec"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        original_operation_id=original_op.id,
        recovery_operation_id=recovery_op.id,
    )
    db_session.add(rec)
    db_session.flush()
    assert rec.state == "open"
    assert rec.resolution is None


def test_reconciliation_resolved_requires_resolution_and_timestamp(db_session):
    """CHECK: (state = 'resolved') = (resolution IS NOT NULL) AND (resolved_at IS NOT NULL)."""
    ws, _conn, _binding = _launch_setup(db_session)
    original_op = _make_operation(db_session, ws)
    recovery_op = _make_operation(db_session, ws, command_type="launch_reconciliation_recovery")
    rec = LaunchReconciliationRecord(
        id=new_id("lrec"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        original_operation_id=original_op.id,
        recovery_operation_id=recovery_op.id,
        state="resolved",
    )
    db_session.add(rec)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_reconciliation_open_uniqueness_per_original_operation(db_session):
    """UNIQUE(original_operation_id) WHERE state = 'open'."""
    ws, _conn, _binding = _launch_setup(db_session)
    original_op = _make_operation(db_session, ws)
    recovery_op_1 = _make_operation(db_session, ws, command_type="launch_reconciliation_recovery")
    recovery_op_2 = _make_operation(db_session, ws, command_type="launch_reconciliation_recovery")
    db_session.add(LaunchReconciliationRecord(
        id=new_id("lrec"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        original_operation_id=original_op.id,
        recovery_operation_id=recovery_op_1.id,
    ))
    db_session.flush()
    db_session.add(LaunchReconciliationRecord(
        id=new_id("lrec"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        original_operation_id=original_op.id,
        recovery_operation_id=recovery_op_2.id,
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_reconciliation_resolved_does_not_block_new_open(db_session):
    """A resolved reconciliation does not count against the open-partial-unique index."""
    ws, _conn, _binding = _launch_setup(db_session)
    original_op = _make_operation(db_session, ws)
    recovery_op_1 = _make_operation(db_session, ws, command_type="launch_reconciliation_recovery")
    recovery_op_2 = _make_operation(db_session, ws, command_type="launch_reconciliation_recovery")
    db_session.add(LaunchReconciliationRecord(
        id=new_id("lrec"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        original_operation_id=original_op.id,
        recovery_operation_id=recovery_op_1.id,
        state="resolved",
        resolution="succeeded",
        resolved_at=utcnow(),
    ))
    db_session.flush()
    db_session.add(LaunchReconciliationRecord(
        id=new_id("lrec"), workspace_id=ws.id,
        meta_mode="customer_sandbox",
        original_operation_id=original_op.id,
        recovery_operation_id=recovery_op_2.id,
    ))
    db_session.flush()
