"""Tests for the Advice lifecycle, conversion, disposition, snooze, and
freshness expiry (Issue #25, Task 13).

Spec: docs/superpowers/specs/2026-09-08-scale-section-3-evidence-advice-orchestration.md §3.5-§3.6
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-13-brief.md
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
)
from app.generation.models import GenerationDraftRecord
from app.platform.tenancy import TenantContext
from app.scale._canonical import canonical_encode
from app.scale._contracts import AdviceCandidate, AdviceEvidenceItem
from app.scale.advice import (
    AdviceLifecycleError,
    convert_occurrence_to_draft,
    convert_occurrence_to_test_plan,
    create_disposition,
    create_snooze,
    expire_advice_thread,
    process_advice_candidates,
)
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleAdviceConversionRecord,
    ScaleAdviceDispositionRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceSnoozeRecord,
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationAdviceCandidateRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScalePolicyVersionRecord,
    ScaleTestPlanDraftRecord,
)
from app.scale.types import AdviceSeverity, AdviceType

_ALL_TABLES = [
    WorkspaceRecord.__table__,
    ProductRecord.__table__,
    LibraryEntryRecord.__table__,
    LibraryVersionRecord.__table__,
    GenerationDraftRecord.__table__,
    ScaleEnrollmentRecord.__table__,
    ScaleEpisodeRecord.__table__,
    ScalePolicyVersionRecord.__table__,
    ScaleEvaluationRecord.__table__,
    ScaleEvaluationAdviceCandidateRecord.__table__,
    ScaleEvaluationRunRecord.__table__,
    ScaleAdviceThreadRecord.__table__,
    ScaleAdviceOccurrenceRecord.__table__,
    ScaleAdviceThreadEventRecord.__table__,
    ScaleAdviceDispositionRecord.__table__,
    ScaleAdviceSnoozeRecord.__table__,
    ScaleAdviceConversionRecord.__table__,
    ScaleTestPlanDraftRecord.__table__,
]

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
        scenario_scope_key="__no_scenario__",
        ad_account_id="act_1",
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="prod_1",
        offer_version_id="ov_1",
        market_code="US",
        remote_binding_key="mcon_1/act_1/camp_1",
        lifecycle="active",
        creator_id="user_1",
        original_initiator_id="user_1",
    )
    fields.update(overrides)
    return ScaleEnrollmentRecord(**fields)


def _episode(ws, mode, enrollment_id, **overrides):
    fields = dict(
        id=new_id("sep"),
        workspace_id=ws,
        meta_mode=mode,
        enrollment_id=enrollment_id,
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="prod_1",
        offer_version_id="ov_1",
        market_code="US",
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        ad_account_id="act_1",
        delivery_config_baseline_source_id="sdcs_baseline",
        delivery_evidence_hash="e" * 64,
        delivery_comparability_hash="c" * 64,
        metric_semantic_contract_set_id="smcs_1",
        timezone_name="UTC",
        currency="USD",
        attribution_semantics_hash="a" * 64,
        phase="collecting",
        started_local_day=date(2026, 1, 1),
    )
    fields.update(overrides)
    return ScaleEpisodeRecord(**fields)


def _evaluation(ws, mode, episode_id, *, policy_version_id, snapshot_id, **overrides):
    fields = dict(
        id=new_id("sev"),
        workspace_id=ws,
        meta_mode=mode,
        episode_id=episode_id,
        snapshot_id=snapshot_id,
        snapshot_canonical_hash=f"hash-{snapshot_id}",
        metric_semantic_contract_set_hash="mcsh-1",
        policy_version_id=policy_version_id,
        target_version_id=None,
        normalized_target_key=NO_TARGET_MARKER,
        algorithm_version="beta-v1",
        canonical_schema_version="scale-eval-v1",
        maturity="economics_ready",
        data_quality_state="pass",
        health=None,
        health_reason="ok",
        streak_compat_policy_version_id=policy_version_id,
        streak_compat_target_version_id=None,
        streak_compat_metric_contract_set_hash="mcsh-1",
        streak_compat_configuration_baseline_hash="cbh-1",
        streak_compat_data_continuity_generation="gen-1",
        streak_compat_canonical_hash="cch-1",
        streak_healthy_evaluations=0,
        canonical_result_payload={},
        result_hash=new_id("rh"),
    )
    fields.update(overrides)
    return ScaleEvaluationRecord(**fields)


def _candidate_row(ws, mode, evaluation_id, candidate: AdviceCandidate):
    return ScaleEvaluationAdviceCandidateRecord(
        id=new_id("seac"),
        workspace_id=ws,
        meta_mode=mode,
        evaluation_id=evaluation_id,
        advice_type=candidate.advice_type.value,
        reason_signature=candidate.reason_signature,
        severity=candidate.severity.value,
        signal_ids=list(candidate.signal_ids),
        evidence=[
            {"key": e.key, "value": json.loads(canonical_encode(e.value))}
            for e in candidate.evidence
        ],
    )


def _candidate(
    advice_type=AdviceType.REFRESH_CREATIVE,
    reason_signature="rs-1",
    severity=AdviceSeverity.WARNING,
    signal_ids=("sig_1",),
    evidence=(),
) -> AdviceCandidate:
    return AdviceCandidate(
        advice_type=advice_type,
        reason_signature=reason_signature,
        severity=severity,
        signal_ids=signal_ids,
        evidence=evidence or (AdviceEvidenceItem(key="observed", value=Decimal("1.5")),),
    )


def _run_row(
    ws, mode, episode_id, enrollment_id, *, policy_version_id, target_local_day,
    freshness_reference_at, evaluation_id, trigger_kind="live", run_state="evaluated",
):
    return ScaleEvaluationRunRecord(
        id=new_id("sevr"),
        workspace_id=ws,
        meta_mode=mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__",
        episode_id=episode_id,
        enrollment_id=enrollment_id,
        target_local_day=target_local_day,
        trigger_kind=trigger_kind,
        source_manifest_id="manifest_1",
        source_manifest_hash="h" * 8,
        source_revision=1,
        freshness_reference_at=freshness_reference_at,
        policy_version_id=policy_version_id,
        target_version_id=None,
        normalized_target_key=NO_TARGET_MARKER,
        algorithm_version="beta-v1",
        semantic_event_sequence=0,
        replay_generation=0,
        original_initiator_id="user_1",
        run_state=run_state,
        snapshot_id="snap-run-1",
        evaluation_id=evaluation_id,
        outcome_kind="evaluated" if run_state == "evaluated" else None,
    )


class AdviceTestBase(unittest.TestCase):
    """One Workspace, active Enrollment, collecting Episode, published
    Policy, and a matching Product row (so a `refresh_creative` conversion's
    real `GenerationDraftRecord` FK resolves)."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(bind=self.engine, tables=_ALL_TABLES)
        self.session = Session(bind=self.engine)
        self.addCleanup(self.session.close)

        self.ws = WorkspaceRecord(id=new_id("ws"), name="Test WS")
        self.session.add(self.ws)
        self.mode = "internal_validation"
        self.session.flush()

        self.product = ProductRecord(id="prod_1", workspace_id=self.ws.id, name="Product 1")
        self.session.add(self.product)

        self.policy = _policy()
        self.session.add(self.policy)

        self.enrollment = _enrollment(self.ws.id, self.mode)
        self.session.add(self.enrollment)
        self.session.flush()

        self.episode = _episode(self.ws.id, self.mode, self.enrollment.id)
        self.session.add(self.episode)
        self.enrollment.current_episode_id = self.episode.id
        self.session.flush()

        self.context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )

    def _add_evaluation(self, episode_id, *, snapshot_id, candidates=(), **overrides):
        fields = dict(policy_version_id=self.policy.id)
        fields.update(overrides)
        evaluation = _evaluation(
            self.ws.id, self.mode, episode_id, snapshot_id=snapshot_id, **fields,
        )
        self.session.add(evaluation)
        self.session.flush()
        for candidate in candidates:
            self.session.add(_candidate_row(self.ws.id, self.mode, evaluation.id, candidate))
        self.session.flush()
        return evaluation

    def _process(self, episode_id, evaluation_id, candidates, *, day, eligible=True, trigger_kind="live", occurred_at=None):
        return process_advice_candidates(
            self.session, self.context, episode_id, evaluation_id, candidates,
            trigger_kind=trigger_kind, evaluated_local_day=day, projection_eligible=eligible,
            occurred_at=occurred_at,
        )


