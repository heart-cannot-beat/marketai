"""Tests for durable evaluation orchestration (Issue #25, Task 12).

Spec: docs/superpowers/specs/2026-09-08-scale-section-3-evidence-advice-orchestration.md §3.1, §3.4
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-12-brief.md
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord, new_id, utcnow
from app.platform.tenancy import TenantContext
from app.scale.enrollment import ScaleEnrollmentEventRecord
from app.scale.evidence import seal_snapshot
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleBreakdownDailyChildRecord,
    ScaleBreakdownPrivacyRecord,
    ScaleBreakdownSourceRecord,
    ScaleDailyMetricSourceRecord,
    ScaleDeliveryConfigSourceRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeEventRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationAdviceCandidateRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationSignalRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvaluationStreakResetRecord,
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
import app.scale.service as service_module
from app.scale.service import (
    ScaleEvaluationRequest,
    ScaleOrchestrationError,
    ScaleRunOutcome,
    request_scale_evaluation,
    run_scale_evaluation,
)
from app.scale.types import RunOutcomeKind

# Reuse Task 10's fixture builders rather than duplicating ~350 lines of ORM
# row construction.
from tests.test_scale_evidence import (
    _SIGNAL_THRESHOLDS,
    _daily_source,
    _delivery_source,
    _enrollment,
    _episode,
    _exact_source,
    _item,
    _manifest,
    _policy,
    _run,
)

AS_OF = date(2026, 9, 5)
CURRENT_START = AS_OF - timedelta(days=6)
CURRENT_END = AS_OF + timedelta(days=1)
BASELINE_START = CURRENT_START - timedelta(days=7)
BASELINE_END = CURRENT_START

_ALL_TABLES = [
    WorkspaceRecord.__table__,
    AuditEventRecord.__table__,
    ScaleAdviceThreadRecord.__table__,
    ScaleAdviceOccurrenceRecord.__table__,
    ScaleAdviceThreadEventRecord.__table__,
    ScaleEnrollmentRecord.__table__,
    ScaleEnrollmentEventRecord.__table__,
    ScaleEpisodeRecord.__table__,
    ScaleEpisodeEventRecord.__table__,
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
    ScaleEvaluationSignalRecord.__table__,
    ScaleEvaluationAdviceCandidateRecord.__table__,
    ScaleEvaluationStreakCounterRecord.__table__,
    ScaleEvaluationStreakResetRecord.__table__,
    ScaleEpisodeProjectionRecord.__table__,
]


class OrchestrationTestBase(unittest.TestCase):
    """One complete, sealable happy-path fixture (mirrors
    ``tests.test_scale_evidence.EvidenceTestBase``, plus every table Task 12
    additionally reads/writes): a workspace, active Enrollment, collecting
    Episode, published Policy (no Target), a 3-member metric-contract set,
    a `complete` source manifest covering 21 daily days + 3 exact windows +
    1 delivery-configuration instance, and one `requested` evaluation run."""

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

        self.enrollment = _enrollment(self.ws.id, self.mode)
        self.session.add(self.enrollment)

        self.contract_set = ScaleMetricSemanticContractSetRecord(
            id=new_id("smcs"), workspace_id=self.ws.id, meta_mode=self.mode, created_by="user_1",
        )
        self.session.add(self.contract_set)
        self.session.flush()

        members = [
            ScaleMetricSemanticContractSetMemberRecord(
                id=new_id("smcm"), contract_set_id=self.contract_set.id, contract_type="daily",
                observation_contract_generation="gen-1", contract_hash="daily-hash",
            ),
            ScaleMetricSemanticContractSetMemberRecord(
                id=new_id("smcm"), contract_set_id=self.contract_set.id, contract_type="exact_window",
                observation_contract_generation="gen-1", contract_hash="exact-hash",
            ),
            ScaleMetricSemanticContractSetMemberRecord(
                id=new_id("smcm"), contract_set_id=self.contract_set.id, contract_type="delivery_configuration",
                observation_contract_generation="gen-1", contract_hash="delivery-hash",
            ),
        ]
        self.session.add_all(members)
        self.session.flush()

        self.episode = _episode(self.ws.id, self.mode, self.enrollment.id, self.contract_set.id)
        self.session.add(self.episode)
        self.policy = _policy()
        self.session.add(self.policy)
        self.session.flush()

        self.enrollment.current_episode_id = self.episode.id
        self.session.flush()

        self.context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )

    def _seed_manifest(
        self, as_of, *, revision=1, comparability_hash="c" * 64,
        current_start=None, current_end=None, baseline_start=None, baseline_end=None,
        daily_rows=None,
    ):
        """Build one complete manifest for `as_of`, covering the 14 daily
        days its current+baseline windows need plus 3 exact windows and 1
        delivery-configuration instance. `daily_rows`, when given, maps
        `local_day -> ScaleDailyMetricSourceRecord` and is used instead of
        building fresh rows (so a sequence of same-Episode manifests can
        share an underlying daily-row pool)."""
        current_start = current_start or (as_of - timedelta(days=6))
        current_end = current_end or (as_of + timedelta(days=1))
        baseline_start = baseline_start or (current_start - timedelta(days=7))
        baseline_end = baseline_end or current_start

        if daily_rows is None:
            daily_rows = {}
            d = baseline_start
            while d < current_end:
                row = _daily_source(self.ws.id, self.mode, self.episode.id, d, revision=revision)
                daily_rows[d] = row
                d += timedelta(days=1)
            self.session.add_all(daily_rows.values())

        exact_current = _exact_source(
            self.ws.id, self.mode, self.episode.id, "current_7d", current_start, current_end, revision=revision,
        )
        exact_baseline = _exact_source(
            self.ws.id, self.mode, self.episode.id, "baseline_7d", baseline_start, current_start, revision=revision,
        )
        exact_rolling = _exact_source(
            self.ws.id, self.mode, self.episode.id, "rolling_14d", baseline_start, current_end, revision=revision,
        )
        self.session.add_all([exact_current, exact_baseline, exact_rolling])

        delivery = _delivery_source(self.ws.id, self.mode, self.episode.id, revision=revision)
        delivery.comparability_hash = comparability_hash
        self.session.add(delivery)
        self.session.flush()

        manifest = _manifest(self.ws.id, self.mode, self.episode.id, as_of, revision=revision)
        self.session.add(manifest)
        self.session.flush()

        items = []
        d = baseline_start
        while d < current_end:
            items.append(
                _item(self.ws.id, self.mode, manifest.id, "daily", daily_rows[d].id, local_day=d)
            )
            d += timedelta(days=1)
        for row, role in (
            (exact_current, "current_7d"), (exact_baseline, "baseline_7d"), (exact_rolling, "rolling_14d"),
        ):
            items.append(_item(self.ws.id, self.mode, manifest.id, "exact_window", row.id, window_role=role))
        items.append(_item(self.ws.id, self.mode, manifest.id, "delivery_configuration", delivery.id))
        self.session.add_all(items)
        self.session.flush()

        return manifest, daily_rows

    def _seed_run(self, manifest, as_of, *, revision=1, trigger_kind="live", freshness_hour=4, **overrides):
        run = _run(
            self.ws.id, self.mode, self.episode.id, self.enrollment.id, manifest, as_of,
            policy_id=self.policy.id, trigger_kind=trigger_kind, revision=revision,
        )
        run.freshness_reference_at = datetime(2026, 9, 6, freshness_hour, 0, tzinfo=timezone.utc)
        for key, value in overrides.items():
            setattr(run, key, value)
        self.session.add(run)
        self.session.flush()
        return run


class TestFullEvaluationHappyPath(OrchestrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_evaluates_persists_and_projects(self):
        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.assertEqual(outcome.run_id, self.run.id)
        self.assertEqual(outcome.enrollment_id, self.enrollment.id)
        self.assertEqual(outcome.episode_id, self.episode.id)
        self.assertIsNotNone(outcome.evaluation_id)

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "evaluated")
        self.assertEqual(self.run.outcome_kind, "evaluated")
        self.assertEqual(self.run.evaluation_id, outcome.evaluation_id)
        self.assertIsNotNone(self.run.snapshot_id)

        evaluation = self.session.get(ScaleEvaluationRecord, outcome.evaluation_id)
        self.assertIsNotNone(evaluation)
        self.assertEqual(evaluation.episode_id, self.episode.id)
        self.assertEqual(evaluation.snapshot_id, self.run.snapshot_id)
        self.assertEqual(evaluation.algorithm_version, "beta-v1")
        self.assertEqual(evaluation.normalized_target_key, NO_TARGET_MARKER)

        signals = self.session.execute(
            select(ScaleEvaluationSignalRecord).filter_by(evaluation_id=evaluation.id)
        ).scalars().all()
        self.assertGreater(len(signals), 0)

        counters = self.session.execute(
            select(ScaleEvaluationStreakCounterRecord).filter_by(evaluation_id=evaluation.id)
        ).scalars().all()
        self.assertGreater(len(counters), 0)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.phase, "collecting")
        self.assertIsNone(self.episode.phase_run_id)
        self.assertEqual(self.episode.accepted_evaluation_id, evaluation.id)
        self.assertEqual(self.episode.projection_revision, 1)
        self.assertEqual(self.episode.projection_as_of_local_day, AS_OF)

        projection = self.session.execute(
            select(ScaleEpisodeProjectionRecord).filter_by(episode_id=self.episode.id)
        ).scalar_one()
        self.assertEqual(projection.evaluation_id, evaluation.id)
        self.assertEqual(projection.revision, 1)

        audit_events = self.session.execute(
            select(AuditEventRecord).filter_by(resource_id=self.run.id)
        ).scalars().all()
        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0].event_type, "scale_evaluation_evaluated")
        self.assertEqual(audit_events[0].details_json["evaluation_id"], evaluation.id)

    def test_rerunning_terminal_run_is_idempotent(self):
        first = run_scale_evaluation(self.session, self.context, self.run.id)
        second = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(first.evaluation_id, second.evaluation_id)
        self.assertEqual(second.kind, RunOutcomeKind.EVALUATED)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(len(evaluations), 1)

        audit_events = self.session.execute(select(AuditEventRecord)).scalars().all()
        self.assertEqual(len(audit_events), 1)

    def test_resumes_after_seal_only_crash_without_resealing(self):
        # Simulate a crash between Phase A committing the sealed snapshot
        # and Phase C committing the Evaluation: run seal_snapshot() and
        # commit exactly as Phase A would, then resume via the public entry
        # point as a fresh call would after a Worker restart.
        sealed_snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        self.session.commit()
        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "snapshot_sealed")

        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.session.refresh(self.run)
        self.assertEqual(self.run.snapshot_id, sealed_snapshot_id)

        snapshots = self.session.execute(select(ScaleEvidenceSnapshotRecord)).scalars().all()
        self.assertEqual(len(snapshots), 1)

    def test_phase_b_evaluates_after_committing_and_before_evaluator_call(self):
        """Spec §3.4 Phase B: the deterministic bootstrap runs with no open
        database transaction. Proven here by recording call order: the
        session's `commit` must fire strictly before `evaluate_scale` is
        invoked."""
        events: list[str] = []

        original_commit = self.session.commit

        def _recording_commit(*args, **kwargs):
            events.append("commit")
            return original_commit(*args, **kwargs)

        original_evaluate = service_module.evaluate_scale

        def _recording_evaluate(snapshot):
            events.append("evaluate_scale")
            return original_evaluate(snapshot)

        with mock.patch.object(self.session, "commit", side_effect=_recording_commit), \
             mock.patch.object(service_module, "evaluate_scale", side_effect=_recording_evaluate):
            outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.assertIn("evaluate_scale", events)
        evaluate_index = events.index("evaluate_scale")
        # At least one commit (Phase A's) precedes assembly, and the commit
        # immediately preceding `evaluate_scale` is Phase B's own -- proving
        # the transaction was closed before pure computation ran.
        self.assertGreaterEqual(evaluate_index, 1)
        self.assertEqual(events[evaluate_index - 1], "commit")


class TestRequestScaleEvaluation(OrchestrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)

    def _request(self, **overrides) -> ScaleEvaluationRequest:
        fields = dict(
            enrollment_id=self.enrollment.id,
            episode_id=self.episode.id,
            target_local_day=AS_OF,
            trigger_kind="live",
            source_manifest_id=self.manifest.id,
            source_manifest_hash=self.manifest.canonical_hash,
            source_revision=1,
            freshness_reference_at=datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc),
            policy_version_id=self.policy.id,
            algorithm_version="beta-v1",
            original_initiator_id="user_1",
        )
        fields.update(overrides)
        return ScaleEvaluationRequest(**fields)

    def test_creates_a_requested_run(self):
        run_id = request_scale_evaluation(self.session, self.context, self._request())

        run = self.session.get(ScaleEvaluationRunRecord, run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.run_state, "requested")
        self.assertIsNone(run.snapshot_id)
        self.assertIsNone(run.outcome_kind)
        self.assertEqual(run.normalized_target_key, NO_TARGET_MARKER)

    def test_reuses_run_for_the_same_semantic_key(self):
        first_id = request_scale_evaluation(self.session, self.context, self._request())
        second_id = request_scale_evaluation(self.session, self.context, self._request())

        self.assertEqual(first_id, second_id)
        runs = self.session.execute(select(ScaleEvaluationRunRecord)).scalars().all()
        self.assertEqual(len(runs), 1)

    def test_distinct_trigger_kind_creates_a_distinct_run(self):
        first_id = request_scale_evaluation(self.session, self.context, self._request())
        second_id = request_scale_evaluation(
            self.session, self.context, self._request(trigger_kind="backfill"),
        )
        self.assertNotEqual(first_id, second_id)

    def test_end_to_end_request_then_run(self):
        run_id = request_scale_evaluation(self.session, self.context, self._request())
        outcome = run_scale_evaluation(self.session, self.context, run_id)
        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)


class TestManifestBlockedLeavesRunUntouched(OrchestrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)
        # Commit the fixture first so the blocked-manifest path's rollback
        # (which releases the `SELECT ... FOR UPDATE` locks it took) only
        # discards its own uncommitted mutation below, not the fixture --
        # matching a realistic scenario where the manifest transitions to
        # `blocked` in a separate, already-committed transaction.
        self.session.commit()
        self.manifest.state = "blocked"
        self.manifest.last_complete_local_day = None
        self.session.flush()

    def test_returns_blocked_outcome_without_mutating_run(self):
        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.BLOCKED)
        self.assertEqual(outcome.detail, "manifest_blocked")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "requested")
        self.assertIsNone(self.run.snapshot_id)
        self.assertIsNone(self.run.outcome_kind)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])


class TestEpisodeRolloverDuringPhaseA(OrchestrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        # A manifest whose reconciled delivery configuration no longer
        # matches the Episode's frozen comparability baseline ('c' * 64).
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF, comparability_hash="d" * 64)
        self.run = self._seed_run(self.manifest, AS_OF)
        self.old_episode_id = self.episode.id

    def test_closes_old_episode_and_opens_a_new_one(self):
        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EPISODE_ROLLED_OVER)
        self.assertEqual(outcome.episode_id, self.old_episode_id)
        self.assertEqual(outcome.detail, "delivery_configuration_changed")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "episode_rolled_over")
        self.assertEqual(self.run.outcome_kind, "episode_rolled_over")
        # No Evaluation was ever produced for a rolled-over run.
        self.assertIsNone(self.run.evaluation_id)

        old_episode = self.session.get(ScaleEpisodeRecord, self.old_episode_id)
        self.assertEqual(old_episode.phase, "closed")
        self.assertEqual(old_episode.closure_reason, "delivery_configuration_changed")

        self.session.refresh(self.enrollment)
        new_episode_id = self.enrollment.current_episode_id
        self.assertNotEqual(new_episode_id, self.old_episode_id)
        new_episode = self.session.get(ScaleEpisodeRecord, new_episode_id)
        self.assertIsNotNone(new_episode)
        self.assertEqual(new_episode.phase, "collecting")
        self.assertEqual(new_episode.delivery_comparability_hash, "d" * 64)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])


class TestUpstreamWithdrawalDuringPhaseC(OrchestrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_blocks_without_persisting_the_evaluation(self):
        original_evaluate = service_module.evaluate_scale

        def _evaluate_then_withdraw(snapshot):
            # A concurrent upstream withdrawal committed by another
            # transaction during Phase B's DB-free window (spec §3.4 Phase
            # C: "Recheck upstream eligibility ... A concurrent withdrawal
            # ... terminates the run as blocked without persisting the
            # computed result").
            self.enrollment.lifecycle = "closed"
            self.session.flush()
            self.session.commit()
            return original_evaluate(snapshot)

        with mock.patch.object(service_module, "evaluate_scale", side_effect=_evaluate_then_withdraw):
            outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.BLOCKED)
        self.assertEqual(outcome.detail, "upstream_withdrawal")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "blocked")
        # Phase A already sealed a real snapshot before the race happened.
        self.assertIsNotNone(self.run.snapshot_id)
        self.assertIsNone(self.run.evaluation_id)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])

        self.session.refresh(self.episode)
        # Withdrawal did not close this Episode (no current Enrollment
        # Episode was locked by the withdrawal path in this test); the run
        # itself simply stopped short of projecting or evaluating.
        self.assertIsNone(self.episode.accepted_evaluation_id)


class TestPhaseCDistinguishesRolloverFromCancelled(OrchestrationTestBase):
    """spec §3.4 Phase C: "A concurrent configuration rollover makes a live
    run `episode_rolled_over`; pause, close, or another closure makes it
    `cancelled`." Both races are simulated the same way as
    ``TestUpstreamWithdrawalDuringPhaseC``: a side effect on `evaluate_scale`
    commits a concurrent Episode closure during Phase B's DB-free window."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def _close_episode_concurrently(self, closure_reason: str):
        original_evaluate = service_module.evaluate_scale

        def _evaluate_then_close(snapshot):
            self.episode.phase = "closed"
            self.episode.closure_reason = closure_reason
            self.episode.ended_at = utcnow()
            self.episode.revision += 1
            self.session.flush()
            self.session.commit()
            return original_evaluate(snapshot)

        return mock.patch.object(service_module, "evaluate_scale", side_effect=_evaluate_then_close)

    def test_concurrent_rollover_yields_episode_rolled_over(self):
        with self._close_episode_concurrently("delivery_configuration_changed"):
            outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EPISODE_ROLLED_OVER)
        self.assertEqual(outcome.detail, "delivery_configuration_changed")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "episode_rolled_over")
        self.assertEqual(self.run.outcome_kind, "episode_rolled_over")
        self.assertIsNone(self.run.evaluation_id)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])

    def test_concurrent_plain_close_yields_cancelled(self):
        with self._close_episode_concurrently("enrollment_closed"):
            outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.CANCELLED)
        self.assertEqual(outcome.detail, "episode_closed:enrollment_closed")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "cancelled")
        self.assertEqual(self.run.outcome_kind, "cancelled")
        self.assertIsNone(self.run.evaluation_id)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])


