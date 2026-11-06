"""Multi-candidate Launch preview creation (spec §3.15-§3.16, §5.1-§5.2).

Flat in ``tests/`` with a local scratch SQLite engine/session -- mirroring
``tests/test_launch_records.py`` -- since no shared ``db_session`` fixture
exists for this suite. ``WorkspaceRecord`` has a ``name`` field (not
``display_name``/``slug``), so the workspace fixture below uses ``name``.
"""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    LaunchConfirmationRecord,
    OperationRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.entitlements.credentials import CredentialVersionRecord
from app.launch.models import (
    LaunchCandidateRecord,
    MetaLaunchConfirmationRecord,
    MetaLaunchPreviewRecord,
)
from app.launch.service import (
    LaunchConflict,
    confirm_multi_candidate_preview,
    consume_multi_candidate_confirmation,
    create_multi_candidate_preview,
)
from app.meta.models import MetaConnectionRecord, MetaDataSourceBindingRecord
from app.platform.tenancy import TenantContext


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def db_session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-launch-multi-"))
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
            ExperimentRecord.__table__,
            ExperimentVersionRecord.__table__,
            ExperimentVersionCandidateRecord.__table__,
            MetaLaunchPreviewRecord.__table__,
            LaunchCandidateRecord.__table__,
            MetaLaunchConfirmationRecord.__table__,
        ],
    )
    session = Session(bind=engine)
    # See module docstring: the legacy LaunchConfirmationRecord.preview_id
    # FK points at the old single-candidate launch_previews table, which
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


def _launch_env(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="T")
    session.add(ws)
    session.flush()
    now = utcnow()
    experiment = ExperimentRecord(
        id="exp_1", workspace_id=ws.id, product_id="prod_1",
        name="Frozen launch", state="frozen", revision=4,
        config_json={}, metric_contract_hash="m" * 64,
        frozen_snapshot_json={}, frozen_snapshot_hash="f" * 64,
        record_schema_version=2, frozen_at=now,
    )
    session.add(experiment)
    session.flush()
    version = ExperimentVersionRecord(
        id="expv_1", workspace_id=ws.id, experiment_id=experiment.id,
        canonicalization_version="test", frozen_snapshot_json={},
        frozen_snapshot_hash="f" * 64, metric_contract_hash="m" * 64,
        market="US", placement="feed", landing_page_evidence_hash="l" * 64,
        audience_snapshot_json={}, audience_snapshot_hash="a" * 64,
        daily_budget_amount=Decimal("5"), total_budget_cap=Decimal("100"),
        budget_currency="USD", schedule_start_at=now,
        schedule_end_at=now + timedelta(days=7), schedule_timezone="UTC",
        attribution_click_window_days=7, attribution_view_window_days=1,
        statistical_plan_json={}, statistical_plan_hash="s" * 64,
        marketing_vars_schema_version="1", product_version_id="pv_1",
        offer_version_id="ov_1", candidates_hash="c" * 64,
        concept_resolutions_hash="r" * 64,
    )
    session.add(version)
    session.flush()
    for index in range(2):
        session.add(ExperimentVersionCandidateRecord(
            id=f"evc_{index + 1}", workspace_id=ws.id,
            experiment_version_id=version.id, source_kind="generation",
            asset_version_id=f"av_{index + 1}",
            asset_version_content_sha256=str(index + 1) * 64,
            position=index, marketing_vars_json={},
            marketing_vars_hash="v" * 64, execution_mode="sandbox",
            eligibility_evidence_json={}, eligibility_evidence_hash="e" * 64,
            concepts_hash="n" * 64, candidate_hash=str(index + 2) * 64,
            evaluated_at=now,
        ))
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