class ThreadCreationAndOccurrenceTests(AdviceTestBase):
    def test_creates_thread_and_first_occurrence_for_new_candidate(self):
        candidate = _candidate(reason_signature="rs-new")
        evaluation = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])

        thread_ids = self._process(self.episode.id, evaluation.id, [candidate], day=date(2026, 9, 1))

        self.assertEqual(len(thread_ids), 1)
        thread = self.session.get(ScaleAdviceThreadRecord, thread_ids[0])
        self.assertEqual(thread.lifecycle, "active")
        self.assertEqual(thread.advice_type, "refresh_creative")
        self.assertEqual(thread.reason_signature, "rs-new")
        self.assertIsNone(thread.predecessor_thread_id)
        self.assertEqual(thread.first_occurrence_id, thread.latest_occurrence_id)

        occurrence = self.session.get(ScaleAdviceOccurrenceRecord, thread.first_occurrence_id)
        self.assertEqual(occurrence.evaluation_id, evaluation.id)
        self.assertEqual(occurrence.thread_id, thread.id)

        event = self.session.execute(
            select(ScaleAdviceThreadEventRecord).filter_by(thread_id=thread.id, event_type="created")
        ).scalar_one()
        self.assertEqual(event.current_lifecycle, "active")
        self.assertIsNone(event.previous_lifecycle)

    def test_appends_occurrence_to_existing_active_thread(self):
        candidate = _candidate(reason_signature="rs-recur")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_ids_1 = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))
        thread_id = thread_ids_1[0]

        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2", candidates=[candidate])
        thread_ids_2 = self._process(self.episode.id, eval2.id, [candidate], day=date(2026, 9, 2))

        self.assertEqual(thread_ids_2, [thread_id])
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")
        self.assertNotEqual(thread.first_occurrence_id, thread.latest_occurrence_id)

        occurrences = self.session.execute(
            select(ScaleAdviceOccurrenceRecord).filter_by(thread_id=thread_id)
        ).scalars().all()
        self.assertEqual(len(occurrences), 2)

        # Only one `created` event across both calls -- recurrence into an
        # existing active thread does not append a new lifecycle event.
        created_events = self.session.execute(
            select(ScaleAdviceThreadEventRecord).filter_by(thread_id=thread_id, event_type="created")
        ).scalars().all()
        self.assertEqual(len(created_events), 1)

    def test_idempotent_replay_of_same_evaluation_does_not_duplicate_occurrence(self):
        candidate = _candidate(reason_signature="rs-replay")
        evaluation = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])

        self._process(self.episode.id, evaluation.id, [candidate], day=date(2026, 9, 1))
        self._process(self.episode.id, evaluation.id, [candidate], day=date(2026, 9, 1))

        occurrences = self.session.execute(
            select(ScaleAdviceOccurrenceRecord).filter_by(evaluation_id=evaluation.id)
        ).scalars().all()
        self.assertEqual(len(occurrences), 1)


