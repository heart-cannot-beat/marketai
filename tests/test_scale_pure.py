"""Pure-function determinism gate for the Scale evaluator (Issue #25, Task 18).

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-18-brief.md §1

Gate 1: "Evaluator is pure, deterministic, versioned." ``tests/test_scale_evaluator.py``
already carries the Task 9 scenario/golden-vector fixture builders and a
``TestDeterminism`` smoke pair (repeated-call byte-identity, no input
mutation); this module reuses those exact builders -- rather than
duplicating ~250 lines of ``ScaleEvidenceSnapshot`` construction -- and adds
the cross-cutting proof Task 18 additionally requires:

* many repeated calls (not just two) on one snapshot instance are all
  byte-identical, including full dataclass equality, not only the hash;
* two *independently constructed* snapshot objects with identical field
  values (never the same Python object) still hash identically -- proving
  determinism is a property of the *content*, not of object identity or
  any cached/memoized state on the snapshot or the evaluator;
* calls interleaved across several distinct snapshots never let one
  evaluation's result leak into another's (no hidden module-level state);
* changing exactly one leaf field changes the result hash (sensitivity);
* a regression pin on ``_flat_snapshot()``'s exact result hash, so a
  future unintentional change to canonical encoding, rounding, or the
  bootstrap PRNG sequence fails loudly here instead of silently drifting.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from app.scale.evaluator import CURRENT_ALGORITHM_VERSION, evaluate_scale
from app.scale.types import EvidenceMaturity, ScaleHealth

from tests.test_scale_evaluator import (
    _flat_snapshot,
    _policy,
    _snapshot,
    _uniform_week,
    BASELINE_START,
    CURRENT_START,
)


class TestManyRepeatedCallsAreByteIdentical(unittest.TestCase):
    """Gate 1: not just "two calls agree" (already covered by
    ``test_scale_evaluator.py``) but that a long run of repeated calls on
    the same snapshot object never drifts -- e.g. from an evaluator bug
    that mutates shared state on its Nth invocation."""

    def test_thirty_repeated_calls_on_one_instance_are_identical(self):
        snapshot = _flat_snapshot()
        results = [evaluate_scale(snapshot) for _ in range(30)]
        first = results[0]
        for result in results[1:]:
            self.assertEqual(result.result_hash, first.result_hash)
            self.assertEqual(result, first)

    def test_repeated_calls_on_a_blocked_quality_snapshot_are_identical(self):
        from app.scale._contracts import DataQualityItem, DataQualityLocator
        from app.scale.types import DataQualityImpact, DataQualityIssueCode, MetricContractType, WindowRole

        issue = DataQualityItem(
            code=DataQualityIssueCode.DAILY_SLOT_MISSING,
            impact=DataQualityImpact.BLOCKING,
            locator=DataQualityLocator(
                contract_type=MetricContractType.DAILY,
                window_role=WindowRole.CURRENT_7D,
                local_date=CURRENT_START,
                breakdown_type=None,
                segment_key=None,
                source_evidence_hash="h" * 64,
            ),
            details=(),
        )
        snapshot = _flat_snapshot(data_quality_issues=(issue,))
        results = [evaluate_scale(snapshot) for _ in range(10)]
        for result in results[1:]:
            self.assertEqual(result.result_hash, results[0].result_hash)


class TestIndependentConstructionIsDeterministic(unittest.TestCase):
    """Determinism is a property of *content*, not of the particular
    Python object: two fresh, independently-built snapshots with identical
    field values (never the same instance, never sharing a cache) must
    still hash identically."""

    def test_two_independently_built_snapshots_hash_identically(self):
        snapshot_a = _flat_snapshot()
        snapshot_b = _flat_snapshot()
        self.assertIsNot(snapshot_a, snapshot_b)
        result_a = evaluate_scale(snapshot_a)
        result_b = evaluate_scale(snapshot_b)
        self.assertEqual(result_a.result_hash, result_b.result_hash)
        self.assertEqual(result_a, result_b)

    def test_interleaved_calls_across_distinct_snapshots_do_not_cross_contaminate(self):
        """Alternate evaluating two *different* snapshots many times; each
        snapshot's own result must stay stable throughout, proving no
        module-level cache keyed loosely (e.g. by id-only) or hidden
        mutable state leaks a result from one snapshot into another."""
        healthy = _flat_snapshot()
        warming = _flat_snapshot(complete_days=5, active_days=3)

        healthy_result = evaluate_scale(healthy)
        warming_result = evaluate_scale(warming)

        for _ in range(15):
            self.assertEqual(evaluate_scale(healthy).result_hash, healthy_result.result_hash)
            self.assertEqual(evaluate_scale(warming).result_hash, warming_result.result_hash)

        self.assertNotEqual(healthy_result.result_hash, warming_result.result_hash)
        self.assertEqual(healthy_result.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(warming_result.maturity, EvidenceMaturity.WARMING)


class TestDifferentSnapshotsProduceDifferentHashes(unittest.TestCase):
    """Sensitivity: a single-field change in the input must change the
    output hash -- otherwise the hash is not really binding the evidence
    it claims to represent."""

    def test_changing_one_days_impressions_changes_the_hash(self):
        baseline = _flat_snapshot()
        current_daily = list(baseline.current_7d.daily)
        first_day = current_daily[0]
        mutated_metrics = first_day.metrics.__class__(
            impressions=first_day.metrics.impressions + 1,
            link_clicks=first_day.metrics.link_clicks,
            purchases=first_day.metrics.purchases,
            spend=first_day.metrics.spend,
            purchase_value=first_day.metrics.purchase_value,
            delivered=first_day.metrics.delivered,
        )
        mutated_day = first_day.__class__(
            local_date=first_day.local_date,
            evidence_state=first_day.evidence_state,
            metrics=mutated_metrics,
        )
        current_daily[0] = mutated_day
        mutated_window = baseline.current_7d.__class__(
            role=baseline.current_7d.role,
            start_date=baseline.current_7d.start_date,
            end_date_exclusive=baseline.current_7d.end_date_exclusive,
            daily=tuple(current_daily),
        )
        import dataclasses

        mutated_snapshot = dataclasses.replace(baseline, current_7d=mutated_window)

        result_baseline = evaluate_scale(baseline)
        result_mutated = evaluate_scale(mutated_snapshot)
        self.assertNotEqual(result_baseline.result_hash, result_mutated.result_hash)

    def test_changing_policy_threshold_changes_the_hash(self):
        current_daily = _uniform_week(CURRENT_START, 2000, 200, 1, 20, 60)
        baseline_daily = _uniform_week(BASELINE_START, 2000, 200, 1, 20, 60)
        snap_a = _snapshot(
            policy=_policy(), current_daily=current_daily, baseline_daily=baseline_daily,
        )
        snap_b = _snapshot(
            policy=_policy(ctr_decline_threshold=Decimal("0.4")),
            current_daily=current_daily, baseline_daily=baseline_daily,
        )
        result_a = evaluate_scale(snap_a)
        result_b = evaluate_scale(snap_b)
        self.assertNotEqual(result_a.result_hash, result_b.result_hash)

    def test_flat_and_warming_snapshots_differ(self):
        healthy = evaluate_scale(_flat_snapshot())
        warming = evaluate_scale(_flat_snapshot(complete_days=5, active_days=3))
        self.assertNotEqual(healthy.result_hash, warming.result_hash)


class TestAlgorithmVersioning(unittest.TestCase):
    """Gate 1's "versioned" clause: the evaluator publishes its own
    algorithm version, and a snapshot's Policy must declare a version the
    registry actually recognizes (see ``TestRegistry`` in
    ``test_scale_evaluator.py`` for the rejection path)."""

    def test_current_algorithm_version_is_stable_literal(self):
        self.assertEqual(CURRENT_ALGORITHM_VERSION, "beta-v1")

    def test_result_carries_the_snapshot_algorithm_version(self):
        result = evaluate_scale(_flat_snapshot())
        # ScaleEvaluation.algorithm_version travels through from the Policy,
        # not from a separate evaluator-global constant, so a Policy pinned
        # to an older-but-still-registered version stays reproducible.
        self.assertEqual(result.algorithm_version, CURRENT_ALGORITHM_VERSION)


class TestGoldenVectorRegressionPin(unittest.TestCase):
    """A hard-coded regression pin on one canonical scenario's exact result
    hash (Task 9's `_flat_snapshot()` golden vector): any future change to
    canonical encoding, Decimal rounding, or the bootstrap PRNG sequence
    that alters this evaluation's byte-for-byte output must fail this test
    explicitly rather than drift silently. Recompute and update this
    literal deliberately if -- and only if -- the algorithm version is
    also bumped (a Policy/registry change, never a silent edit)."""

    def test_flat_snapshot_result_hash_is_pinned(self):
        result = evaluate_scale(_flat_snapshot())
        self.assertEqual(
            result.result_hash,
            "12e0d3bae29416f1b2c78df509df1b901f39e7887d3e6130e37c82b5ec108dab",
            "Golden-vector result hash drifted -- see class docstring before updating this pin.",
        )
        self.assertEqual(result.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(result.health, ScaleHealth.HEALTHY)


if __name__ == "__main__":
    unittest.main()