def test_create_multi_candidate_preview(db_session):
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {"headline": "A"}},
        {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
         "creative_config": {"headline": "B"}},
    ]
    preview, cands = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1",
        experiment_version_id="expv_1",
        connection_id=conn.id,
        ad_account_id="act_1",
        candidates=candidates,
        daily_budget_minor=500,
        currency="USD",
        campaign_config={"objective": "OUTCOME_TRAFFIC"},
    )
    assert preview.candidate_count == 2
    assert preview.state == "draft"
    assert preview.meta_binding_id == binding.id
    assert len(cands) == 2
    assert cands[0].candidate_index == 0
    assert cands[1].candidate_index == 1
    assert cands[0].adset_idempotency_key
    assert cands[0].ad_idempotency_key
    assert cands[0].adset_idempotency_key != cands[1].adset_idempotency_key
    assert cands[0].adset_idempotency_key != cands[0].ad_idempotency_key

    # Rows are actually persisted, not just returned.
    persisted = db_session.get(MetaLaunchPreviewRecord, preview.id)
    assert persisted is not None
    assert persisted.frozen_snapshot["candidate_count"] == 2


def test_preview_hash_deterministic(db_session):
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {"headline": "A"}},
        {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
         "creative_config": {"headline": "B"}},
    ]
    p1, _ = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1", experiment_version_id="expv_1",
        connection_id=conn.id, ad_account_id="act_1",
        candidates=candidates, daily_budget_minor=500,
        currency="USD", campaign_config={},
    )
    assert len(p1.preview_hash) == 64  # SHA-256 hex


def test_idempotency_keys_stable_across_calls_with_same_preview_id(db_session):
    """Idempotency keys are deterministic per (connection, preview, candidate, object)."""
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {"headline": "A"}},
        {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
         "creative_config": {"headline": "B"}},
    ]
    p1, c1 = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1", experiment_version_id="expv_1",
        connection_id=conn.id, ad_account_id="act_1",
        candidates=candidates, daily_budget_minor=500,
        currency="USD", campaign_config={},
    )
    p2, c2 = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1", experiment_version_id="expv_1",
        connection_id=conn.id, ad_account_id="act_1",
        candidates=candidates, daily_budget_minor=500,
        currency="USD", campaign_config={},
    )
    # Different preview ids -> different idempotency keys (canonical
    # identity includes preview_id), but both deterministic (64-hex HMAC).
    assert p1.id != p2.id
    assert len(c1[0].adset_idempotency_key) == 64
    assert c1[0].adset_idempotency_key != c2[0].adset_idempotency_key


def test_create_multi_candidate_preview_rejects_invalid_budget(db_session):
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {}},
    ]
    from app.launch.service import LaunchError
    with pytest.raises(LaunchError):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id=conn.id, ad_account_id="act_1",
            candidates=candidates, daily_budget_minor=0,
            currency="USD", campaign_config={},
        )


def test_create_multi_candidate_preview_rejects_empty_candidates(db_session):
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    from app.launch.service import LaunchError
    with pytest.raises(LaunchError):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id=conn.id, ad_account_id="act_1",
            candidates=[], daily_budget_minor=500,
            currency="USD", campaign_config={},
        )


def test_create_multi_candidate_preview_rejects_candidate_manifest_drift(db_session):
    ws, conn, _binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    from app.launch.service import LaunchConflict
    with pytest.raises(LaunchConflict, match="launch_candidate_manifest_mismatch"):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id=conn.id, ad_account_id="act_1",
            candidates=[
                {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2"},
                {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1"},
            ],
            daily_budget_minor=500, currency="USD",
            campaign_config={"initial_status": "PAUSED"},
        )


def test_create_multi_candidate_preview_rejects_mixed_frozen_modes(db_session):
    ws, conn, _binding = _launch_env(db_session)
    db_session.get(ExperimentVersionCandidateRecord, "evc_2").execution_mode = "mock"
    db_session.flush()
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    from app.launch.service import LaunchConflict
    with pytest.raises(LaunchConflict, match="execution_mode_mixed"):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id=conn.id, ad_account_id="act_1",
            candidates=[
                {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1"},
                {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2"},
            ],
            daily_budget_minor=500, currency="USD",
            campaign_config={"initial_status": "PAUSED"},
        )


def test_create_multi_candidate_preview_rejects_non_paused_config(db_session):
    ws, conn, _binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    from app.launch.service import LaunchConflict
    with pytest.raises(LaunchConflict, match="PAUSED"):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id=conn.id, ad_account_id="act_1",
            candidates=[
                {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1"},
                {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2"},
            ],
            daily_budget_minor=500, currency="USD",
            campaign_config={"initial_status": "ACTIVE"},
        )


def test_confirm_multi_candidate_preview(db_session, monkeypatch):
    monkeypatch.setattr("app.config.LAUNCH_CONFIRMATION_SECRET", "test-confirm-secret")
    monkeypatch.setattr(
        "app.experiments.service.validate_live_launch_eligibility",
        lambda session, _context, **_kwargs: list(
            session.query(ExperimentVersionCandidateRecord).order_by(
                ExperimentVersionCandidateRecord.position
            )
        ),
    )
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {"headline": "A"}},
        {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
         "creative_config": {"headline": "B"}},
    ]
    preview, _ = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1", experiment_version_id="expv_1",
        connection_id=conn.id, ad_account_id="act_1",
        candidates=candidates, daily_budget_minor=500,
        currency="USD", campaign_config={},
    )
    issued = confirm_multi_candidate_preview(
        db_session, context=ctx,
        preview_id=preview.id, preview_hash=preview.preview_hash,
    )
    assert issued.preview.state == "confirmed"
    assert issued.preview.confirmed_at is not None
    assert issued.confirmation.context_hash
    assert issued.confirmation.expires_at <= preview.expires_at
    consumed = consume_multi_candidate_confirmation(
        db_session, ctx, preview, issued.confirmation_token,
    )
    assert consumed.consumed_at is not None
    with pytest.raises(LaunchConflict, match="confirmation_token_consumed"):
        consume_multi_candidate_confirmation(
            db_session, ctx, preview, issued.confirmation_token,
        )