class MissCountingTests(AdviceTestBase):
    def _open_thread(self):
        candidate = _candidate(reason_signature="rs-miss")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_ids = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))
        return thread_ids[0]

    def test_single_eligible_miss_does_not_resolve(self):
        thread_id = self._open_thread()
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")
        self.assertIn("miss_streak=1", thread.lifecycle_reason)

    def test_two_distinct_day_eligible_misses_resolve_thread(self):
        thread_id = self._open_thread()
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))
        eval3 = self._add_evaluation(self.episode.id, snapshot_id="snap-3")
        self._process(self.episode.id, eval3.id, [], day=date(2026, 9, 3))

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "resolved")
        self.assertIsNotNone(thread.resolved_at)
        event = self.session.execute(
            select(ScaleAdviceThreadEventRecord).filter_by(thread_id=thread_id, event_type="resolved")
        ).scalar_one()
        self.assertEqual(event.current_lifecycle, "resolved")

    def test_same_day_reevaluation_does_not_double_count_miss(self):
        thread_id = self._open_thread()
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))
        # A second, same-day (policy/target) reevaluation.
        eval2b = self._add_evaluation(self.episode.id, snapshot_id="snap-2b")
        self._process(self.episode.id, eval2b.id, [], day=date(2026, 9, 2))

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")
        self.assertIn("miss_streak=1", thread.lifecycle_reason)

    def test_warming_maturity_evaluation_does_not_count_as_miss(self):
        thread_id = self._open_thread()
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2", maturity="warming")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))
        eval3 = self._add_evaluation(self.episode.id, snapshot_id="snap-3", maturity="warming")
        self._process(self.episode.id, eval3.id, [], day=date(2026, 9, 3))

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")
        self.assertIsNone(thread.lifecycle_reason)

    def test_backfill_trigger_does_not_count_as_miss(self):
        thread_id = self._open_thread()
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2), trigger_kind="backfill")
        eval3 = self._add_evaluation(self.episode.id, snapshot_id="snap-3")
        self._process(self.episode.id, eval3.id, [], day=date(2026, 9, 3), trigger_kind="backfill")

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")
        self.assertIsNone(thread.lifecycle_reason)

    def test_recurrence_resets_miss_streak(self):
        candidate = _candidate(reason_signature="rs-reset")
        thread_id = self._process(
            self.episode.id,
            self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate]).id,
            [candidate], day=date(2026, 9, 1),
        )[0]
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertIn("miss_streak=1", thread.lifecycle_reason)

        eval3 = self._add_evaluation(self.episode.id, snapshot_id="snap-3", candidates=[candidate])
        self._process(self.episode.id, eval3.id, [candidate], day=date(2026, 9, 3))
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")
        self.assertIsNone(thread.lifecycle_reason)


