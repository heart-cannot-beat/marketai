"""Tests for evidence sealing and assembly (Issue #25, Task 10).

Spec: docs/superpowers/specs/2026-09-08-scale-section-3-evidence-advice-orchestration.md §3.3
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-10-brief.md
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.platform.tenancy import TenantContext
from app.scale._contracts import ScaleEvidenceSnapshot
from app.scale.evidence import (
    SnapshotAssemblyError,
    SnapshotSealingError,
    assemble_snapshot,
    seal_snapshot,
)
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleBreakdownDailyChildRecord,
    ScaleBreakdownPrivacyRecord,
    ScaleBreakdownSourceRecord,
    ScaleDailyMetricSourceRecord,
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceLinkRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleExactWindowSourceRecord,
    ScaleManifestItemRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
    ScalePolicyVersionRecord,
    ScaleSnapshotBreakdownDailyRecord,
    ScaleSnapshotBreakdownSegmentRecord,
    ScaleSnapshotQualityIssueRecord,
    ScaleSourceQualityIssueRecord,
    ScaleTargetVersionRecord,
)

AS_OF = date(2026, 9, 5)
CURRENT_START = AS_OF - timedelta(days=6)
CURRENT_END = AS_OF + timedelta(days=1)
BASELINE_START = CURRENT_START - timedelta(days=7)
BASELINE_END = CURRENT_START


# ---------------------------------------------------------------------------
# Fixture builders (mirror the pattern in tests/test_scale_migration.py)
# ---------------------------------------------------------------------------


def _daily_source(ws, mode, episode_id, local_day, *, revision=1):
    return ScaleDailyMetricSourceRecord(
        id=new_id("sdms"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act_1",
        episode_id=episode_id,
        source_revision=revision,
        contract_generation_id="gen-1",
        local_day=local_day,
        state="present",
        delivered=True,
        spend=Decimal("20"),
        impressions=2000,
        link_clicks=200,
        purchases=1,
        purchase_value=Decimal("60"),
        currency="USD",
        timezone_name="UTC",
        raw_response_hash="d" * 64,
        sync_run_id="sync_1",
        occurrence_id="occ_daily",
        call_intent_id="call_daily",
        observed_at=datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc),
    )


def _exact_source(ws, mode, episode_id, role, start, end, *, revision=1):
    return ScaleExactWindowSourceRecord(
        id=new_id("sews"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act_1",
        episode_id=episode_id,
        source_revision=revision,
        contract_generation_id="gen-1",
        window_role=role,
        window_start_local_day=start,
        window_end_local_day=end,
        reach=1000,
        frequency=Decimal("1.5"),
        spend=Decimal("140"),
        impressions=14000,
        link_clicks=1400,
        purchases=7,
        purchase_value=Decimal("420"),
        currency="USD",
        timezone_name="UTC",
        raw_response_hash="e" * 64,
        sync_run_id="sync_1",
        occurrence_id=f"occ_exact_{role}",
        call_intent_id=f"call_exact_{role}",
        observed_at=datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc),
    )


def _delivery_source(ws, mode, episode_id, *, revision=1):
    return ScaleDeliveryConfigSourceRecord(
        id=new_id("sdcs"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act_1",
        episode_id=episode_id,
        source_revision=revision,
        contract_generation_id="gen-1",
        ad_creative_id="creative_1",
        campaign_remote_id="camp_1",
        ad_set_remote_id="adset_1",
        ad_remote_id="ad_1",
        campaign_status="ACTIVE",
        ad_set_status="ACTIVE",
        ad_status="ACTIVE",
        budget_source="ad_set",
        budget_amount=Decimal("50"),
        currency="USD",
        optimization_goal="OFFSITE_CONVERSIONS",
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        attribution_setting="7d_click_1d_view",
        targeting_safety_hash="t" * 64,
        evidence_hash="v" * 64,
        comparability_hash="c" * 64,
        raw_response_hash="r" * 64,
        sync_run_id="sync_1",
        occurrence_id="occ_delivery",
        call_intent_id="call_delivery",
        observed_at=datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc),
    )


def _item(ws, mode, manifest_id, contract_type, source_record_id, **overrides):
    fields = dict(
        id=new_id("smi"),
        workspace_id=ws,
        meta_mode=mode,
        manifest_id=manifest_id,
        contract_type=contract_type,
        window_role=None,
        local_day=None,
        segment_key=None,
        state="fulfilled",
        source_record_id=source_record_id,
    )
    fields.update(overrides)
    return ScaleManifestItemRecord(**fields)


def _manifest(ws, mode, episode_id, as_of, *, revision=1, generation=1, state="complete"):
    return ScaleEvidenceSourceManifestRecord(
        id=new_id("sesm"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        episode_id=episode_id,
        target_local_day=as_of,
        source_revision=revision,
        source_generation=generation,
        state=state,
        contract_request_set_hash="q" * 64,
        last_complete_local_day=(as_of if state == "complete" else None),
        safe_watermark=as_of,
        terminal_at=datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc),
        canonical_hash="m" * 64,
    )


def _run(ws, mode, episode_id, enrollment_id, manifest, as_of, *, policy_id, trigger_kind="live", revision=1):
    return ScaleEvaluationRunRecord(
        id=new_id("sevr"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        episode_id=episode_id,
        enrollment_id=enrollment_id,
        target_local_day=as_of,
        trigger_kind=trigger_kind,
        source_manifest_id=manifest.id,
        source_manifest_hash=manifest.canonical_hash,
        source_revision=revision,
        freshness_reference_at=datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc),
        policy_version_id=policy_id,
        target_version_id=None,
        normalized_target_key=NO_TARGET_MARKER,
        algorithm_version="beta-v1",
        semantic_event_sequence=0,
        replay_generation=0,
        original_initiator_id="user_1",
        run_state="requested",
    )


_SIGNAL_THRESHOLDS = {
    "confidence_level": "0.95",
    "confidence_z": "1.96",
    "frequency_level_threshold": "3",
    "frequency_growth_threshold": "0.2",
    "ctr_decline_threshold": "0.2",
    "ctr_stability_tolerance": "0.05",
    "cvr_decline_threshold": "0.2",
    "cvr_stability_tolerance": "0.05",
    "cpm_increase_threshold": "0.2",
    "delivery_decline_threshold": "0.2",
    "cpa_worsening_threshold": "0.2",
    "roas_worsening_threshold": "0.2",
    "bootstrap_lower_percentile": "0.025",
    "bootstrap_upper_percentile": "0.975",
    "breakdown_min_spend_share": "0.05",
    "active_day_floor": 10,
    "consecutive_composite_fatigue_hits_required": 2,
    "consecutive_healthy_evaluations_required": 3,
    "breakdown_min_sample_impressions": 500,
    "breakdown_min_sample_clicks": 10,
    "breakdown_min_sample_purchases": 2,
    "quality_warning_permitted_codes": ["freshness_slo_exceeded"],
    "streak_reset_reason_codes": ["policy_version_changed"],
    "advice_construction_parameters": [],
}


def _policy(**overrides):
    fields = dict(
        id=new_id("spol"),
        algorithm_version="beta-v1",
        canonical_schema_version="scale-snapshot-v1",
        decimal_precision=28,
        decimal_rounding="round_half_even",
        z_value=Decimal("1.96"),
        evidence_floor_minimum_spend=Decimal("100"),
        evidence_floor_minimum_days=14,
        evidence_floor_minimum_conversions=5,
        activity_floor_minimum_impressions=1000,
        sample_floor_minimum_clicks=20,
        spend_floor_minimum_daily_spend=Decimal("100"),
        freshness_floor_maximum_lag_hours=24,
        signal_thresholds=dict(_SIGNAL_THRESHOLDS),
        streak_rules={},
        bootstrap_prng_algorithm="bootstrap-prng-v1",
        bootstrap_draw_count=200,
        bootstrap_valid_draw_ratio=Decimal("0.5"),
        bootstrap_percentile_method="nearest_rank",
        bootstrap_draw_order="sequential",
        breakdown_families={},
        breakdown_sample_gate_minimum=1,
        breakdown_share_gate_minimum_ratio=Decimal("0.05"),
        bh_fdr_alpha=Decimal("0.10"),
        advice_severity_thresholds={},
        notification_cooldown_hours=24,
        conversion_safe_values={},
        mode_permissions={},
        is_published=True,
        created_by="user_1",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return ScalePolicyVersionRecord(**fields)


def _enrollment(ws, mode, **overrides):
    fields = dict(
        id=new_id("senr"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__" if mode != "controlled_mock" else "scenario_1",
        ad_account_id="act_1",
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="pv_1",
        offer_version_id="ov_1",
        market_code="US",
        remote_binding_key="mcon_1/act_1/camp_1",
        lifecycle="active",
        creator_id="user_1",
        original_initiator_id="user_1",
    )
    fields.update(overrides)
    return ScaleEnrollmentRecord(**fields)


def _episode(ws, mode, enrollment_id, contract_set_id, **overrides):
    fields = dict(
        id=new_id("sep"),
        workspace_id=ws,
        meta_mode=mode,
        enrollment_id=enrollment_id,
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="pv_1",
        offer_version_id="ov_1",
        market_code="US",
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__" if mode != "controlled_mock" else "scenario_1",
        ad_account_id="act_1",
        delivery_config_baseline_source_id="sdcs_baseline",
        delivery_evidence_hash="e" * 64,
        delivery_comparability_hash="c" * 64,
        metric_semantic_contract_set_id=contract_set_id,
        timezone_name="UTC",
        currency="USD",
        attribution_semantics_hash="a" * 64,
        phase="collecting",
        started_local_day=date(2026, 1, 1),
    )
    fields.update(overrides)
    return ScaleEpisodeRecord(**fields)


class EvidenceTestBase(unittest.TestCase):
    """Builds one complete, sealable happy-path fixture: a workspace,
    Enrollment, Episode, published Policy, a 3-member metric-contract set
    (daily/exact_window/delivery_configuration -- no breakdowns, kept
    minimal), a `complete` source manifest covering 14 daily days + 3 exact
    windows + 1 delivery-configuration instance, and one `requested`
    evaluation run selecting that manifest."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                WorkspaceRecord.__table__,
                ScaleEnrollmentRecord.__table__,
                ScaleEpisodeRecord.__table__,
                ScalePolicyVersionRecord.__table__,
                ScaleTargetVersionRecord.__table__,
                ScaleMetricSemanticContractSetRecord.__table__,
                ScaleMetricSemanticContractSetMemberRecord.__table__,
                ScaleDailyMetricSourceRecord.__table__,
                ScaleExactWindowSourceRecord.__table__,
                ScaleBreakdownSourceRecord.__table__,
                ScaleBreakdownDailyChildRecord.__table__,
                ScaleBreakdownPrivacyRecord.__table__,
                ScaleDeliveryConfigSourceRecord.__table__,
                ScaleEvidenceSourceManifestRecord.__table__,
                ScaleManifestItemRecord.__table__,
                ScaleSourceQualityIssueRecord.__table__,
                ScaleEvidenceSnapshotRecord.__table__,
                ScaleEvidenceSourceLinkRecord.__table__,
                ScaleSnapshotQualityIssueRecord.__table__,
                ScaleSnapshotBreakdownSegmentRecord.__table__,
                ScaleSnapshotBreakdownDailyRecord.__table__,
                ScaleEvaluationRunRecord.__table__,
                ScaleEvaluationRecord.__table__,
                ScaleEvaluationStreakCounterRecord.__table__,
            ],
        )
        self.session = Session(bind=self.engine)
        self.addCleanup(self.session.close)

        self.ws = WorkspaceRecord(id=new_id("ws"), name="Test WS")
        self.session.add(self.ws)
        self.mode = "internal_validation"
        # SQLAlchemy's automatic flush-ordering across mapped classes with no
        # ORM `relationship()` does not reliably topo-sort composite,
        # non-primary-key ForeignKeyConstraints (as every Scale table uses,
        # per the (workspace_id, meta_mode, id) convention) -- it can emit a
        # child table's INSERT before its parent's even though the schema's
        # own FK graph says otherwise. Flushing in explicit dependency stages
        # sidesteps that rather than fighting the unit-of-work's ordering.
        self.session.flush()

        self.enrollment = _enrollment(self.ws.id, self.mode)
        self.session.add(self.enrollment)

        self.contract_set = ScaleMetricSemanticContractSetRecord(
            id=new_id("smcs"), workspace_id=self.ws.id, meta_mode=self.mode, created_by="user_1",
        )
        self.session.add(self.contract_set)
        self.session.flush()

        members = [
            ScaleMetricSemanticContractSetMemberRecord(
                id=new_id("smcm"),
                contract_set_id=self.contract_set.id,
                contract_type="daily",
                observation_contract_generation="gen-1",
                contract_hash="daily-hash",
            ),
            ScaleMetricSemanticContractSetMemberRecord(
                id=new_id("smcm"),
                contract_set_id=self.contract_set.id,
                contract_type="exact_window",
                observation_contract_generation="gen-1",
                contract_hash="exact-hash",
            ),
            ScaleMetricSemanticContractSetMemberRecord(
                id=new_id("smcm"),
                contract_set_id=self.contract_set.id,
                contract_type="delivery_configuration",
                observation_contract_generation="gen-1",
                contract_hash="delivery-hash",
            ),
        ]
        self.session.add_all(members)
        self.session.flush()

        self.episode = _episode(self.ws.id, self.mode, self.enrollment.id, self.contract_set.id)
        self.session.add(self.episode)
        self.policy = _policy()
        self.session.add(self.policy)
        self.session.flush()

        self.manifest = _manifest(self.ws.id, self.mode, self.episode.id, AS_OF)
        self.session.add(self.manifest)
        self.session.flush()

        daily_rows = []
        d = BASELINE_START
        while d < CURRENT_END:
            daily_rows.append(_daily_source(self.ws.id, self.mode, self.episode.id, d))
            d += timedelta(days=1)
        self.session.add_all(daily_rows)

        self.exact_current = _exact_source(
            self.ws.id, self.mode, self.episode.id, "current_7d", CURRENT_START, CURRENT_END,
        )
        self.exact_baseline = _exact_source(
            self.ws.id, self.mode, self.episode.id, "baseline_7d", BASELINE_START, CURRENT_START,
        )
        self.exact_rolling = _exact_source(
            self.ws.id, self.mode, self.episode.id, "rolling_14d", BASELINE_START, CURRENT_END,
        )
        self.session.add_all([self.exact_current, self.exact_baseline, self.exact_rolling])

        self.delivery = _delivery_source(self.ws.id, self.mode, self.episode.id)
        self.session.add(self.delivery)
        self.session.flush()

        items = []
        for row in daily_rows:
            items.append(
                _item(
                    self.ws.id, self.mode, self.manifest.id, "daily", row.id, local_day=row.local_day,
                )
            )
        for row, role in (
            (self.exact_current, "current_7d"),
            (self.exact_baseline, "baseline_7d"),
            (self.exact_rolling, "rolling_14d"),
        ):
            items.append(
                _item(
                    self.ws.id, self.mode, self.manifest.id, "exact_window", row.id, window_role=role,
                )
            )
        items.append(
            _item(
                self.ws.id, self.mode, self.manifest.id, "delivery_configuration", self.delivery.id,
            )
        )
        self.session.add_all(items)

        self.run = _run(
            self.ws.id, self.mode, self.episode.id, self.enrollment.id, self.manifest, AS_OF,
            policy_id=self.policy.id,
        )
        self.session.add(self.run)

        self.session.flush()

        self.context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )


class TestSealSnapshotHappyPath(EvidenceTestBase):
    def test_seals_parent_children_and_hash_in_one_transaction(self):
        snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        self.session.commit()

        row = self.session.get(ScaleEvidenceSnapshotRecord, snapshot_id)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row.sealed_at)
        self.assertIsNotNone(row.canonical_hash)

        links = self.session.execute(
            select(ScaleEvidenceSourceLinkRecord).filter_by(snapshot_id=snapshot_id)
        ).scalars().all()
        # 14 daily + 3 exact-window + 1 delivery-configuration.
        self.assertEqual(len(links), 18)

        issues = self.session.execute(
            select(ScaleSnapshotQualityIssueRecord).filter_by(snapshot_id=snapshot_id)
        ).scalars().all()
        self.assertEqual(issues, [])

        self.session.refresh(self.run)
        self.assertEqual(self.run.snapshot_id, snapshot_id)
        self.assertEqual(self.run.run_state, "snapshot_sealed")

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.phase, "evaluating")
        self.assertEqual(self.episode.phase_run_id, self.run.id)

    def test_seal_is_idempotent_for_the_same_run(self):
        first = seal_snapshot(self.session, self.context, self.run.id)
        second = seal_snapshot(self.session, self.context, self.run.id)
        self.assertEqual(first, second)