class TestProjectionCursorOrdering(OrchestrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        second_day = AS_OF + timedelta(days=1)
        self.manifest_1, self.daily_rows = self._seed_manifest(AS_OF, revision=1)
        # Extend the shared daily-row pool by exactly the one additional day
        # the second (later) evaluation's shifted window needs.
        extra_day = self.daily_rows_extra = _daily_source(
            self.ws.id, self.mode, self.episode.id, second_day, revision=1,
        )
        self.session.add(extra_day)
        self.session.flush()
        combined_rows = dict(self.daily_rows)
        combined_rows[second_day] = extra_day

        self.manifest_2, _ = self._seed_manifest(
            second_day, revision=2, daily_rows=combined_rows,
            current_start=second_day - timedelta(days=6), current_end=second_day + timedelta(days=1),
            baseline_start=second_day - timedelta(days=13), baseline_end=second_day - timedelta(days=6),
        )
        self.run_1 = self._seed_run(self.manifest_1, AS_OF, revision=1, freshness_hour=4)
        self.run_2 = self._seed_run(self.manifest_2, second_day, revision=2, freshness_hour=5)
        self.second_day = second_day

    def test_later_cursor_wins_and_earlier_rerun_does_not_regress(self):
        outcome_1 = run_scale_evaluation(self.session, self.context, self.run_1.id)
        self.assertEqual(outcome_1.kind, RunOutcomeKind.EVALUATED)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.projection_as_of_local_day, AS_OF)
        self.assertEqual(self.episode.projection_revision, 1)

        outcome_2 = run_scale_evaluation(self.session, self.context, self.run_2.id)
        self.assertEqual(outcome_2.kind, RunOutcomeKind.EVALUATED)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.projection_as_of_local_day, self.second_day)
        self.assertEqual(self.episode.projection_source_revision, 2)
        self.assertEqual(self.episode.projection_revision, 2)
        self.assertEqual(self.episode.accepted_evaluation_id, outcome_2.evaluation_id)

        projection = self.session.execute(
            select(ScaleEpisodeProjectionRecord).filter_by(episode_id=self.episode.id)
        ).scalar_one()
        self.assertEqual(projection.evaluation_id, outcome_2.evaluation_id)
        self.assertEqual(projection.revision, 2)

        # Re-running the first (now-stale) run again must not regress the
        # projection cursor.
        second_run_of_day_1 = _run(
            self.ws.id, self.mode, self.episode.id, self.enrollment.id, self.manifest_1, AS_OF,
            policy_id=self.policy.id, trigger_kind="live", revision=1,
        )
        second_run_of_day_1.freshness_reference_at = datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc)
        second_run_of_day_1.semantic_event_sequence = 1  # distinct semantic key
        self.session.add(second_run_of_day_1)
        self.session.flush()

        outcome_3 = run_scale_evaluation(self.session, self.context, second_run_of_day_1.id)
        # Day 1's evidence is byte-identical regardless of semantic sequence,
        # so this reseals to the same content and reuses the same immutable
        # Evaluation fingerprint -- it is `already_evaluated`, not a fresh
        # `evaluated` result.
        self.assertEqual(outcome_3.kind, RunOutcomeKind.ALREADY_EVALUATED)
        self.assertEqual(outcome_3.evaluation_id, outcome_1.evaluation_id)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.projection_as_of_local_day, self.second_day)
        self.assertEqual(self.episode.projection_revision, 2)
        self.assertEqual(self.episode.accepted_evaluation_id, outcome_2.evaluation_id)