class SupersessionTests(AdviceTestBase):
    def test_semantic_identity_change_supersedes_with_successor(self):
        old_candidate = _candidate(reason_signature="rs-old-policy")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[old_candidate])
        old_thread_id = self._process(self.episode.id, eval1.id, [old_candidate], day=date(2026, 9, 1))[0]

        new_policy = _policy()
        self.session.add(new_policy)
        self.session.flush()
        new_candidate = _candidate(reason_signature="rs-new-policy")
        eval2 = self._add_evaluation(
            self.episode.id, snapshot_id="snap-2", candidates=[new_candidate],
            policy_version_id=new_policy.id,
        )
        thread_ids = self._process(self.episode.id, eval2.id, [new_candidate], day=date(2026, 9, 2))

        new_thread = self.session.execute(
            select(ScaleAdviceThreadRecord).filter_by(reason_signature="rs-new-policy")
        ).scalar_one()
        self.assertIn(new_thread.id, thread_ids)

        old_thread = self.session.get(ScaleAdviceThreadRecord, old_thread_id)
        self.assertEqual(old_thread.lifecycle, "superseded")
        self.assertEqual(old_thread.lifecycle_reason, "semantic_identity_changed")
        event = self.session.execute(
            select(ScaleAdviceThreadEventRecord).filter_by(thread_id=old_thread_id, event_type="superseded")
        ).scalar_one()
        self.assertEqual(event.successor_thread_id, new_thread.id)

    def test_episode_closure_supersedes_lazily_on_recurrence(self):
        candidate = _candidate(reason_signature="rs-carryover")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        old_thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]

        # Close episode 1 and open episode 2 for the same Enrollment.
        self.episode.phase = "closed"
        self.episode.ended_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        self.episode.closure_reason = "delivery_configuration_changed"
        self.session.flush()

        episode2 = _episode(self.ws.id, self.mode, self.enrollment.id, id=new_id("sep"))
        self.session.add(episode2)
        self.enrollment.current_episode_id = episode2.id
        self.session.flush()

        eval2 = self._add_evaluation(episode2.id, snapshot_id="snap-2", candidates=[candidate])
        thread_ids = self._process(episode2.id, eval2.id, [candidate], day=date(2026, 9, 3))

        new_thread = self.session.execute(
            select(ScaleAdviceThreadRecord).filter_by(episode_id=episode2.id, reason_signature="rs-carryover")
        ).scalar_one()
        self.assertIn(new_thread.id, thread_ids)
        self.assertEqual(new_thread.predecessor_thread_id, old_thread_id)

        old_thread = self.session.get(ScaleAdviceThreadRecord, old_thread_id)
        self.assertEqual(old_thread.lifecycle, "superseded")
        self.assertEqual(old_thread.lifecycle_reason, "episode_closed_recurred")
        event = self.session.execute(
            select(ScaleAdviceThreadEventRecord).filter_by(thread_id=old_thread_id, event_type="superseded")
        ).scalar_one()
        self.assertEqual(event.successor_thread_id, new_thread.id)


