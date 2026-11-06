"""Target lifecycle: context resolution, preview/publish/withdraw.

Issue #87 Task 2. Flat scratch-SQLite style (mirroring
``tests/test_scale_command_journal.py``): a fresh temp-file SQLite database
per test via pytest fixtures. Foreign keys are enforced (the shared
``Engine``-level ``PRAGMA foreign_keys=ON`` listener in
``app.adapters.persistence.database`` applies to every SQLite engine once
that module is imported, which it is transitively here), so every test
creates a real ``WorkspaceRecord`` and threads its id through the tenant
contexts rather than using a bare literal workspace id.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.platform.tenancy import TenantContext
from app.scale.models import ScaleEnrollmentRecord, ScaleEpisodeRecord
from app.scale.targets import (
    TargetLifecycleError,
    TargetNotFoundError,
    TargetPreviewConsumedError,
    TargetPreviewExpiredError,
    create_publication_preview,
    create_withdrawal_preview,
    get_target,
    list_targets,
    publish_target,
    resolve_target_contexts,
    target_actions,
    withdraw_target,
)


@pytest.fixture
def session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-stgt-"))
    engine = create_engine(
        f"sqlite:///{(tmp_dir / 'stgt.db').as_posix()}",
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
    ws = WorkspaceRecord(id=new_id("ws"), name="Target lifecycle test")
    session.add(ws)
    session.flush()
    return ws.id


def _owner_context(ws_id: str) -> TenantContext:
    return TenantContext(
        workspace_id=ws_id,
        role="owner",
        initiator_id="user-owner",
        executor="user-owner",
        meta_mode="customer_sandbox",
    )


def _member_context(ws_id: str) -> TenantContext:
    return TenantContext(
        workspace_id=ws_id,
        role="member",
        initiator_id="user-member",
        executor="user-member",
        meta_mode="customer_sandbox",
    )


class TestCreatePublicationPreview:
    def test_creates_preview_with_valid_decimals(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("25.50"),
            breakeven_roas=None,
            minimum_economic_spend=Decimal("100.00"),
            idempotency_key="pub-preview-1",
        )
        assert preview.id.startswith("stpp")
        assert preview.target_cpa == Decimal("25.50")
        assert preview.consumed_at is None

    def test_rejects_member(self, session, ws_id):
        ctx = _member_context(ws_id)
        with pytest.raises(TargetLifecycleError, match="owner_required"):
            create_publication_preview(
                session, ctx,
                product_version_id="pv-1",
                offer_version_id="ov-1",
                market_code="US",
                currency="USD",
                target_cpa=Decimal("10.00"),
                idempotency_key="pub-preview-2",
            )

    def test_rejects_no_economic_target(self, session, ws_id):
        ctx = _owner_context(ws_id)
        with pytest.raises(TargetLifecycleError, match="at_least_one_target"):
            create_publication_preview(
                session, ctx,
                product_version_id="pv-1",
                offer_version_id="ov-1",
                market_code="US",
                currency="USD",
                target_cpa=None,
                breakeven_roas=None,
                idempotency_key="pub-preview-3",
            )


class TestPublishTarget:
    def test_publishes_from_preview(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-1",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-2")
        assert target.lifecycle_state == "active"
        assert target.target_cpa == Decimal("20.00")
        assert target.published_at is not None

    def test_rejects_expired_preview(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-3",
        )
        # Force-expire the preview
        preview.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.flush()
        with pytest.raises(TargetPreviewExpiredError):
            publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-4")

    def test_rejects_unknown_preview(self, session, ws_id):
        ctx = _owner_context(ws_id)
        with pytest.raises(TargetNotFoundError):
            publish_target(session, ctx, preview_id="stpp_missing", idempotency_key="pub-missing")

    def test_idempotent_replay(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-5",
        )
        t1 = publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-6")
        t2 = publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-6")
        assert t1.id == t2.id

    def test_rejects_consumed_preview_under_a_new_idempotency_key(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("20.00"),
            idempotency_key="pub-7",
        )
        publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-8")
        with pytest.raises(TargetPreviewConsumedError):
            publish_target(session, ctx, preview_id=preview.id, idempotency_key="pub-9")


class TestWithdrawTarget:
    def test_withdraws_active_target(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            breakeven_roas=Decimal("2.0"),
            idempotency_key="wd-1",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="wd-2")
        wd_preview = create_withdrawal_preview(
            session, ctx,
            target_id=target.id,
            reason="superseded_by_new_offer",
            idempotency_key="wd-3",
        )
        assert wd_preview.id.startswith("stwp")
        assert wd_preview.dependent_advice_thread_count == 0
        withdrawn = withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-4")
        assert withdrawn.lifecycle_state == "withdrawn"
        assert withdrawn.withdrawn_at is not None

    def test_rejects_withdraw_of_non_active_target(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="wd-5",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="wd-6")
        wd_preview = create_withdrawal_preview(
            session, ctx,
            target_id=target.id,
            reason="superseded_by_new_offer",
            idempotency_key="wd-7",
        )
        withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-8")

        with pytest.raises(TargetLifecycleError, match="invalid_lifecycle"):
            create_withdrawal_preview(
                session, ctx,
                target_id=target.id,
                reason="already_withdrawn",
                idempotency_key="wd-9",
            )

    def test_rejects_expired_withdrawal_preview(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="wd-10",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="wd-11")
        wd_preview = create_withdrawal_preview(
            session, ctx,
            target_id=target.id,
            reason="superseded_by_new_offer",
            idempotency_key="wd-12",
        )
        wd_preview.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.flush()
        with pytest.raises(TargetPreviewExpiredError):
            withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-13")

    def test_idempotent_withdraw_replay(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="wd-14",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="wd-15")
        wd_preview = create_withdrawal_preview(
            session, ctx,
            target_id=target.id,
            reason="superseded_by_new_offer",
            idempotency_key="wd-16",
        )
        w1 = withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-17")
        w2 = withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-17")
        assert w1.id == w2.id
        assert w2.lifecycle_state == "withdrawn"

    def test_rejects_consumed_withdrawal_preview_under_a_new_idempotency_key(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="wd-18",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="wd-19")
        wd_preview = create_withdrawal_preview(
            session, ctx,
            target_id=target.id,
            reason="superseded_by_new_offer",
            idempotency_key="wd-20",
        )
        withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-21")
        with pytest.raises(TargetPreviewConsumedError):
            withdraw_target(session, ctx, preview_id=wd_preview.id, idempotency_key="wd-22")


class TestListAndGetTargets:
    def test_lists_by_workspace(self, session, ws_id):
        ctx = _owner_context(ws_id)
        results = list_targets(session, ctx)
        assert isinstance(results, list)
        assert results == []

    def test_lists_after_publish(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="list-1",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="list-2")
        results = list_targets(session, ctx)
        assert [t.id for t in results] == [target.id]

        filtered = list_targets(session, ctx, lifecycle_state="withdrawn")
        assert filtered == []

    def test_get_target_not_found(self, session, ws_id):
        ctx = _owner_context(ws_id)
        with pytest.raises(TargetNotFoundError):
            get_target(session, ctx, "stgt_missing")

    def test_get_target_found(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="get-1",
        )
        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="get-2")
        fetched = get_target(session, ctx, target.id)
        assert fetched.id == target.id


def _seed_enrollment_with_episode(
    session, ws_id: str, *, currency: str = "USD",
) -> None:
    """Directly construct a minimal active Enrollment + its current Episode
    so ``resolve_target_contexts`` has a currency to discover. Bypasses the
    full candidate/lineage chain (``tests.test_scale_enrollment_candidates``)
    deliberately -- this module only needs plain rows in these two tables,
    neither of which carries a lineage FK, to exercise the Episode-join
    currency lookup ruled on for Task 2."""
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
        currency=currency,
        attribution_semantics_hash="hash-3",
        phase="collecting",
        started_at=utcnow(),
        started_local_day=utcnow().date(),
        revision=1,
    )
    session.add(episode)
    session.flush()


class TestResolveTargetContexts:
    def test_empty_workspace_has_no_contexts(self, session, ws_id):
        ctx = _owner_context(ws_id)
        assert resolve_target_contexts(session, ctx) == []

    def test_discovers_currency_via_episode_join(self, session, ws_id):
        _seed_enrollment_with_episode(session, ws_id, currency="USD")
        ctx = _owner_context(ws_id)

        contexts = resolve_target_contexts(session, ctx)

        assert contexts == [{
            "product_version_id": "pv-1",
            "offer_version_id": "ov-1",
            "market_code": "US",
            "currency": "USD",
            "has_active_target": False,
        }]

    def test_reflects_has_active_target_once_published(self, session, ws_id):
        _seed_enrollment_with_episode(session, ws_id, currency="USD")
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx,
            product_version_id="pv-1",
            offer_version_id="ov-1",
            market_code="US",
            currency="USD",
            target_cpa=Decimal("15.00"),
            idempotency_key="ctx-1",
        )
        publish_target(session, ctx, preview_id=preview.id, idempotency_key="ctx-2")

        contexts = resolve_target_contexts(session, ctx)
        assert contexts[0]["has_active_target"] is True


class TestTargetActions:
    # ``target_actions`` only reads ``lifecycle_state``, so a lightweight
    # stand-in is used instead of constructing a real mapped
    # ``ScaleTargetVersionRecord`` via ``__new__`` -- SQLAlchemy's
    # instrumentation requires the instance state ``__init__`` normally
    # installs, which ``__new__`` bypasses.
    @staticmethod
    def _withdraw(lifecycle_state: str, context: TenantContext) -> dict:
        target = SimpleNamespace(lifecycle_state=lifecycle_state)
        return {a["action"]: a for a in target_actions(target, context)}["withdraw"]

    def test_active_target_allows_withdraw_for_the_owner(self):
        withdraw = self._withdraw("active", _owner_context("ws-actions"))
        assert (withdraw["allowed"], withdraw["reason_code"]) == (True, None)

    def test_active_target_disallows_withdraw_for_a_member(self):
        withdraw = self._withdraw("active", _member_context("ws-actions"))
        assert (withdraw["allowed"], withdraw["reason_code"]) == (False, "owner_required")

    def test_withdrawn_target_reports_lifecycle_before_role(self):
        for context in (_owner_context("ws-actions"), _member_context("ws-actions")):
            withdraw = self._withdraw("withdrawn", context)
            assert (withdraw["allowed"], withdraw["reason_code"]) == (False, "invalid_lifecycle")

    def test_active_target_allows_withdraw_for_the_system(self):
        system = _owner_context("ws-actions").for_system_execution()
        withdraw = self._withdraw("active", system)
        assert (withdraw["allowed"], withdraw["reason_code"]) == (True, None)