class TestSevenDayNoDeliveryClosure(OrchestrationTestBase):
    """Spec §3.7: seven consecutive complete zero-delivery days close the
    Episode and pause the Enrollment. Days D1..D7 are each zero-delivery;
    the 13 days before D1 deliver normally so every intermediate evaluation
    has a well-formed current/baseline window."""

    def setUp(self) -> None:
        super().setUp()
        self.d1 = AS_OF
        self.days = [self.d1 + timedelta(days=i) for i in range(7)]
        pool_start = self.d1 - timedelta(days=13)
        pool_end = self.days[-1]

        self.daily_pool: dict[date, ScaleDailyMetricSourceRecord] = {}
        d = pool_start
        while d <= pool_end:
            if d in self.days:
                row = _daily_source(self.ws.id, self.mode, self.episode.id, d, revision=1)
                row.delivered = False
                row.spend = Decimal("0")
                row.impressions = 0
                row.link_clicks = 0
                row.purchases = 0
                row.purchase_value = Decimal("0")
            else:
                row = _daily_source(self.ws.id, self.mode, self.episode.id, d, revision=1)
            self.daily_pool[d] = row
            d += timedelta(days=1)
        self.session.add_all(self.daily_pool.values())
        self.session.flush()

    def _evaluate_day(self, index: int) -> ScaleRunOutcome:
        as_of = self.days[index]
        manifest, _ = self._seed_manifest(
            as_of, revision=index + 1, daily_rows=self.daily_pool,
            current_start=as_of - timedelta(days=6), current_end=as_of + timedelta(days=1),
            baseline_start=as_of - timedelta(days=13), baseline_end=as_of - timedelta(days=6),
        )
        run = self._seed_run(manifest, as_of, revision=index + 1, freshness_hour=4 + index)
        return run_scale_evaluation(self.session, self.context, run.id)

    def test_seventh_zero_delivery_day_closes_episode_and_pauses_enrollment(self):
        outcomes = [self._evaluate_day(i) for i in range(7)]

        for outcome in outcomes:
            self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)

        last_evaluation = self.session.get(ScaleEvaluationRecord, outcomes[-1].evaluation_id)
        no_delivery_counter = self.session.execute(
            select(ScaleEvaluationStreakCounterRecord).filter_by(
                evaluation_id=last_evaluation.id, signal_key="no_delivery_complete_day",
            )
        ).scalar_one()
        self.assertGreaterEqual(no_delivery_counter.consecutive_hits, 7)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.phase, "closed")
        self.assertEqual(self.episode.closure_reason, "no_delivery_7_complete_days")

        self.session.refresh(self.enrollment)
        self.assertEqual(self.enrollment.lifecycle, "paused")
        self.assertEqual(self.enrollment.transition_reason, "no_delivery_7_complete_days")

        episode_events = self.session.execute(
            select(ScaleEpisodeEventRecord).filter_by(
                episode_id=self.episode.id, closure_reason="no_delivery_7_complete_days",
            )
        ).scalars().all()
        self.assertEqual(len(episode_events), 1)