class ConversionTests(AdviceTestBase):
    def _open_thread(self, advice_type, reason_signature):
        candidate = _candidate(advice_type=advice_type, reason_signature=reason_signature)
        evaluation = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, evaluation.id, [candidate], day=date(2026, 9, 1))[0]
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        return thread, self.session.get(ScaleAdviceOccurrenceRecord, thread.latest_occurrence_id)

    def test_convert_occurrence_to_draft_creates_generation_draft(self):
        thread, occurrence = self._open_thread(AdviceType.REFRESH_CREATIVE, "rs-draft")

        conversion_id = convert_occurrence_to_draft(
            self.session, self.context, occurrence.id, initiator_id="user_1",
        )

        conversion = self.session.get(ScaleAdviceConversionRecord, conversion_id)
        self.assertIsNotNone(conversion.generation_draft_id)
        self.assertIsNone(conversion.scale_test_plan_draft_id)

        draft = self.session.get(GenerationDraftRecord, conversion.generation_draft_id)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.state, "editing")
        self.assertEqual(draft.product_id, self.enrollment.product_version_id)
        # The planner (Issue #157) rejects unknown strategy shapes instead
        # of collapsing them, so the conversion must write the canonical one.
        from app.generation.planner import normalize_strategy

        self.assertEqual(normalize_strategy(draft.combination_strategy_json).kind, "full")
        self.assertEqual(draft.combination_strategy_json["origin"], "scale_advice_refresh")

        disposition = self.session.execute(
            select(ScaleAdviceDispositionRecord).filter_by(thread_id=thread.id)
        ).scalar_one()
        self.assertEqual(disposition.kind, "converted_to_draft")
        self.assertEqual(disposition.linked_conversion_id, conversion_id)

        refreshed_thread = self.session.get(ScaleAdviceThreadRecord, thread.id)
        self.assertEqual(refreshed_thread.disposition_kind, "converted_to_draft")
        # Lifecycle is untouched by conversion's disposition.
        self.assertEqual(refreshed_thread.lifecycle, "active")

    def test_convert_occurrence_to_draft_uses_the_product_behind_the_winner_version(self):
        """A real Enrollment carries the Winner's Library *ProductVersion*
        id; the Draft must reference the Product bridged to that version's
        ProductProfile entry (products.product_profile_entry_id), never the
        version id, which generation_drafts' Product FK rejects."""
        entry = LibraryEntryRecord(
            id="le_product_1", workspace_id=self.ws.id,
            entry_type="product", display_name="Product 1",
        )
        self.session.add(entry)
        self.session.flush()
        self.session.add(LibraryVersionRecord(
            id="lv_product_1", workspace_id=self.ws.id, entry_id=entry.id,
            entry_type="product", version_no=1, content_json={},
            content_hash="c" * 64, published_by="user_1",
        ))
        self.session.add(ProductRecord(
            id="prod_library", workspace_id=self.ws.id, name="Library Product",
            product_profile_entry_id=entry.id,
        ))
        self.enrollment.product_version_id = "lv_product_1"
        self.session.flush()
        _, occurrence = self._open_thread(AdviceType.REFRESH_CREATIVE, "rs-draft-library")

        conversion_id = convert_occurrence_to_draft(
            self.session, self.context, occurrence.id, initiator_id="user_1",
        )

        conversion = self.session.get(ScaleAdviceConversionRecord, conversion_id)
        draft = self.session.get(GenerationDraftRecord, conversion.generation_draft_id)
        self.assertEqual(draft.product_id, "prod_library")

    def test_convert_occurrence_to_draft_is_idempotent(self):
        _, occurrence = self._open_thread(AdviceType.REFRESH_CREATIVE, "rs-draft-idem")

        first_id = convert_occurrence_to_draft(self.session, self.context, occurrence.id, initiator_id="user_1")
        second_id = convert_occurrence_to_draft(self.session, self.context, occurrence.id, initiator_id="user_1")

        self.assertEqual(first_id, second_id)
        conversions = self.session.execute(
            select(ScaleAdviceConversionRecord).filter_by(occurrence_id=occurrence.id)
        ).scalars().all()
        self.assertEqual(len(conversions), 1)

    def test_convert_occurrence_to_draft_rejects_wrong_advice_type(self):
        _, occurrence = self._open_thread(AdviceType.CONSIDER_SCALE_TEST, "rs-wrong-type")

        with self.assertRaises(AdviceLifecycleError):
            convert_occurrence_to_draft(self.session, self.context, occurrence.id, initiator_id="user_1")

    def test_convert_occurrence_to_test_plan_creates_draft(self):
        thread, occurrence = self._open_thread(AdviceType.CONSIDER_SCALE_TEST, "rs-test-plan")

        conversion_id = convert_occurrence_to_test_plan(
            self.session, self.context, occurrence.id, initiator_id="user_1",
        )

        conversion = self.session.get(ScaleAdviceConversionRecord, conversion_id)
        self.assertIsNone(conversion.generation_draft_id)
        self.assertIsNotNone(conversion.scale_test_plan_draft_id)

        draft = self.session.get(ScaleTestPlanDraftRecord, conversion.scale_test_plan_draft_id)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.conversion_id, conversion_id)
        self.assertEqual(draft.generated_criteria["occurrence_id"], occurrence.id)
        self.assertIsNone(draft.planned_changes)

        disposition = self.session.execute(
            select(ScaleAdviceDispositionRecord).filter_by(thread_id=thread.id)
        ).scalar_one()
        self.assertEqual(disposition.kind, "converted_to_draft")


