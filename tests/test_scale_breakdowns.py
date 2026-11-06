"""Snapshot-pinned breakdown query with privacy suppression and pagination.

Issue #87 Task 3. Flat scratch-SQLite style (mirroring
``tests/test_scale_targets.py``): a fresh temp-file SQLite database per test
via pytest fixtures, with the shared Engine-level ``PRAGMA foreign_keys=ON``
listener enforcing every FK once ``app.adapters.persistence.database`` is
imported (it is, transitively, here).
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.platform.pagination import InvalidCursorError
from app.platform.tenancy import TenantContext
from app.scale.breakdowns import (
    BreakdownNotAvailableError,
    query_snapshot_breakdowns,
)
from app.scale.models import (
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleSnapshotBreakdownDailyRecord,
    ScaleSnapshotBreakdownSegmentRecord,
)


@pytest.fixture
def session():
    tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-sbrk-"))
    engine = create_engine(
        f"sqlite:///{(tmp_dir / 'sbrk.db').as_posix()}",
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


def _seed_sealed_snapshot(
    session: Session, ws_id: str, episode_id: str, *, claim_suffix: str = "1",
) -> str:
    now = datetime.now(timezone.utc)
    snapshot = ScaleEvidenceSnapshotRecord(
        id=new_id("sesn"),
        workspace_id=ws_id,
        meta_mode="customer_sandbox",
        meta_connection_id="conn-1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act-1",
        episode_id=episode_id,
        remote_binding_key="binding-1",
        claim_hash=f"claim-{claim_suffix}",
        source_manifest_id="manifest-1",
        source_manifest_hash="manifest-hash-1",
        source_revision=1,
        safe_watermark=date(2026, 9, 10),
        policy_version_id="policy-1",
        target_version_id=None,
        normalized_target_key="__no_target__",
        algorithm_version="v1",
        canonical_schema_version="v1",
        metric_semantic_contract_set_id="mscs-1",
        metric_semantic_contract_set_hash="mscs-hash-1",
        delivery_config_source_id="delivery-src-1",
        delivery_evidence_hash="delivery-hash-1",
        delivery_comparability_hash="delivery-comp-1",
        timezone_name="UTC",
        currency="USD",
        attribution_semantics_hash="attr-hash-1",
        current_window_start_local_day=date(2026, 9, 3),
        current_window_end_local_day=date(2026, 9, 10),
        baseline_window_start_local_day=date(2026, 8, 27),
        baseline_window_end_local_day=date(2026, 9, 3),
        rolling_window_start_local_day=date(2026, 8, 27),
        rolling_window_end_local_day=date(2026, 9, 10),
        complete_local_days_in_episode=7,
        active_delivery_days_in_episode=7,
        data_continuity_generation="gen-1",
        prior_evaluation_id=None,
        prior_evaluated_local_day=None,
        prior_streak_compat_policy_version_id=None,
        prior_streak_compat_target_version_id=None,
        prior_streak_compat_metric_contract_set_hash=None,
        prior_streak_compat_configuration_baseline_hash=None,
        prior_streak_compat_data_continuity_generation=None,
        prior_streak_compat_canonical_hash=None,
        prior_health=None,
        prior_healthy_evaluations=None,
        prior_streak_counters=None,
        prior_streak_absence_reason="no_earlier_distinct_day",
        last_complete_local_day=date(2026, 9, 10),
        last_sync_at=now,
        freshness_reference_at=now,
        as_of_local_day=date(2026, 9, 10),
        canonical_hash=f"canon-{claim_suffix}",
        sealed_at=now,
    )
    session.add(snapshot)
    session.flush()
    return snapshot.id


def _add_segment(
    session: Session,
    ws_id: str,
    snapshot_id: str,
    *,
    breakdown_type: str,
    segment_key: str,
    state: str = "observed",
    suppression_reason: str | None = None,
) -> str:
    seg = ScaleSnapshotBreakdownSegmentRecord(
        id=new_id("ssbs"),
        workspace_id=ws_id,
        meta_mode="customer_sandbox",
        snapshot_id=snapshot_id,
        breakdown_type=breakdown_type,
        segment_key=segment_key,
        state=state,
        suppression_reason=suppression_reason,
        source_record_id="source-1",
    )
    session.add(seg)
    session.flush()
    return seg.id


def _add_daily(
    session: Session,
    ws_id: str,
    segment_id: str,
    local_day: date,
    *,
    spend: str,
    impressions: int,
    link_clicks: int,
    purchases: int,
    purchase_value: str,
) -> None:
    session.add(
        ScaleSnapshotBreakdownDailyRecord(
            id=new_id("ssbd"),
            workspace_id=ws_id,
            meta_mode="customer_sandbox",
            breakdown_segment_id=segment_id,
            local_day=local_day,
            state="present",
            delivered=True,
            spend=Decimal(spend),
            impressions=impressions,
            link_clicks=link_clicks,
            purchases=purchases,
            purchase_value=Decimal(purchase_value),
        )
    )
    session.flush()


@pytest.fixture
def seeded_snapshot(session):
    ws = WorkspaceRecord(id=new_id("ws"), name="Breakdown query test")
    session.add(ws)
    session.flush()

    enrollment_id, episode_id = _seed_enrollment_and_episode(session, ws.id)
    snapshot_id = _seed_sealed_snapshot(session, ws.id, episode_id)

    # -- placement: two observed segments (with daily evidence) plus one
    # privacy-suppressed placeholder.
    seg_feed = _add_segment(
        session, ws.id, snapshot_id, breakdown_type="placement", segment_key="facebook_feed",
    )
    _add_daily(
        session, ws.id, seg_feed, date(2026, 9, 9),
        spend="100.00", impressions=1000, link_clicks=50, purchases=5, purchase_value="250.00",
    )
    _add_daily(
        session, ws.id, seg_feed, date(2026, 9, 10),
        spend="50.00", impressions=500, link_clicks=20, purchases=2, purchase_value="80.00",
    )

    seg_stories = _add_segment(
        session, ws.id, snapshot_id, breakdown_type="placement", segment_key="instagram_stories",
    )
    _add_daily(
        session, ws.id, seg_stories, date(2026, 9, 9),
        spend="30.00", impressions=300, link_clicks=10, purchases=1, purchase_value="40.00",
    )

    _add_segment(
        session, ws.id, snapshot_id,
        breakdown_type="placement", segment_key="audience_network",
        state="privacy_suppressed", suppression_reason="api_privacy_threshold",
    )

    # -- age_gender: one observed segment with metrics that would otherwise
    # support CPA/ROAS, to prove they're withheld regardless.
    seg_age = _add_segment(
        session, ws.id, snapshot_id, breakdown_type="age_gender", segment_key="25-34_female",
    )
    _add_daily(
        session, ws.id, seg_age, date(2026, 9, 9),
        spend="20.00", impressions=200, link_clicks=5, purchases=3, purchase_value="90.00",
    )

    session.commit()

    return SimpleNamespace(
        context=_owner_context(ws.id),
        workspace_id=ws.id,
        enrollment_id=enrollment_id,
        episode_id=episode_id,
        snapshot_id=snapshot_id,
    )


class TestQuerySnapshotBreakdowns:
    def test_returns_observed_segments_with_aggregated_metrics(self, session, seeded_snapshot):
        page = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
        )
        assert page.snapshot_id == seeded_snapshot.snapshot_id
        assert page.episode_id == seeded_snapshot.episode_id
        assert page.breakdown_type == "placement"
        assert page.window_start == date(2026, 9, 3)
        assert page.window_end == date(2026, 9, 10)
        assert page.safe_watermark == date(2026, 9, 10)
        assert page.policy_version_id == "policy-1"
        assert page.target_version_id is None

        observed = [s for s in page.segments if s.state == "observed"]
        assert len(observed) == 2
        for seg in observed:
            assert seg.spend_share is not None
            assert seg.impressions is not None

        feed = next(s for s in observed if s.segment_key == "facebook_feed")
        assert feed.impressions == 1500
        assert feed.link_clicks == 70
        assert feed.purchases == 7
        assert feed.purchase_value == "330.000000"
        # feed spend 150 of (150 + 30) total observed spend = 0.833333...
        assert feed.spend_share == "0.833333"
        assert feed.cpa == str((Decimal("150.00") / 7).quantize(Decimal("0.000001")))
        assert feed.roas == str((Decimal("330.00") / Decimal("150.00")).quantize(Decimal("0.000001")))

    def test_privacy_suppressed_segments_have_no_metrics(self, session, seeded_snapshot):
        page = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
        )
        suppressed = [s for s in page.segments if s.state == "privacy_suppressed"]
        assert len(suppressed) == 1
        for seg in suppressed:
            assert seg.suppression_reason == "api_privacy_threshold"
            assert seg.impressions is None
            assert seg.spend_share is None
            assert seg.link_clicks is None
            assert seg.purchases is None
            assert seg.purchase_value is None
            assert seg.cpa is None
            assert seg.roas is None

    def test_age_gender_shows_only_descriptive_shares(self, session, seeded_snapshot):
        page = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="age_gender",
        )
        assert len(page.segments) == 1
        for seg in page.segments:
            if seg.state == "observed":
                assert seg.spend_share is not None
                assert seg.cpa is None
                assert seg.roas is None

    def test_rejects_unknown_breakdown_type(self, session, seeded_snapshot):
        with pytest.raises(BreakdownNotAvailableError, match="invalid_breakdown_type"):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=seeded_snapshot.snapshot_id,
                breakdown_type="invalid",
            )

    def test_paginates_segments(self, session, seeded_snapshot):
        page1 = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
            limit=1,
        )
        assert len(page1.segments) == 1
        assert page1.next_cursor is not None

        page2 = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
            cursor=page1.next_cursor,
            limit=1,
        )
        assert len(page2.segments) == 1
        assert page2.segments[0].segment_key != page1.segments[0].segment_key

        page3 = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
            cursor=page2.next_cursor,
            limit=1,
        )
        assert len(page3.segments) == 1
        assert page3.next_cursor is None

        all_keys = {page1.segments[0].segment_key, page2.segments[0].segment_key, page3.segments[0].segment_key}
        assert all_keys == {"audience_network", "facebook_feed", "instagram_stories"}

    def test_cursor_minted_for_one_breakdown_type_rejected_for_another(self, session, seeded_snapshot):
        page1 = query_snapshot_breakdowns(
            session, seeded_snapshot.context,
            enrollment_id=seeded_snapshot.enrollment_id,
            snapshot_id=seeded_snapshot.snapshot_id,
            breakdown_type="placement",
            limit=1,
        )
        with pytest.raises(InvalidCursorError):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=seeded_snapshot.snapshot_id,
                breakdown_type="age_gender",
                cursor=page1.next_cursor,
            )

    def test_rejects_enrollment_not_found(self, session, seeded_snapshot):
        with pytest.raises(BreakdownNotAvailableError, match="enrollment_not_found"):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id="senr_does-not-exist",
                snapshot_id=seeded_snapshot.snapshot_id,
                breakdown_type="placement",
            )

    def test_rejects_snapshot_not_found(self, session, seeded_snapshot):
        with pytest.raises(BreakdownNotAvailableError, match="snapshot_not_found"):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id="sesn_does-not-exist",
                breakdown_type="placement",
            )

    def test_rejects_unsealed_snapshot(self, session, seeded_snapshot):
        unsealed = _seed_sealed_snapshot(
            session, seeded_snapshot.workspace_id, seeded_snapshot.episode_id, claim_suffix="2",
        )
        row = session.get(ScaleEvidenceSnapshotRecord, unsealed)
        row.sealed_at = None
        row.canonical_hash = None
        session.flush()

        with pytest.raises(BreakdownNotAvailableError, match="snapshot_not_sealed"):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=unsealed,
                breakdown_type="placement",
            )

    def test_rejects_snapshot_from_a_different_enrollment(self, session, seeded_snapshot):
        other_enrollment_id, other_episode_id = _seed_enrollment_and_episode(
            session, seeded_snapshot.workspace_id,
        )
        other_snapshot_id = _seed_sealed_snapshot(
            session, seeded_snapshot.workspace_id, other_episode_id, claim_suffix="other",
        )

        with pytest.raises(BreakdownNotAvailableError, match="snapshot_not_found"):
            query_snapshot_breakdowns(
                session, seeded_snapshot.context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=other_snapshot_id,
                breakdown_type="placement",
            )

    def test_rejects_cross_workspace_enrollment(self, session, seeded_snapshot):
        other_ws = WorkspaceRecord(id=new_id("ws"), name="Other workspace")
        session.add(other_ws)
        session.flush()
        other_context = _owner_context(other_ws.id)

        with pytest.raises(BreakdownNotAvailableError, match="enrollment_not_found"):
            query_snapshot_breakdowns(
                session, other_context,
                enrollment_id=seeded_snapshot.enrollment_id,
                snapshot_id=seeded_snapshot.snapshot_id,
                breakdown_type="placement",
            )
