"""A new Episode younger than the 14-day evidence window (Issue #57).

Contract: docs/product/scale-advice-semantics-contract.md §8 blocks only gaps
that the state history cannot explain, and §9.1 says an Episode short of 14
complete local days is `warming`. Days before the Episode's
`started_local_day` are explained by the Episode's own start, so they are not
`daily_slot_missing`.

These tests drive the production path: `run_scale_evaluation` ->
`seal_snapshot` -> `evaluate_scale` -> persistence, with a source manifest
that carries daily slots only for the Episode's own days (daily source rows
are Episode-scoped, so an ingestion that follows the contract has nothing to
put in a pre-Episode slot).
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from sqlalchemy import select

import app.scale.evidence as evidence_module
from app.scale._canonical import canonical_encode
from app.scale._contracts import (
    DailyMetricValues,
    DailyObservation,
    DataQualityItem,
    DataQualityLocator,
    InvalidScaleSnapshot,
)
from app.scale.evaluator import evaluate_scale
from app.scale.evidence import assemble_snapshot
from app.scale.models import (
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceLinkRecord,
    ScaleSnapshotQualityIssueRecord,
)
from app.scale.service import run_scale_evaluation
from app.scale.types import (
    DailyEvidenceState,
    DataQualityImpact,
    DataQualityIssueCode,
    EvidenceMaturity,
    MetricContractType,
    RunOutcomeKind,
    ScaleHealth,
    SignalState,
)
from tests import test_scale_evaluator as ev
from tests.test_scale_evidence import _daily_source, _delivery_source, _exact_source, _item, _manifest
from tests.test_scale_orchestration import AS_OF, OrchestrationTestBase

EPISODE_DAYS = 5
EPISODE_START = AS_OF - timedelta(days=EPISODE_DAYS - 1)
WINDOW_START = AS_OF - timedelta(days=13)
WINDOW_END = AS_OF + timedelta(days=1)


class YoungEpisodeBase(OrchestrationTestBase):
    """An Episode that started `EPISODE_DAYS` days before (and including)
    `AS_OF`, so 9 of the 14 window days precede it."""

    def setUp(self) -> None:
        super().setUp()
        self.episode.started_local_day = EPISODE_START
        self.session.flush()

    def _seed_young_manifest(
        self, *, as_of=AS_OF, revision=1, in_episode_days=None, daily_rows=None,
        pre_episode_item_state=None,
    ):
        """A `complete` manifest for `as_of` whose daily slots cover
        `in_episode_days` (default: every day from the Episode's start to
        `as_of`), or exactly `daily_rows` (`local_day -> row`) when given.
        `pre_episode_item_state`, when given, also adds a slot in that state
        for every window day before the Episode started -- the shape an
        ingestion that plans the whole 14-day window would write."""
        window_start = as_of - timedelta(days=13)
        window_end = as_of + timedelta(days=1)
        if daily_rows is None:
            if in_episode_days is None:
                in_episode_days = [
                    EPISODE_START + timedelta(days=i) for i in range((as_of - EPISODE_START).days + 1)
                ]
            daily_rows = {
                day: _daily_source(self.ws.id, self.mode, self.episode.id, day, revision=revision)
                for day in in_episode_days
            }
            self.session.add_all(daily_rows.values())

        current_start = as_of - timedelta(days=6)
        exact_rows = [
            (_exact_source(self.ws.id, self.mode, self.episode.id, role, start, end, revision=revision), role)
            for role, start, end in (
                ("current_7d", current_start, window_end),
                ("baseline_7d", window_start, current_start),
                ("rolling_14d", window_start, window_end),
            )
        ]
        self.session.add_all([row for row, _ in exact_rows])
        delivery = _delivery_source(self.ws.id, self.mode, self.episode.id, revision=revision)
        self.session.add(delivery)
        self.session.flush()

        manifest = _manifest(self.ws.id, self.mode, self.episode.id, as_of, revision=revision)
        self.session.add(manifest)
        self.session.flush()

        items = [
            _item(self.ws.id, self.mode, manifest.id, "daily", row.id, local_day=day)
            for day, row in daily_rows.items()
        ]
        if pre_episode_item_state is not None:
            day = window_start
            while day < EPISODE_START:
                items.append(
                    _item(
                        self.ws.id, self.mode, manifest.id, "daily", None,
                        local_day=day, state=pre_episode_item_state,
                    )
                )
                day += timedelta(days=1)
        for row, role in exact_rows:
            items.append(_item(self.ws.id, self.mode, manifest.id, "exact_window", row.id, window_role=role))
        items.append(_item(self.ws.id, self.mode, manifest.id, "delivery_configuration", delivery.id))
        self.session.add_all(items)
        self.session.flush()
        return manifest

    def _evaluate(self, manifest):
        run = self._seed_run(manifest, AS_OF)
        outcome = run_scale_evaluation(self.session, self.context, run.id)
        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        evaluation = self.session.get(ScaleEvaluationRecord, outcome.evaluation_id)
        snapshot = self.session.get(ScaleEvidenceSnapshotRecord, evaluation.snapshot_id)
        return evaluation, snapshot

    def _quality_codes(self, snapshot_id):
        return [
            (row.code, row.local_day, row.is_blocking)
            for row in self.session.execute(
                select(ScaleSnapshotQualityIssueRecord).filter_by(snapshot_id=snapshot_id)
            ).scalars()
        ]


class TestYoungEpisodeIsWarming(YoungEpisodeBase):
    def test_days_before_the_episode_started_are_not_missing(self):
        evaluation, snapshot = self._evaluate(self._seed_young_manifest())

        self.assertEqual(self._quality_codes(snapshot.id), [])
        self.assertEqual(evaluation.data_quality_state, "pass")
        self.assertEqual(evaluation.maturity, "warming")
        self.assertIsNone(evaluation.health)
        self.assertEqual(evaluation.health_reason, "episode_window_incomplete")
        self.assertEqual(snapshot.complete_local_days_in_episode, EPISODE_DAYS)

        assembled = assemble_snapshot(self.session, self.context, snapshot.id)
        states = [obs.evidence_state for obs in (*assembled.baseline_7d.daily, *assembled.current_7d.daily)]
        self.assertEqual(
            states,
            [DailyEvidenceState.BEFORE_EPISODE] * (14 - EPISODE_DAYS)
            + [DailyEvidenceState.PRESENT] * EPISODE_DAYS,
        )

    def test_pre_episode_slots_the_manifest_reports_missing_are_still_explained(self):
        evaluation, snapshot = self._evaluate(
            self._seed_young_manifest(pre_episode_item_state="missing")
        )

        self.assertEqual(self._quality_codes(snapshot.id), [])
        self.assertEqual(evaluation.maturity, "warming")

    def test_no_advice_while_warming(self):
        evaluation, _ = self._evaluate(self._seed_young_manifest())

        self.assertEqual(evaluation.canonical_result_payload["advice_candidates"], [])


class TestGapsAfterTheEpisodeStartStillBlock(YoungEpisodeBase):
    def test_missing_day_after_the_start_blocks_with_daily_slot_missing(self):
        gap = EPISODE_START + timedelta(days=2)
        days = [EPISODE_START + timedelta(days=i) for i in range(EPISODE_DAYS) if EPISODE_START + timedelta(days=i) != gap]
        evaluation, snapshot = self._evaluate(self._seed_young_manifest(in_episode_days=days))

        self.assertEqual(self._quality_codes(snapshot.id), [("daily_slot_missing", gap, True)])
        self.assertEqual(evaluation.data_quality_state, "blocked")
        self.assertEqual(evaluation.maturity, "insufficient")

    def test_missing_start_day_itself_blocks(self):
        days = [EPISODE_START + timedelta(days=i) for i in range(1, EPISODE_DAYS)]
        evaluation, snapshot = self._evaluate(self._seed_young_manifest(in_episode_days=days))

        self.assertEqual(
            self._quality_codes(snapshot.id), [("daily_slot_missing", EPISODE_START, True)]
        )
        self.assertEqual(evaluation.maturity, "insufficient")

    def test_failed_in_episode_slot_blocks_even_when_pre_episode_slots_are_present(self):
        manifest = self._seed_young_manifest(pre_episode_item_state="missing")
        last = self.session.execute(
            select(evidence_module.ScaleManifestItemRecord).filter_by(
                manifest_id=manifest.id, contract_type="daily", local_day=AS_OF,
            )
        ).scalar_one()
        last.state = "failed"
        last.source_record_id = None
        self.session.flush()

        evaluation, snapshot = self._evaluate(manifest)

        self.assertEqual(self._quality_codes(snapshot.id), [("daily_slot_missing", AS_OF, True)])
        self.assertEqual(evaluation.maturity, "insufficient")


class TestYoungEpisodeReplay(YoungEpisodeBase):
    def test_sealed_snapshot_reassembles_and_reevaluates_byte_identically(self):
        evaluation, snapshot = self._evaluate(self._seed_young_manifest())

        first = assemble_snapshot(self.session, self.context, snapshot.id)
        second = assemble_snapshot(self.session, self.context, snapshot.id)
        self.assertEqual(first.canonical_hash, snapshot.canonical_hash)

        result_a = evaluate_scale(first)
        result_b = evaluate_scale(second)
        self.assertEqual(canonical_encode(result_a), canonical_encode(result_b))
        self.assertEqual(result_a.result_hash, evaluation.result_hash)

    def test_pre_episode_days_have_no_source_links(self):
        _, snapshot = self._evaluate(self._seed_young_manifest(pre_episode_item_state="missing"))

        daily_link_days = sorted(
            link.local_day
            for link in self.session.execute(
                select(ScaleEvidenceSourceLinkRecord).filter_by(snapshot_id=snapshot.id, contract_type="daily")
            ).scalars()
        )
        self.assertEqual(
            daily_link_days, [EPISODE_START + timedelta(days=i) for i in range(EPISODE_DAYS)]
        )

    def test_v1_snapshot_keeps_its_sealed_missing_days(self):
        """A snapshot sealed before this change (schema v1) recorded
        pre-Episode days as missing and blocking. It must still assemble to
        the same canonical hash and re-evaluate to the same `insufficient`
        result; sealed history is not reinterpreted."""
        with mock.patch.object(evidence_module, "CANONICAL_SCHEMA_VERSION", "scale-snapshot-v1"):
            evaluation, snapshot = self._evaluate(self._seed_young_manifest())

        self.assertEqual(snapshot.canonical_schema_version, "scale-snapshot-v1")
        self.assertEqual(evaluation.maturity, "insufficient")
        missing_days = [day for code, day, _ in self._quality_codes(snapshot.id) if code == "daily_slot_missing"]
        self.assertEqual(len(missing_days), 14 - EPISODE_DAYS)

        assembled = assemble_snapshot(self.session, self.context, snapshot.id)
        self.assertEqual(assembled.canonical_hash, snapshot.canonical_hash)
        self.assertNotIn(
            DailyEvidenceState.BEFORE_EPISODE,
            {obs.evidence_state for obs in (*assembled.baseline_7d.daily, *assembled.current_7d.daily)},
        )
        self.assertEqual(evaluate_scale(assembled).result_hash, evaluation.result_hash)


class TestAsOfBeforeEpisodeStart(YoungEpisodeBase):
    def test_run_for_a_day_before_the_episode_is_refused_without_a_snapshot(self):
        manifest = self._seed_young_manifest()
        self.episode.started_local_day = AS_OF + timedelta(days=1)
        run = self._seed_run(manifest, AS_OF)
        # Commit the fixture so the refused run's rollback only discards the
        # sealing attempt (as in TestManifestBlockedLeavesRunUntouched).
        self.session.commit()

        outcome = run_scale_evaluation(self.session, self.context, run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.FAILED_INTEGRITY)
        self.assertEqual(outcome.detail, "as_of_before_episode_start")
        refreshed = self.session.get(ScaleEvaluationRunRecord, run.id)
        self.assertEqual(refreshed.run_state, "requested")
        self.assertIsNone(refreshed.snapshot_id)
        self.assertEqual(self.session.execute(select(ScaleEvidenceSnapshotRecord)).all(), [])


class TestYoungEpisodeSevenDayNoDelivery(YoungEpisodeBase):
    """Contract §4.3: seven consecutive complete days without delivery close
    the Episode. Before Issue #57 a young Episode could never reach this,
    because every evaluation in its first 14 days was quality-blocked and
    reset the streak."""

    def test_episode_with_no_delivery_from_its_first_day_closes_on_day_seven(self):
        start = EPISODE_START
        days = [start + timedelta(days=i) for i in range(7)]
        pool = {}
        for day in days:
            row = _daily_source(self.ws.id, self.mode, self.episode.id, day)
            row.delivered = False
            row.spend = Decimal("0")
            row.impressions = 0
            row.link_clicks = 0
            row.purchases = 0
            row.purchase_value = Decimal("0")
            pool[day] = row
        self.session.add_all(pool.values())
        self.session.flush()

        outcomes = []
        for index, as_of in enumerate(days):
            manifest = self._seed_young_manifest(
                as_of=as_of, revision=index + 1,
                daily_rows={d: r for d, r in pool.items() if d <= as_of},
            )
            run = self._seed_run(manifest, as_of, revision=index + 1, freshness_hour=4 + index)
            outcomes.append(run_scale_evaluation(self.session, self.context, run.id))

        for outcome in outcomes:
            self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        first = self.session.get(ScaleEvaluationRecord, outcomes[0].evaluation_id)
        self.assertEqual(first.maturity, "warming")
        self.assertEqual(first.health, "not_delivering")

        counter = self.session.execute(
            select(ScaleEvaluationStreakCounterRecord).filter_by(
                evaluation_id=outcomes[-1].evaluation_id, signal_key="no_delivery_complete_day",
            )
        ).scalar_one()
        self.assertEqual(counter.consecutive_hits, 7)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.phase, "closed")
        self.assertEqual(self.episode.closure_reason, "no_delivery_7_complete_days")
        self.session.refresh(self.enrollment)
        self.assertEqual(self.enrollment.lifecycle, "paused")


# ---------------------------------------------------------------------------
# Pure evaluator: `before_episode` days (no database)
# ---------------------------------------------------------------------------


def _before(day):
    return DailyObservation(local_date=day, evidence_state=DailyEvidenceState.BEFORE_EPISODE, metrics=None)


def _young_snapshot(days_in_episode, *, delivered=True, **overrides):
    """The evaluator fixture's 14-day span with its first
    `14 - days_in_episode` days before the Episode started. Episode days
    carry purchases and spend on both sides, so the economic signals would
    reach the day-block bootstrap if this path ran it."""
    span = []
    for i in range(14):
        day = ev.BASELINE_START + timedelta(days=i)
        if i < 14 - days_in_episode:
            span.append(_before(day))
        elif delivered:
            span.append(ev._day(day, 2000, 200, 2, 20, 60))
        else:
            span.append(ev._day(day, 0, 0, 0, 0, 0, delivered=False))
    return ev._snapshot(
        baseline_daily=tuple(span[:7]),
        current_daily=tuple(span[7:]),
        complete_days=days_in_episode,
        active_days=days_in_episode if delivered else 0,
        **overrides,
    )


class TestEvaluatorBeforeEpisodeDays(unittest.TestCase):
    def test_partial_window_is_warming_without_comparisons_or_advice(self):
        result = evaluate_scale(_young_snapshot(10))

        self.assertEqual(result.maturity, EvidenceMaturity.WARMING)
        self.assertIsNone(result.health)
        self.assertEqual(result.health_reason, "episode_window_incomplete")
        self.assertEqual(result.advice_candidates, ())
        by_id = {signal.signal_id: signal for signal in result.signals}
        for key in (
            "frequency_high", "frequency_growth", "ctr_decline", "cvr_decline",
            "cpm_increase", "delivery_decline", "cpa_worsening", "roas_worsening",
        ):
            self.assertEqual(by_id[key].state, SignalState.UNAVAILABLE, key)
            self.assertEqual(by_id[key].reason_code, "episode_window_incomplete", key)
        self.assertEqual(by_id["configuration_drift"].state, SignalState.MISS)
        self.assertEqual(by_id["no_delivery_complete_day"].state, SignalState.MISS)

    def test_day_count_does_not_override_a_partial_window(self):
        """Even a snapshot that claims enough complete days stays warming
        while its window opens before the Episode."""
        result = evaluate_scale(replace(_young_snapshot(13), complete_local_days_in_episode=20))

        self.assertEqual(result.maturity, EvidenceMaturity.WARMING)

    def test_no_impressions_since_the_start_is_not_delivering(self):
        result = evaluate_scale(_young_snapshot(3, delivered=False))

        self.assertEqual(result.maturity, EvidenceMaturity.WARMING)
        self.assertEqual(result.health, ScaleHealth.NOT_DELIVERING)
        by_id = {signal.signal_id: signal for signal in result.signals}
        self.assertEqual(by_id["no_delivery_complete_day"].state, SignalState.HIT)

    def test_blocking_issue_still_wins_over_warming(self):
        gap = ev.AS_OF
        issue = DataQualityItem(
            code=DataQualityIssueCode.DAILY_SLOT_MISSING,
            impact=DataQualityImpact.BLOCKING,
            locator=DataQualityLocator(
                contract_type=MetricContractType.DAILY, window_role=None, local_date=gap,
                breakdown_type=None, segment_key=None, source_evidence_hash="",
            ),
            details=(),
        )
        snapshot = _young_snapshot(5)
        current = snapshot.current_7d.daily[:-1] + (
            DailyObservation(local_date=gap, evidence_state=DailyEvidenceState.MISSING, metrics=None),
        )
        snapshot = replace(
            snapshot,
            current_7d=replace(snapshot.current_7d, daily=current),
            data_quality_issues=(issue,),
        )

        result = evaluate_scale(snapshot)

        self.assertEqual(result.maturity, EvidenceMaturity.INSUFFICIENT)
        self.assertEqual(result.health_reason, "quality_blocked")

    def test_replay_is_byte_identical(self):
        snapshot = _young_snapshot(10)

        self.assertEqual(canonical_encode(evaluate_scale(snapshot)), canonical_encode(evaluate_scale(snapshot)))

    def test_full_window_snapshots_are_unchanged(self):
        """A snapshot without `before_episode` days takes the existing
        full-window path."""
        result = evaluate_scale(ev._flat_snapshot())

        self.assertEqual(result.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(result.health, ScaleHealth.HEALTHY)
        self.assertNotIn(
            "episode_window_incomplete", {signal.reason_code for signal in result.signals}
        )


class TestEvaluatorRejectsMisplacedBeforeEpisodeDays(unittest.TestCase):
    def _assert_invalid(self, snapshot, code):
        with self.assertRaises(InvalidScaleSnapshot) as ctx:
            evaluate_scale(snapshot)
        self.assertEqual(ctx.exception.code, code)

    def test_before_episode_day_after_an_episode_day(self):
        snapshot = _young_snapshot(5)
        current = list(snapshot.current_7d.daily)
        current[-2] = _before(current[-2].local_date)
        snapshot = replace(snapshot, current_7d=replace(snapshot.current_7d, daily=tuple(current)))

        self._assert_invalid(snapshot, "invalid_before_episode_day")

    def test_every_day_before_the_episode(self):
        snapshot = _young_snapshot(1)
        current = snapshot.current_7d.daily[:-1] + (_before(snapshot.current_7d.daily[-1].local_date),)
        snapshot = replace(
            snapshot, current_7d=replace(snapshot.current_7d, daily=current),
            complete_local_days_in_episode=0, active_delivery_days_in_episode=0,
        )

        self._assert_invalid(snapshot, "invalid_before_episode_day")

    def test_before_episode_day_with_metrics(self):
        snapshot = _young_snapshot(5)
        baseline = list(snapshot.baseline_7d.daily)
        baseline[0] = DailyObservation(
            local_date=baseline[0].local_date,
            evidence_state=DailyEvidenceState.BEFORE_EPISODE,
            metrics=DailyMetricValues(
                impressions=1, link_clicks=0, purchases=Decimal(0), spend=Decimal(0),
                purchase_value=Decimal(0), delivered=True,
            ),
        )
        snapshot = replace(snapshot, baseline_7d=replace(snapshot.baseline_7d, daily=tuple(baseline)))

        self._assert_invalid(snapshot, "evidence_metrics_mismatch")