class TestSealSnapshotBlockedManifest(EvidenceTestBase):
    def test_rejects_blocked_manifest_without_mutating_run(self):
        self.manifest.state = "blocked"
        self.manifest.last_complete_local_day = None
        self.session.flush()

        with self.assertRaises(SnapshotSealingError) as ctx:
            seal_snapshot(self.session, self.context, self.run.id)
        self.assertEqual(ctx.exception.code, "manifest_blocked")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "requested")
        self.assertIsNone(self.run.snapshot_id)


class TestDuplicateSealingRace(EvidenceTestBase):
    def test_concurrent_semantically_identical_run_reuses_sealed_snapshot(self):
        """Two distinct runs (different trigger_kind, so both are valid rows
        under `uq_sevr_semantic_key`) that otherwise select the same
        manifest/day/policy/freshness-reference produce the same claim hash.
        The second sealer must recover the first sealer's snapshot rather
        than erroring or creating a second row (spec §3.3 uniqueness race)."""
        first_id = seal_snapshot(self.session, self.context, self.run.id)

        # Isolate the claim-hash/sealed-content race recovery from the
        # separate Episode phase_run_id CAS fence (spec §3.3 step 8, "a
        # different live run cannot replace a non-terminal phase_run_id"):
        # releasing it here simulates the first run having already moved
        # phase ownership elsewhere, so this test exercises only the
        # uniqueness-race recovery this bullet is about.
        self.episode.phase_run_id = None
        self.session.flush()

        second_run = _run(
            self.ws.id, self.mode, self.episode.id, self.enrollment.id, self.manifest, AS_OF,
            policy_id=self.policy.id, trigger_kind="backfill",
        )
        self.session.add(second_run)
        self.session.flush()

        second_id = seal_snapshot(self.session, self.context, second_run.id)

        self.assertEqual(first_id, second_id)

        all_snapshots = self.session.execute(select(ScaleEvidenceSnapshotRecord)).scalars().all()
        self.assertEqual(len(all_snapshots), 1)

        self.session.refresh(second_run)
        self.assertEqual(second_run.snapshot_id, first_id)