def test_confirm_multi_candidate_preview_hash_mismatch_raises(db_session):
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {}},
        {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
         "creative_config": {}},
    ]
    preview, _ = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1", experiment_version_id="expv_1",
        connection_id=conn.id, ad_account_id="act_1",
        candidates=candidates, daily_budget_minor=500,
        currency="USD", campaign_config={},
    )
    from app.launch.service import LaunchConflict
    with pytest.raises(LaunchConflict):
        confirm_multi_candidate_preview(
            db_session, context=ctx,
            preview_id=preview.id, preview_hash="not-the-real-hash",
        )


def test_confirm_expired_preview_raises(db_session):
    from datetime import datetime, timezone
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    preview, _ = create_multi_candidate_preview(
        db_session, context=ctx,
        experiment_id="exp_1", experiment_version_id="expv_1",
        connection_id=conn.id, ad_account_id="act_1",
        candidates=[
            {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
             "creative_config": {}},
            {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
             "creative_config": {}},
        ],
        daily_budget_minor=500, currency="USD", campaign_config={},
    )
    # Force expiration
    preview.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db_session.flush()
    with pytest.raises(Exception, match="expired"):
        confirm_multi_candidate_preview(
            db_session, context=ctx,
            preview_id=preview.id, preview_hash=preview.preview_hash,
        )


def test_create_multi_candidate_preview_rejects_malformed_candidate(db_session):
    """A candidate dict missing a required field raises LaunchError, not KeyError."""
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    from app.launch.service import LaunchError
    with pytest.raises(LaunchError):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id=conn.id, ad_account_id="act_1",
            candidates=[{"asset_version_id": "av_1", "creative_config": {}}],
            daily_budget_minor=500, currency="USD", campaign_config={},
        )


def test_create_multi_candidate_preview_unknown_connection_raises_not_found(db_session):
    ws, conn, binding = _launch_env(db_session)
    ctx = TenantContext(
        workspace_id=ws.id, role="member", initiator_id="usr_a",
        meta_mode="customer_sandbox",
    )
    candidates = [
        {"experiment_version_candidate_id": "evc_1", "asset_version_id": "av_1",
         "creative_config": {}},
        {"experiment_version_candidate_id": "evc_2", "asset_version_id": "av_2",
         "creative_config": {}},
    ]
    from app.launch.service import LaunchNotFound
    with pytest.raises(LaunchNotFound):
        create_multi_candidate_preview(
            db_session, context=ctx,
            experiment_id="exp_1", experiment_version_id="expv_1",
            connection_id="mcon_does_not_exist", ad_account_id="act_1",
            candidates=candidates, daily_budget_minor=500,
            currency="USD", campaign_config={},
        )
