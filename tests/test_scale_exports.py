"""Export creation, Operation lifecycle, download intents.

Issue #87 Task 5. Flat scratch-SQLite style (mirroring
``tests/test_scale_targets.py`` / ``tests/test_scale_breakdowns.py``): a
fresh temp-file SQLite database per test via pytest fixtures, with the
shared Engine-level ``PRAGMA foreign_keys=ON`` listener enforcing every FK
once ``app.adapters.persistence.database`` is imported (it is, transitively,
here).
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
from app.platform.operations import OperationState, get_operation
from app.platform.tenancy import TenantContext, tenant_scope
from app.scale.exports import (
    ExportError,
    ExportNotCompleteError,
    ExportNotFoundError,
    create_export,
    create_export_download_intent,
    run_scale_export,
)
from app.scale.models import ScaleEnrollmentRecord, ScaleEpisodeRecord, ScaleExportRecord


@pytest.fixture
def session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-sexp-"))
    engine = create_engine(
        f"sqlite:///{(tmp_dir / 'sexp.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    sess = Session(bind=engine)
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture
def ws_id(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="Export lifecycle test")
    session.add(ws)
    session.flush()
    return ws.id


def _owner_context(ws_id: str, meta_mode: str = "customer_sandbox") -> TenantContext:
    return TenantContext(
        workspace_id=ws_id,
        role="owner",
        initiator_id="user-owner",
        executor="user-owner",
        meta_mode=meta_mode,
    )


def _seed_enrollment_and_episode(session: Session, ws_id: str) -> tuple[str, str]:
    enrollment = ScaleEnrollmentRecord(
        id=new_id("senr"),
        workspace_id=ws_id,
        meta_mode="customer_sandbox",
        meta_connection_id="conn-1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act-1",
        winner_decision_id="wdec-1",
        winner_confirmation_id="wconf-1",
        asset_version_id="asset-1",
        product_version_id="pv-1",
        offer_version_id="ov-1",
        market_code="US",
        remote_binding_key="binding-1",
        lifecycle="active",
        creator_id="user-owner",
        original_initiator_id="user-owner",
    )
    session.add(enrollment)
    session.flush()

    episode = ScaleEpisodeRecord(
        id=new_id("sep"),
        workspace_id=ws_id,
        meta_mode="customer_sandbox",
        enrollment_id=enrollment.id,
        winner_decision_id="wdec-1",
        winner_confirmation_id="wconf-1",
        asset_version_id="asset-1",
        product_version_id="pv-1",
        offer_version_id="ov-1",
        market_code="US",
        meta_connection_id="conn-1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act-1",
        delivery_config_baseline_source_id="baseline-1",
        delivery_evidence_hash="hash-1",
        delivery_comparability_hash="hash-2",
        metric_semantic_contract_set_id="mscs-1",
        timezone_name="UTC",
        currency="USD",
        attribution_semantics_hash="hash-3",
        phase="collecting",
        started_at=utcnow(),
        started_local_day=utcnow().date(),
        revision=1,
    )
    session.add(episode)
    session.flush()
    return enrollment.id, episode.id


class TestCreateExport:
    def test_creates_export_and_operation(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)

        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-1",
        )

        assert result.export_id.startswith("sexp")
        assert result.operation_id is not None
        with tenant_scope(session, ctx):
            operation = get_operation(session, result.operation_id)
        assert operation.state == OperationState.QUEUED.value
        assert operation.command_type == "scale_export"

        export = session.get(ScaleExportRecord, result.export_id)
        assert export is not None
        assert export.enrollment_id == enrollment_id
        assert export.episode_id == episode_id
        assert export.export_kind == "evidence_summary"
        assert export.completed_at is None
        assert export.result_object_reference is None

    def test_idempotent_replay(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)

        r1 = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-2",
        )
        r2 = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-2",
        )

        assert r1.export_id == r2.export_id
        assert r1.operation_id == r2.operation_id

    def test_rejects_unknown_enrollment(self, session, ws_id):
        ctx = _owner_context(ws_id)
        with pytest.raises(ExportError, match="enrollment_not_found"):
            create_export(
                session, ctx,
                enrollment_id="senr_missing",
                episode_id="sep_missing",
                export_kind="evidence_summary",
                idempotency_key="export-3",
            )

    def test_rejects_episode_from_other_enrollment(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        other_enrollment_id, _ = _seed_enrollment_and_episode(session, ws_id)

        with pytest.raises(ExportError, match="episode_not_found"):
            create_export(
                session, ctx,
                enrollment_id=other_enrollment_id,
                episode_id=episode_id,
                export_kind="evidence_summary",
                idempotency_key="export-4",
            )

    def test_cross_tenant_enrollment_not_visible(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)

        other_ws = WorkspaceRecord(id=new_id("ws"), name="Other workspace")
        session.add(other_ws)
        session.flush()
        other_ctx = _owner_context(other_ws.id)

        with pytest.raises(ExportError, match="enrollment_not_found"):
            create_export(
                session, other_ctx,
                enrollment_id=enrollment_id,
                episode_id=episode_id,
                export_kind="evidence_summary",
                idempotency_key="export-5",
            )


class TestRunScaleExport:
    def test_completes_export_and_succeeds_operation(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-run-1",
        )

        with tenant_scope(session, ctx):
            run_scale_export(session, ctx, result.export_id)

        export = session.get(ScaleExportRecord, result.export_id)
        assert export.completed_at is not None
        assert export.result_object_reference is not None

        with tenant_scope(session, ctx):
            operation = get_operation(session, result.operation_id)
        assert operation.state == OperationState.SUCCEEDED.value
        assert operation.result_resource_type == "scale_export"
        assert operation.result_resource_id == export.id

    def test_second_call_is_a_noop(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-run-2",
        )

        with tenant_scope(session, ctx):
            run_scale_export(session, ctx, result.export_id)
            first_ref = session.get(ScaleExportRecord, result.export_id).result_object_reference
            # A redelivered Job must not re-transition an already-terminal
            # Operation (that would raise InvalidOperationTransitionError).
            run_scale_export(session, ctx, result.export_id)

        export = session.get(ScaleExportRecord, result.export_id)
        assert export.result_object_reference == first_ref

    def test_unknown_export_id_is_a_noop(self, session, ws_id):
        ctx = _owner_context(ws_id)
        with tenant_scope(session, ctx):
            run_scale_export(session, ctx, "sexp_missing")  # must not raise

    def test_build_failure_fails_the_operation(self, session, ws_id, monkeypatch):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-run-3",
        )

        import app.scale.exports as exports_module

        def _boom(export):
            raise RuntimeError("synthetic build failure")

        monkeypatch.setattr(exports_module, "_build_safe_result_reference", _boom)

        with pytest.raises(RuntimeError, match="synthetic build failure"):
            with tenant_scope(session, ctx):
                run_scale_export(session, ctx, result.export_id)

        with tenant_scope(session, ctx):
            operation = get_operation(session, result.operation_id)
        assert operation.state == OperationState.FAILED.value

        export = session.get(ScaleExportRecord, result.export_id)
        assert export.completed_at is None
        assert export.result_object_reference is None


class TestCreateExportDownloadIntent:
    def test_rejects_unknown_export(self, session, ws_id):
        ctx = _owner_context(ws_id)
        with pytest.raises(ExportNotFoundError):
            create_export_download_intent(
                session, ctx,
                export_id="sexp_missing",
                idempotency_key="dl-1",
            )

    def test_rejects_incomplete_export(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-dl-1",
        )

        with pytest.raises(ExportNotCompleteError):
            create_export_download_intent(
                session, ctx,
                export_id=result.export_id,
                idempotency_key="dl-2",
            )

    def test_issues_intent_for_completed_export(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-dl-2",
        )
        with tenant_scope(session, ctx):
            run_scale_export(session, ctx, result.export_id)

        intent = create_export_download_intent(
            session, ctx,
            export_id=result.export_id,
            idempotency_key="dl-3",
        )

        assert intent.intent_id is not None
        assert intent.expires_at is not None

    def test_idempotent_replay_returns_same_intent(self, session, ws_id):
        ctx = _owner_context(ws_id)
        enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws_id)
        result = create_export(
            session, ctx,
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            export_kind="evidence_summary",
            idempotency_key="export-dl-3",
        )
        with tenant_scope(session, ctx):
            run_scale_export(session, ctx, result.export_id)

        i1 = create_export_download_intent(
            session, ctx, export_id=result.export_id, idempotency_key="dl-4",
        )
        i2 = create_export_download_intent(
            session, ctx, export_id=result.export_id, idempotency_key="dl-4",
        )

        assert i1.intent_id == i2.intent_id