class DispositionSnoozeOrthogonalityTests(AdviceTestBase):
    def test_disposition_never_changes_lifecycle(self):
        candidate = _candidate(reason_signature="rs-dispo")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]

        disposition_id = create_disposition(
            self.session, self.context, thread_id, "dismissed", actor_id="user_1",
        )
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.disposition_kind, "dismissed")
        self.assertEqual(thread.lifecycle, "active")

        # Dismissed Advice still resolves through evidence.
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))
        eval3 = self._add_evaluation(self.episode.id, snapshot_id="snap-3")
        self._process(self.episode.id, eval3.id, [], day=date(2026, 9, 3))

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "resolved")
        self.assertEqual(thread.disposition_kind, "dismissed")

        disposition = self.session.get(ScaleAdviceDispositionRecord, disposition_id)
        self.assertEqual(disposition.kind, "dismissed")

    def test_create_disposition_rejects_converted_to_draft_kind(self):
        candidate = _candidate(reason_signature="rs-dispo-reject")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]

        with self.assertRaises(AdviceLifecycleError):
            create_disposition(self.session, self.context, thread_id, "converted_to_draft", actor_id="user_1")

    def test_snooze_never_changes_lifecycle_and_survives_supersession(self):
        candidate = _candidate(reason_signature="rs-snooze")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]

        snooze_until = datetime(2026, 12, 1, tzinfo=timezone.utc)
        create_snooze(self.session, self.context, thread_id, snooze_until, actor_id="user_1", reason="quiet")

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.snooze_until.replace(tzinfo=None), snooze_until.replace(tzinfo=None))
        self.assertEqual(thread.lifecycle, "active")

        # Close episode 1, recur in episode 2 -- snoozed Advice can still be
        # superseded by Episode closure.
        self.episode.phase = "closed"
        self.episode.ended_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        self.episode.closure_reason = "delivery_configuration_changed"
        self.session.flush()
        episode2 = _episode(self.ws.id, self.mode, self.enrollment.id, id=new_id("sep"))
        self.session.add(episode2)
        self.enrollment.current_episode_id = episode2.id
        self.session.flush()
        eval2 = self._add_evaluation(episode2.id, snapshot_id="snap-2", candidates=[candidate])
        self._process(episode2.id, eval2.id, [candidate], day=date(2026, 9, 3))

        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "superseded")
        # SQLite round-trips a datetime through TEXT storage and drops
        # tzinfo; compare naive-to-naive (known pitfall, see `_ensure_utc`).
        self.assertEqual(thread.snooze_until.replace(tzinfo=None), snooze_until.replace(tzinfo=None))

    def test_create_snooze_rejects_non_future_until(self):
        candidate = _candidate(reason_signature="rs-snooze-past")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]

        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(AdviceLifecycleError):
            create_snooze(self.session, self.context, thread_id, past, actor_id="user_1")