class TestAssembleSnapshot(EvidenceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        self.session.commit()

    def test_returns_frozen_value_object_matching_sealed_hash(self):
        value = assemble_snapshot(self.session, self.context, self.snapshot_id)
        self.assertIsInstance(value, ScaleEvidenceSnapshot)

        row = self.session.get(ScaleEvidenceSnapshotRecord, self.snapshot_id)
        self.assertEqual(value.canonical_hash, row.canonical_hash)
        self.assertEqual(value.as_of_local_day, AS_OF)
        self.assertEqual(value.currency, "USD")
        self.assertEqual(value.timezone_name, "UTC")
        self.assertEqual(len(value.current_7d.daily), 7)
        self.assertEqual(len(value.baseline_7d.daily), 7)
        self.assertEqual(value.current_exact_7d.reach, 1000)
        self.assertIsNone(value.prior_streak)
        self.assertIsNotNone(value.prior_streak_absence_reason)

        # Byte-identical replay: reassembling again is deterministic.
        value_again = assemble_snapshot(self.session, self.context, self.snapshot_id)
        self.assertEqual(value.canonical_hash, value_again.canonical_hash)

    def test_raises_on_unsealed_snapshot(self):
        unsealed = ScaleEvidenceSnapshotRecord(
            id=new_id("sesn"),
            workspace_id=self.ws.id,
            meta_mode=self.mode,
            meta_connection_id="mcon_1",
            scenario_scope_key="__no_scenario__",
            ad_account_id="act_1",
            episode_id=self.episode.id,
            remote_binding_key="mcon_1/act_1/camp_1",
            claim_hash="unsealed-claim",
            source_manifest_id=self.manifest.id,
            source_manifest_hash=self.manifest.canonical_hash,
            source_revision=1,
            safe_watermark=AS_OF,
            policy_version_id=self.policy.id,
            target_version_id=None,
            normalized_target_key=NO_TARGET_MARKER,
            algorithm_version="beta-v1",
            canonical_schema_version="scale-snapshot-v1",
            metric_semantic_contract_set_id=self.contract_set.id,
            metric_semantic_contract_set_hash="x",
            delivery_config_source_id=self.delivery.id,
            delivery_evidence_hash=self.delivery.evidence_hash,
            delivery_comparability_hash=self.delivery.comparability_hash,
            timezone_name="UTC",
            currency="USD",
            attribution_semantics_hash="a" * 64,
            current_window_start_local_day=CURRENT_START,
            current_window_end_local_day=CURRENT_END,
            baseline_window_start_local_day=BASELINE_START,
            baseline_window_end_local_day=BASELINE_END,
            rolling_window_start_local_day=BASELINE_START,
            rolling_window_end_local_day=CURRENT_END,
            complete_local_days_in_episode=14,
            active_delivery_days_in_episode=14,
            data_continuity_generation="gen-1",
            prior_streak_absence_reason="no_earlier_distinct_day",
            last_complete_local_day=AS_OF,
            last_sync_at=datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc),
            freshness_reference_at=datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc),
            as_of_local_day=AS_OF,
            canonical_hash=None,
            sealed_at=None,
        )
        self.session.add(unsealed)
        self.session.flush()

        with self.assertRaises(SnapshotAssemblyError) as ctx:
            assemble_snapshot(self.session, self.context, unsealed.id)
        self.assertEqual(ctx.exception.code, "snapshot_not_sealed")

    def test_raises_on_hash_mismatch(self):
        row = self.session.get(ScaleEvidenceSnapshotRecord, self.snapshot_id)
        # Tamper a field that IS part of the hash payload but is not one of
        # the provenance fields checked first, so this exercises the hash
        # comparison specifically rather than the provenance check.
        row.complete_local_days_in_episode = 999
        self.session.flush()

        with self.assertRaises(SnapshotAssemblyError) as ctx:
            assemble_snapshot(self.session, self.context, self.snapshot_id)
        self.assertEqual(ctx.exception.code, "hash_mismatch")

    def test_raises_on_provenance_mismatch(self):
        row = self.session.get(ScaleEvidenceSnapshotRecord, self.snapshot_id)
        row.meta_connection_id = "mcon_other"
        self.session.flush()

        with self.assertRaises(SnapshotAssemblyError) as ctx:
            assemble_snapshot(self.session, self.context, self.snapshot_id)
        self.assertEqual(ctx.exception.code, "provenance_mismatch")


if __name__ == "__main__":
    unittest.main()