class TestUpstreamWithdrawnBeforePhaseA(OrchestrationTestBase):
    """spec §3.9: upstream withdrawal may already have closed the Enrollment
    before this run is ever resumed at all (as opposed to
    ``TestUpstreamWithdrawalDuringPhaseC``'s concurrent-race scenario)."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)
        self.enrollment.lifecycle = "closed"
        self.enrollment.transition_reason = "upstream_withdrawal"
        self.session.flush()

    def test_terminates_blocked_with_the_sealed_snapshot(self):
        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.BLOCKED)
        self.assertEqual(outcome.detail, "upstream_withdrawal")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "blocked")
        self.assertEqual(self.run.outcome_kind, "blocked")
        # `seal_snapshot()` itself does not recheck upstream eligibility
        # (Task 10 scoping); the run's own CHECK constraint requires a
        # `snapshot_id` on any non-`requested` state, so this module checks
        # eligibility after sealing succeeds -- the sealed snapshot is real,
        # valid evidence even though the run never goes on to evaluate it.
        self.assertIsNotNone(self.run.snapshot_id)
        self.assertIsNone(self.run.evaluation_id)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])

        self.session.refresh(self.episode)
        self.assertIsNone(self.episode.accepted_evaluation_id)
        self.assertEqual(self.episode.phase, "collecting")
        self.assertIsNone(self.episode.phase_run_id)


class TestTenantIsolation(OrchestrationTestBase):
    """CLAUDE.md: negative tenant tests are required whenever data or
    commands are Workspace-scoped."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

        self.other_ws = WorkspaceRecord(id=new_id("ws"), name="Other WS")
        self.session.add(self.other_ws)
        self.session.flush()

    def test_run_scale_evaluation_rejects_cross_workspace_context(self):
        other_context = TenantContext(
            workspace_id=self.other_ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )
        with self.assertRaises(ScaleOrchestrationError) as ctx:
            run_scale_evaluation(self.session, other_context, self.run.id)
        self.assertEqual(ctx.exception.code, "run_not_found")

        # The run remains completely untouched under the legitimate tenant.
        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "requested")
        self.assertIsNone(self.run.snapshot_id)
        self.assertIsNone(self.run.outcome_kind)

    def test_run_scale_evaluation_rejects_cross_meta_mode_context(self):
        other_mode_context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode="customer_sandbox",
        )
        with self.assertRaises(ScaleOrchestrationError) as ctx:
            run_scale_evaluation(self.session, other_mode_context, self.run.id)
        self.assertEqual(ctx.exception.code, "run_not_found")

    def test_request_scale_evaluation_rejects_cross_workspace_context(self):
        other_context = TenantContext(
            workspace_id=self.other_ws.id, role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )
        request = ScaleEvaluationRequest(
            enrollment_id=self.enrollment.id,
            episode_id=self.episode.id,
            target_local_day=AS_OF,
            trigger_kind="backfill",
            source_manifest_id=self.manifest.id,
            source_manifest_hash=self.manifest.canonical_hash,
            source_revision=1,
            freshness_reference_at=datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc),
            policy_version_id=self.policy.id,
            algorithm_version="beta-v1",
        )
        with self.assertRaises(ScaleOrchestrationError) as ctx:
            request_scale_evaluation(self.session, other_context, request)
        self.assertEqual(ctx.exception.code, "enrollment_not_found")

        # No run leaked into either tenant's scope.
        runs = self.session.execute(select(ScaleEvaluationRunRecord)).scalars().all()
        self.assertEqual(len(runs), 1)  # only the fixture's original run


class TestScaleOrchestrationErrors(OrchestrationTestBase):
    def test_run_not_found_raises(self):
        with self.assertRaises(ScaleOrchestrationError) as ctx:
            run_scale_evaluation(self.session, self.context, "sevr_missing")
        self.assertEqual(ctx.exception.code, "run_not_found")

    def test_request_requires_meta_mode(self):
        headless_context = TenantContext(
            workspace_id=self.ws.id, role="system", executor="system:worker", initiator_id="user_1",
        )
        request = ScaleEvaluationRequest(
            enrollment_id=self.enrollment.id,
            episode_id=self.episode.id,
            target_local_day=AS_OF,
            trigger_kind="live",
            source_manifest_id="sesm_x",
            source_manifest_hash="x" * 64,
            source_revision=1,
            freshness_reference_at=datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc),
            policy_version_id=self.policy.id,
            algorithm_version="beta-v1",
        )
        with self.assertRaises(ScaleOrchestrationError) as ctx:
            request_scale_evaluation(self.session, headless_context, request)
        self.assertEqual(ctx.exception.code, "meta_mode_required")


if __name__ == "__main__":
    unittest.main()