class FreshnessExpiryTests(AdviceTestBase):
    def test_expires_thread_when_deadline_exceeded(self):
        candidate = _candidate(reason_signature="rs-expire")
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(
            self.episode.id, eval1.id, [candidate], day=date(2026, 1, 1), occurred_at=old_time,
        )[0]

        expired = expire_advice_thread(
            self.session, self.context, thread_id,
            now=old_time + timedelta(hours=self.policy.freshness_floor_maximum_lag_hours + 1),
        )

        self.assertTrue(expired)
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "expired")
        event = self.session.execute(
            select(ScaleAdviceThreadEventRecord).filter_by(thread_id=thread_id, event_type="expired")
        ).scalar_one()
        self.assertEqual(event.current_lifecycle, "expired")

    def test_no_op_when_evidence_still_fresh(self):
        candidate = _candidate(reason_signature="rs-fresh")
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(
            self.episode.id, eval1.id, [candidate], day=date(2026, 1, 1), occurred_at=old_time,
        )[0]

        # A newer live run recorded fresher evidence for the Episode --
        # this should move the deadline even though the occurrence itself
        # is old.
        self.session.add(_run_row(
            self.ws.id, self.mode, self.episode.id, self.enrollment.id,
            policy_version_id=self.policy.id,
            target_local_day=date(2026, 1, 10),
            freshness_reference_at=old_time + timedelta(days=9),
            evaluation_id=eval1.id,
        ))
        self.session.flush()

        expired = expire_advice_thread(
            self.session, self.context, thread_id,
            now=old_time + timedelta(hours=self.policy.freshness_floor_maximum_lag_hours + 1),
        )

        self.assertFalse(expired)
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "active")

    def test_no_op_when_already_terminal(self):
        candidate = _candidate(reason_signature="rs-terminal")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]
        create_disposition(self.session, self.context, thread_id, "acknowledged", actor_id="user_1")

        # Resolve it via two eligible misses.
        eval2 = self._add_evaluation(self.episode.id, snapshot_id="snap-2")
        self._process(self.episode.id, eval2.id, [], day=date(2026, 9, 2))
        eval3 = self._add_evaluation(self.episode.id, snapshot_id="snap-3")
        self._process(self.episode.id, eval3.id, [], day=date(2026, 9, 3))

        expired = expire_advice_thread(
            self.session, self.context, thread_id, now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(expired)
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)
        self.assertEqual(thread.lifecycle, "resolved")


class NegativeTenantTests(AdviceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.other_ws = WorkspaceRecord(id=new_id("ws"), name="Other WS")
        self.session.add(self.other_ws)
        self.session.flush()
        self.other_context = TenantContext(
            workspace_id=self.other_ws.id, role="system", executor="system:worker",
            initiator_id="user_2", meta_mode=self.mode,
        )

    def test_process_advice_candidates_rejects_cross_workspace_episode(self):
        candidate = _candidate(reason_signature="rs-tenant")
        evaluation = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])

        with self.assertRaises(AdviceLifecycleError) as ctx:
            process_advice_candidates(
                self.session, self.other_context, self.episode.id, evaluation.id, [candidate],
                trigger_kind="live", evaluated_local_day=date(2026, 9, 1), projection_eligible=True,
            )
        self.assertEqual(ctx.exception.code, "episode_not_found")

    def test_process_advice_candidates_rejects_cross_meta_mode_episode(self):
        candidate = _candidate(reason_signature="rs-tenant-mode")
        evaluation = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        wrong_mode_context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="customer_sandbox",
        )

        with self.assertRaises(AdviceLifecycleError) as ctx:
            process_advice_candidates(
                self.session, wrong_mode_context, self.episode.id, evaluation.id, [candidate],
                trigger_kind="live", evaluated_local_day=date(2026, 9, 1), projection_eligible=True,
            )
        self.assertEqual(ctx.exception.code, "episode_not_found")

    def test_create_disposition_rejects_cross_workspace_thread(self):
        candidate = _candidate(reason_signature="rs-tenant-dispo")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]

        with self.assertRaises(AdviceLifecycleError) as ctx:
            create_disposition(self.session, self.other_context, thread_id, "acknowledged", actor_id="user_2")
        self.assertEqual(ctx.exception.code, "thread_not_found")

    def test_convert_occurrence_to_draft_rejects_cross_workspace_occurrence(self):
        candidate = _candidate(advice_type=AdviceType.REFRESH_CREATIVE, reason_signature="rs-tenant-conv")
        eval1 = self._add_evaluation(self.episode.id, snapshot_id="snap-1", candidates=[candidate])
        thread_id = self._process(self.episode.id, eval1.id, [candidate], day=date(2026, 9, 1))[0]
        thread = self.session.get(ScaleAdviceThreadRecord, thread_id)

        with self.assertRaises(AdviceLifecycleError) as ctx:
            convert_occurrence_to_draft(
                self.session, self.other_context, thread.latest_occurrence_id, initiator_id="user_2",
            )
        self.assertEqual(ctx.exception.code, "occurrence_not_found")


if __name__ == "__main__":
    unittest.main()
