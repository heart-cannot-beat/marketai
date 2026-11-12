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
from app.scale.models import (
    ScaleCommandJournalRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScalePolicyVersionRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleTargetPublicationPreviewRecord,
    ScaleTargetVersionRecord,
)
from app.scale.platform_policy import PUBLISHED_POLICY_VERSIONS
from app.scale.targets import (
    TargetLifecycleError,
    TargetNotFoundError,
    TargetPreviewConsumedError,
    TargetPreviewExpiredError,
    applicable_policy,
    create_publication_preview,
    create_withdrawal_preview,
    get_target,
    list_targets,
    platform_spend_floor,
    publish_target,
    resolve_target_contexts,
    target_actions,
    target_below_platform_floor,
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


@pytest.fixture(autouse=True)
def platform_policies(session):
    """The Policies the migrations publish (#127, #132): a Target's minimum
    economic spend is checked against the floor of the one in force (#134).
    ``create_all`` leaves the table empty."""
    for version in PUBLISHED_POLICY_VERSIONS:
        session.add(ScalePolicyVersionRecord(**version.row()))
    session.flush()


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


def _mode_owner(ws_id: str, meta_mode: str) -> TenantContext:
    return TenantContext(
        workspace_id=ws_id,
        role="owner",
        initiator_id="user-owner",
        executor="user-owner",
        meta_mode=meta_mode,
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
            minimum_economic_spend=Decimal("100"),
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
            "platform_spend_floor": "100",
            "platform_spend_floor_unavailable_reason": None,
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

    def test_an_active_target_in_another_currency_is_not_this_contexts(self, session, ws_id):
        """#398: the flag reads the same key as the one-active-Target rule,
        currency included."""
        _seed_enrollment_with_episode(session, ws_id, currency="USD")
        ctx = _owner_context(ws_id)
        _publish(session, ctx, cpa="15.00", key="ctx-3", currency="EUR")

        assert resolve_target_contexts(session, ctx)[0]["has_active_target"] is False
        _publish(session, ctx, cpa="15.00", key="ctx-4", currency="USD")
        assert resolve_target_contexts(session, ctx)[0]["has_active_target"] is True


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


def _publish(
    session, ctx, *, cpa: str, key: str, currency: str = "USD", minimum: str | None = "100.00",
):
    preview = create_publication_preview(
        session, ctx,
        product_version_id="pv-1",
        offer_version_id="ov-1",
        market_code="US",
        currency=currency,
        target_cpa=Decimal(cpa),
        minimum_economic_spend=Decimal(minimum) if minimum is not None else None,
        idempotency_key=f"{key}-preview",
    )
    return publish_target(session, ctx, preview_id=preview.id, idempotency_key=f"{key}-publish")


def _events(session, target_id: str) -> list[tuple]:
    return [
        (e.event_type, e.previous_lifecycle_state, e.current_lifecycle_state, e.reason,
         e.semantic_event_sequence, e.revision_at_event)
        for e in session.query(ScaleTargetLifecycleEventRecord)
        .filter_by(target_version_id=target_id)
        .order_by(ScaleTargetLifecycleEventRecord.semantic_event_sequence)
    ]


def _active(session, ctx, currency: str = "USD") -> list[str]:
    return [t.id for t in list_targets(session, ctx, lifecycle_state="active") if t.currency == currency]


class TestReplaceActiveTarget:
    """#398: publishing into a context that has an active Target replaces it
    in one transaction. SQLite has no partial unique index, so the
    application check alone keeps one active Target per context."""

    def test_publishing_again_withdraws_the_previous_target(self, session, ws_id):
        ctx = _owner_context(ws_id)
        old = _publish(session, ctx, cpa="20.00", key="rep-1")
        published_at = old.published_at

        new = _publish(session, ctx, cpa="25.00", key="rep-2")

        assert (new.lifecycle_state, new.revision, new.target_cpa) == ("active", 1, Decimal("25.00"))
        assert (old.lifecycle_state, old.revision) == ("withdrawn", 2)
        assert old.withdrawn_at is not None
        assert (old.target_cpa, old.minimum_economic_spend, old.published_at) == (
            Decimal("20.00"), Decimal("100.00"), published_at,
        )
        assert _events(session, old.id) == [
            ("publish", None, "active", "publication_confirmed", 1, 1),
            ("withdraw", "active", "withdrawn", f"replaced_by:{new.id}", 2, 2),
        ]
        assert _events(session, new.id) == [("publish", None, "active", "publication_confirmed", 1, 1)]
        assert _active(session, ctx) == [new.id]

    def test_a_retry_of_a_replacement_returns_the_same_target(self, session, ws_id):
        ctx = _owner_context(ws_id)
        old = _publish(session, ctx, cpa="20.00", key="rep-3")
        preview = create_publication_preview(
            session, ctx, product_version_id="pv-1", offer_version_id="ov-1", market_code="US",
            currency="USD", target_cpa=Decimal("25.00"), idempotency_key="rep-4",
        )
        first = publish_target(session, ctx, preview_id=preview.id, idempotency_key="rep-5")
        again = publish_target(session, ctx, preview_id=preview.id, idempotency_key="rep-5")

        assert again.id == first.id
        assert len(_events(session, old.id)) == 2
        assert _active(session, ctx) == [first.id]

    def test_a_preview_made_before_another_publication_is_refused(self, session, ws_id):
        for replacing in (False, True):
            ctx = _owner_context(ws_id)
            currency = "EUR" if replacing else "USD"
            if replacing:
                _publish(session, ctx, cpa="20.00", key="rep-6", currency=currency)
            previews = [
                create_publication_preview(
                    session, ctx, product_version_id="pv-1", offer_version_id="ov-1",
                    market_code="US", currency=currency, target_cpa=Decimal(cpa),
                    idempotency_key=f"rep-7-{replacing}-{cpa}",
                )
                for cpa in ("25.00", "30.00")
            ]
            winner = publish_target(session, ctx, preview_id=previews[0].id, idempotency_key=f"rep-8-{replacing}")

            with pytest.raises(TargetLifecycleError, match="preview_hash_mismatch"):
                publish_target(session, ctx, preview_id=previews[1].id, idempotency_key=f"rep-9-{replacing}")
            assert previews[1].consumed_at is None
            assert _active(session, ctx, currency) == [winner.id]

    def test_an_enrollment_change_after_the_preview_is_refused(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = create_publication_preview(
            session, ctx, product_version_id="pv-1", offer_version_id="ov-1", market_code="US",
            currency="USD", target_cpa=Decimal("20.00"), idempotency_key="rep-10",
        )
        assert preview.affected_enrollment_count == 0
        _seed_enrollment_with_episode(session, ws_id)

        with pytest.raises(TargetLifecycleError, match="preview_hash_mismatch"):
            publish_target(session, ctx, preview_id=preview.id, idempotency_key="rep-11")
        assert _active(session, ctx) == []

    def test_another_currency_keeps_its_own_active_target(self, session, ws_id):
        ctx = _owner_context(ws_id)
        dollars = _publish(session, ctx, cpa="20.00", key="rep-12", currency="USD")
        euros = _publish(session, ctx, cpa="18.00", key="rep-13", currency="EUR")
        assert (_active(session, ctx, "USD"), _active(session, ctx, "EUR")) == ([dollars.id], [euros.id])

        replacement = _publish(session, ctx, cpa="22.00", key="rep-14", currency="USD")
        assert (_active(session, ctx, "USD"), _active(session, ctx, "EUR")) == ([replacement.id], [euros.id])
        assert len(_events(session, euros.id)) == 1

    def test_another_workspace_neither_replaces_nor_publishes_a_preview(self, session, ws_id):
        other = WorkspaceRecord(id=new_id("ws"), name="Another Workspace")
        session.add(other)
        session.flush()
        mine, theirs = _owner_context(ws_id), _owner_context(other.id)
        target = _publish(session, mine, cpa="20.00", key="rep-15")

        their_target = _publish(session, theirs, cpa="25.00", key="rep-16")
        assert (target.lifecycle_state, _active(session, theirs)) == ("active", [their_target.id])

        preview = create_publication_preview(
            session, theirs, product_version_id="pv-1", offer_version_id="ov-1", market_code="US",
            currency="USD", target_cpa=Decimal("30.00"), idempotency_key="rep-17",
        )
        with pytest.raises(TargetNotFoundError):
            publish_target(session, mine, preview_id=preview.id, idempotency_key="rep-18")
        assert preview.consumed_at is None
        assert (_active(session, mine), _active(session, theirs)) == ([target.id], [their_target.id])

    def test_a_member_cannot_publish_a_replacement(self, session, ws_id):
        ctx = _owner_context(ws_id)
        target = _publish(session, ctx, cpa="20.00", key="rep-19")
        preview = create_publication_preview(
            session, ctx, product_version_id="pv-1", offer_version_id="ov-1", market_code="US",
            currency="USD", target_cpa=Decimal("25.00"), idempotency_key="rep-20",
        )
        with pytest.raises(TargetLifecycleError, match="owner_required"):
            publish_target(session, _member_context(ws_id), preview_id=preview.id, idempotency_key="rep-21")
        assert (target.lifecycle_state, preview.consumed_at) == ("active", None)

    def test_a_replaced_target_is_not_withdrawable(self, session, ws_id):
        ctx = _owner_context(ws_id)
        old = _publish(session, ctx, cpa="20.00", key="rep-22")
        new = _publish(session, ctx, cpa="25.00", key="rep-23")

        assert [a["allowed"] for a in target_actions(old, ctx)] == [False]
        with pytest.raises(TargetLifecycleError, match="invalid_lifecycle"):
            create_withdrawal_preview(session, ctx, target_id=old.id, reason="again", idempotency_key="rep-24")
        preview = create_withdrawal_preview(
            session, ctx, target_id=new.id, reason="done", idempotency_key="rep-25",
        )
        assert withdraw_target(session, ctx, preview_id=preview.id, idempotency_key="rep-26").lifecycle_state == (
            "withdrawn"
        )
        assert _active(session, ctx) == []

    def test_a_local_database_with_two_active_targets_converges_to_one(self, session, ws_id):
        """A SQLite database written before #398 may hold two active Targets
        in one context; the next publication replaces both."""
        ctx = _owner_context(ws_id)
        first = _publish(session, ctx, cpa="20.00", key="rep-27")
        duplicate = ScaleTargetVersionRecord(
            id=new_id("stgt"), workspace_id=ws_id, product_version_id="pv-1", offer_version_id="ov-1",
            market_code="US", currency="USD", target_cpa=Decimal("21.00"), lifecycle_state="active",
            creator_id="user-owner", revision=1, published_at=utcnow(),
        )
        session.add(duplicate)
        session.flush()

        new = _publish(session, ctx, cpa="25.00", key="rep-28")
        assert _active(session, ctx) == [new.id]
        for old in (first, duplicate):
            assert _events(session, old.id)[-1][:4] == (
                "withdraw", "active", "withdrawn", f"replaced_by:{new.id}",
            )


def _seed_enrollment(
    session, ws_id: str, *, meta_mode: str, scenario: str, market_code: str = "US",
    created_at: datetime | None = None,
) -> None:
    """A bare Enrollment row in one mode and scenario (no Episode, no
    lineage): all the Target commands read from it is the scenario."""
    session.add(ScaleEnrollmentRecord(
        id=new_id("senr"), workspace_id=ws_id, meta_mode=meta_mode, meta_connection_id="conn-1",
        scenario_scope_key=scenario, ad_account_id="act-1", winner_decision_id="wdec-1",
        winner_confirmation_id="wconf-1", asset_version_id="asset-1", product_version_id="pv-1",
        offer_version_id="ov-1", market_code=market_code, remote_binding_key=new_id("binding"),
        lifecycle="active", creator_id="user-owner", original_initiator_id="user-owner",
        created_at=created_at or utcnow(),
    ))
    session.flush()


def _journal(session, ws_id: str) -> dict[str, tuple[str, str]]:
    return {
        row.command_type: (row.meta_mode, row.scenario_scope_key)
        for row in session.query(ScaleCommandJournalRecord).filter_by(workspace_id=ws_id)
    }


def _publish_and_withdraw(session, ctx) -> None:
    # CPA only: no Policy permits controlled_mock, so a minimum economic
    # spend has no floor to be checked against there (#134).
    target = _publish(session, ctx, cpa="20.00", key="scope", minimum=None)
    preview = create_withdrawal_preview(
        session, ctx, target_id=target.id, reason="scope", idempotency_key="scope-withdrawal-preview",
    )
    withdraw_target(session, ctx, preview_id=preview.id, idempotency_key="scope-withdraw")


_TARGET_COMMANDS = (
    "create_target_publication_preview", "publish_scale_target",
    "create_target_withdrawal_preview", "withdraw_scale_target",
)


class TestTargetCommandScenarioScope:
    """#56: ``claim_command`` requires an explicit scenario under
    ``controlled_mock``. A Target has none of its own, so its commands take
    the scenario of the context's Enrollment in that mode; every other mode
    keeps the no-scenario marker and needs no Enrollment."""

    @pytest.mark.parametrize("meta_mode", ["customer_sandbox", "internal_validation"])
    def test_other_modes_journal_no_scenario_and_need_no_enrollment(self, session, ws_id, meta_mode):
        ctx = _mode_owner(ws_id, meta_mode)

        _publish_and_withdraw(session, ctx)

        assert _journal(session, ws_id) == {
            command: (meta_mode, "__no_scenario__") for command in _TARGET_COMMANDS
        }

    def test_controlled_mock_journals_the_contexts_enrollment_scenario(self, session, ws_id):
        _seed_enrollment(session, ws_id, meta_mode="controlled_mock", scenario="scn-1")
        ctx = _mode_owner(ws_id, "controlled_mock")

        _publish_and_withdraw(session, ctx)

        assert _journal(session, ws_id) == {
            command: ("controlled_mock", "scn-1") for command in _TARGET_COMMANDS
        }

    def test_the_most_recent_enrollment_in_this_mode_and_context_names_the_scenario(self, session, ws_id):
        now = utcnow()
        _seed_enrollment(session, ws_id, meta_mode="controlled_mock", scenario="scn-old",
                         created_at=now - timedelta(days=2))
        _seed_enrollment(session, ws_id, meta_mode="controlled_mock", scenario="scn-new",
                         created_at=now - timedelta(days=1))
        _seed_enrollment(session, ws_id, meta_mode="controlled_mock", scenario="scn-other-market",
                         market_code="DE", created_at=now)
        _seed_enrollment(session, ws_id, meta_mode="customer_sandbox", scenario="__no_scenario__",
                         created_at=now)
        ctx = _mode_owner(ws_id, "controlled_mock")

        _publish(session, ctx, cpa="20.00", key="recent", minimum=None)

        assert _journal(session, ws_id) == {
            "create_target_publication_preview": ("controlled_mock", "scn-new"),
            "publish_scale_target": ("controlled_mock", "scn-new"),
        }

    def test_controlled_mock_refuses_a_context_with_no_enrollment_in_that_mode(self, session, ws_id):
        _seed_enrollment(session, ws_id, meta_mode="customer_sandbox", scenario="__no_scenario__")
        ctx = _mode_owner(ws_id, "controlled_mock")

        with pytest.raises(TargetLifecycleError) as refused:
            _publish(session, ctx, cpa="20.00", key="no-enrollment", minimum=None)

        assert refused.value.code == "no_controlled_mock_enrollment"
        assert _journal(session, ws_id) == {}

    def test_controlled_mock_withdraws_a_target_published_in_another_mode(self, session, ws_id):
        """Targets are Workspace-wide: one published in Sandbox is withdrawn
        in controlled_mock under that mode's Enrollment scenario."""
        sandbox_target = _publish(session, _owner_context(ws_id), cpa="20.00", key="cross-mode")
        _seed_enrollment(session, ws_id, meta_mode="controlled_mock", scenario="scn-1")
        ctx = _mode_owner(ws_id, "controlled_mock")

        preview = create_withdrawal_preview(
            session, ctx, target_id=sandbox_target.id, reason="cross-mode",
            idempotency_key="cross-mode-withdrawal-preview",
        )
        withdraw_target(session, ctx, preview_id=preview.id, idempotency_key="cross-mode-withdraw")

        assert _journal(session, ws_id)["withdraw_scale_target"] == ("controlled_mock", "scn-1")
        assert get_target(session, ctx, sandbox_target.id).lifecycle_state == "withdrawn"


# ---------------------------------------------------------------------------
# #134: the minimum economic spend against the platform floor (§9.2)
# ---------------------------------------------------------------------------


def _preview(session, ctx, *, key: str, currency: str = "USD", cpa: str | None = None,
             roas: str | None = None, minimum: str | None = None):
    return create_publication_preview(
        session, ctx,
        product_version_id="pv-1",
        offer_version_id="ov-1",
        market_code="US",
        currency=currency,
        target_cpa=Decimal(cpa) if cpa is not None else None,
        breakeven_roas=Decimal(roas) if roas is not None else None,
        minimum_economic_spend=Decimal(minimum) if minimum is not None else None,
        idempotency_key=key,
    )


def _refused(session, ctx, **amounts) -> TargetLifecycleError:
    with pytest.raises(TargetLifecycleError) as refused:
        _preview(session, ctx, key=f"refused-{new_id('k')}", **amounts)
    return refused.value


def _publish_policy(session, *, floors: dict[str, str]) -> ScalePolicyVersionRecord:
    """A Policy published after v2 with its own floor table, so the one in
    force is no longer the code constant's."""
    v2 = PUBLISHED_POLICY_VERSIONS[-1].row()
    policy = ScalePolicyVersionRecord(**{
        **v2,
        "id": "spol_test_134_raised_floor",
        "signal_thresholds": {
            **v2["signal_thresholds"],
            "economic_spend_floor_per_7d_window_by_currency": floors,
        },
        "created_at": v2["published_at"] + timedelta(days=1),
        "published_at": v2["published_at"] + timedelta(days=1),
    })
    session.add(policy)
    session.flush()
    return policy


def _legacy_target(session, ws_id: str, *, currency: str = "USD", cpa: str | None = None,
                   roas: str | None = None, minimum: str | None = None,
                   lifecycle_state: str = "active") -> ScaleTargetVersionRecord:
    """A Target as published before #134, written directly (no check)."""
    target = ScaleTargetVersionRecord(
        id=new_id("stgt"), workspace_id=ws_id, product_version_id="pv-1", offer_version_id="ov-1",
        market_code="US", currency=currency,
        target_cpa=Decimal(cpa) if cpa is not None else None,
        breakeven_roas=Decimal(roas) if roas is not None else None,
        minimum_economic_spend=Decimal(minimum) if minimum is not None else None,
        lifecycle_state=lifecycle_state, creator_id="user-owner", revision=1, published_at=utcnow(),
    )
    session.add(target)
    session.flush()
    return target


class TestMinimumEconomicSpendFloor:
    """#134: a ROAS-only Target fills a minimum economic spend, and a
    filled minimum may not go below the floor the Policy in force sets for
    the Target's currency (v2: USD 100, JPY 16000, ...)."""

    def test_a_roas_only_target_without_a_minimum_is_refused(self, session, ws_id):
        refused = _refused(session, _owner_context(ws_id), roas="2.0")

        assert refused.code == "minimum_economic_spend_required"
        assert refused.params == {"currency": "USD", "floor": "100"}
        # The request's transaction also drops its journal claim
        # (``tests/test_scale_router.py``).
        assert session.query(ScaleTargetPublicationPreviewRecord).count() == 0

    def test_a_zero_minimum_is_no_minimum(self, session, ws_id):
        refused = _refused(session, _owner_context(ws_id), roas="2.0", minimum="0")

        assert refused.code == "minimum_economic_spend_required"

    @pytest.mark.parametrize("amounts", [
        {"roas": "2.0", "minimum": "99.99"},
        {"cpa": "20", "minimum": "50"},
        {"cpa": "20", "roas": "2.0", "minimum": "99.999999999999"},
    ])
    def test_a_minimum_below_the_floor_is_refused(self, session, ws_id, amounts):
        refused = _refused(session, _owner_context(ws_id), **amounts)

        assert refused.code == "minimum_economic_spend_below_platform_floor"
        assert refused.params == {"currency": "USD", "floor": "100"}

    @pytest.mark.parametrize("amounts", [
        {"roas": "2.0", "minimum": "100"},
        {"roas": "2.0", "minimum": "100.000"},
        {"cpa": "20", "minimum": "100"},
        {"cpa": "20", "roas": "2.0", "minimum": "250"},
    ])
    def test_a_minimum_equal_to_or_above_the_floor_publishes(self, session, ws_id, amounts):
        ctx = _owner_context(ws_id)
        preview = _preview(session, ctx, key="at-floor", **amounts)
        assert preview.policy_floor_check_passed is True

        target = publish_target(session, ctx, preview_id=preview.id, idempotency_key="at-floor-publish")

        assert target.lifecycle_state == "active"
        assert target.minimum_economic_spend == Decimal(amounts["minimum"])

    def test_a_cpa_target_without_a_minimum_needs_no_floor(self, session, ws_id):
        ctx = _owner_context(ws_id)
        for currency in ("USD", "BRL"):
            preview = _preview(session, ctx, key=f"cpa-{currency}", currency=currency, cpa="20")
            publish_target(session, ctx, preview_id=preview.id, idempotency_key=f"cpa-{currency}-publish")

    def test_the_floor_is_the_targets_currency_entry(self, session, ws_id):
        """v2 keeps 100 in its old single-floor columns; a JPY minimum is
        held to the table's 16000, not to those."""
        ctx = _owner_context(ws_id)
        refused = _refused(session, ctx, currency="JPY", roas="2.0", minimum="150")
        assert (refused.code, refused.params) == (
            "minimum_economic_spend_below_platform_floor", {"currency": "JPY", "floor": "16000"},
        )

        _preview(session, ctx, key="jpy-at-floor", currency="JPY", roas="2.0", minimum="16000")
        refused = _refused(session, ctx, currency="EUR", roas="2.0", minimum="89.99")
        assert refused.params == {"currency": "EUR", "floor": "90"}

    @pytest.mark.parametrize("amounts", [
        {"roas": "2.0"},
        {"roas": "2.0", "minimum": "1000"},
        {"cpa": "20", "minimum": "1000"},
    ])
    def test_a_currency_outside_the_policy_table_is_refused_with_its_own_reason(
        self, session, ws_id, amounts,
    ):
        refused = _refused(session, _owner_context(ws_id), currency="BRL", **amounts)

        assert (refused.code, refused.params) == ("spend_floor_currency_unsupported", {"currency": "BRL"})

    def test_a_mode_no_policy_permits_has_no_floor_to_check_against(self, session, ws_id):
        """controlled_mock: no live run, so no Policy in force. A CPA alone
        still publishes; anything that needs the floor is refused."""
        _seed_enrollment(session, ws_id, meta_mode="controlled_mock", scenario="scn-1")
        ctx = _mode_owner(ws_id, "controlled_mock")

        refused = _refused(session, ctx, roas="2.0", minimum="100")
        assert (refused.code, refused.params) == ("no_eligible_policy", {"currency": "USD"})
        assert _publish(session, ctx, cpa="20.00", key="mock-cpa", minimum=None).lifecycle_state == "active"

    def test_the_floor_comes_from_the_policy_in_force(self, session, ws_id):
        ctx = _owner_context(ws_id)
        _publish_policy(session, floors={"USD": "200"})

        refused = _refused(session, ctx, roas="2.0", minimum="150")

        assert refused.params == {"currency": "USD", "floor": "200"}

    def test_publish_checks_again_under_the_policy_in_force_now(self, session, ws_id):
        ctx = _owner_context(ws_id)
        preview = _preview(session, ctx, key="before-raise", roas="2.0", minimum="150")
        _publish_policy(session, floors={"USD": "200"})

        with pytest.raises(TargetLifecycleError) as refused:
            publish_target(session, ctx, preview_id=preview.id, idempotency_key="after-raise")

        assert (refused.value.code, refused.value.params) == (
            "minimum_economic_spend_below_platform_floor", {"currency": "USD", "floor": "200"},
        )

    def test_a_retried_preview_is_returned_whatever_the_policy_says_now(self, session, ws_id):
        ctx = _owner_context(ws_id)
        first = _preview(session, ctx, key="retry-134", roas="2.0", minimum="150")
        _publish_policy(session, floors={"USD": "200"})

        assert _preview(session, ctx, key="retry-134", roas="2.0", minimum="150").id == first.id

    def test_a_member_is_refused_by_role_before_the_floor(self, session, ws_id):
        refused = _refused(session, _member_context(ws_id), roas="2.0", minimum="1")

        assert refused.code == "owner_required"

    def test_a_member_cannot_publish_an_owners_valid_preview(self, session, ws_id):
        preview = _preview(session, _owner_context(ws_id), key="owner-valid", roas="2.0", minimum="100")

        with pytest.raises(TargetLifecycleError, match="owner_required"):
            publish_target(session, _member_context(ws_id), preview_id=preview.id, idempotency_key="m")

    def test_another_workspace_cannot_publish_a_preview_at_the_floor(self, session, ws_id):
        preview = _preview(session, _owner_context(ws_id), key="ws-a", roas="2.0", minimum="100")
        other = WorkspaceRecord(id=new_id("ws"), name="Other Workspace")
        session.add(other)
        session.flush()

        with pytest.raises(TargetNotFoundError):
            publish_target(session, _owner_context(other.id), preview_id=preview.id, idempotency_key="ws-b")


class TestPlatformFloorOfPublishedTargets:
    """#134: Targets published before the check stay in force; the server
    says which of them fall below the floor in force."""

    def _floor(self, session, ctx, currency: str = "USD"):
        return platform_spend_floor(applicable_policy(session, ctx), currency)

    def test_the_floor_in_force_per_currency(self, session, ws_id):
        ctx = _owner_context(ws_id)

        usd, brl = self._floor(session, ctx), self._floor(session, ctx, "BRL")

        assert (usd.amount, usd.policy_version_id, usd.unavailable_reason) == (
            Decimal("100"), "spol_beta_default_v2", None,
        )
        assert (brl.amount, brl.unavailable_reason) == (None, "spend_floor_currency_unsupported")
        mock = self._floor(session, _mode_owner(ws_id, "controlled_mock"))
        assert (mock.amount, mock.policy_version_id, mock.unavailable_reason) == (
            None, None, "no_eligible_policy",
        )

    @pytest.mark.parametrize(("amounts", "below"), [
        ({"roas": "2.0"}, True),
        ({"roas": "2.0", "minimum": "0"}, True),
        ({"roas": "2.0", "minimum": "10"}, True),
        ({"cpa": "20", "minimum": "50"}, True),
        ({"roas": "2.0", "minimum": "100"}, False),
        ({"cpa": "20"}, False),
        ({"cpa": "20", "minimum": "0"}, False),
    ])
    def test_an_active_target_that_misses_the_check_is_below_the_floor(
        self, session, ws_id, amounts, below,
    ):
        target = _legacy_target(session, ws_id, **amounts)

        assert target_below_platform_floor(target, self._floor(session, _owner_context(ws_id))) is below

    def test_a_target_not_in_force_or_without_a_floor_is_never_below_it(self, session, ws_id):
        ctx = _owner_context(ws_id)
        withdrawn = _legacy_target(session, ws_id, roas="2.0", minimum="10", lifecycle_state="withdrawn")
        brl = _legacy_target(session, ws_id, currency="BRL", roas="2.0", minimum="10")

        assert target_below_platform_floor(withdrawn, self._floor(session, ctx)) is False
        assert target_below_platform_floor(brl, self._floor(session, ctx, "BRL")) is False

    def test_a_published_target_below_the_floor_is_not_rewritten(self, session, ws_id):
        """Nothing reads a Target to change it: the stored version stays."""
        target = _legacy_target(session, ws_id, roas="2.0", minimum="10")
        ctx = _owner_context(ws_id)

        listed = list_targets(session, ctx, lifecycle_state="active")

        assert [t.id for t in listed] == [target.id]
        assert (listed[0].minimum_economic_spend, listed[0].revision) == (Decimal("10"), 1)

    def test_contexts_report_the_floor_or_why_there_is_none(self, session, ws_id):
        _seed_enrollment_with_episode(session, ws_id, currency="JPY")
        floor = resolve_target_contexts(session, _owner_context(ws_id))[0]
        assert (floor["platform_spend_floor"], floor["platform_spend_floor_unavailable_reason"]) == (
            "16000", None,
        )

        mock = resolve_target_contexts(session, _mode_owner(ws_id, "controlled_mock"))[0]
        assert (mock["platform_spend_floor"], mock["platform_spend_floor_unavailable_reason"]) == (
            None, "no_eligible_policy",
        )
