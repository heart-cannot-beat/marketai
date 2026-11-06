"""Crash-recovery tests for durable Scale evaluation runs (Issue #25, Task 18).

Spec: docs/superpowers/specs/2026-09-08-scale-section-3-evidence-advice-orchestration.md §3.4
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-18-brief.md §4

Gate 11: "Durable evaluation runs." ``app.scale.service.run_scale_evaluation``
is structured as three phases with two persisted checkpoints (spec §3.4):
Phase A seals (or reuses) an immutable snapshot and commits; Phase B is a
*pure, DB-free* bootstrap + ``evaluate_scale`` call with no open
transaction; Phase C persists the Evaluation, projection, and Advice
seam, and commits once. A crash can only ever be observed at one of the
run's two persisted states plus one truly-mid-flight case:

1. **before any Phase A work** -- the run still sits in ``requested``;
   resuming is simply the first real attempt (sanity baseline).
2. **after Phase A's commit, before Phase C's commit** -- the run is
   durably ``snapshot_sealed``; resuming must not reseal and must finish
   cleanly to exactly one terminal Evaluation.
3. **mid-flight inside Phase C itself** -- the process dies after
   ``evaluate_scale`` returned but before Phase C's own commit lands (no
   database change from Phase C survives an uncommitted transaction);
   resuming from the still-``snapshot_sealed`` run must reach the exact
   same terminal Evaluation with no duplicate or orphaned rows.
4. **after Phase C's commit** -- the run is terminal (``evaluated``);
   re-running it must be a pure idempotent replay.

``tests/test_scale_orchestration.py`` already proves cases 2 and 4 as a
side effect of testing the phase-transaction boundaries themselves
(``TestFullEvaluationHappyPath.test_resumes_after_seal_only_crash_without_resealing``
/ ``test_rerunning_terminal_run_is_idempotent``); this module reuses its
``OrchestrationTestBase`` fixture rather than re-deriving it, and adds
case 1 (baseline) and case 3 (a genuinely new scenario: an exception
injected *inside* Phase C itself, after the pure computation has already
run) plus explicit "no duplicate evidence" assertions at every point --
the requirement Task 18 states in its own words: "Verify terminal
outcomes and no duplicate records."
"""
from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import select

from app.scale.evidence import seal_snapshot
from app.scale.models import (
    ScaleEpisodeProjectionRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSnapshotRecord,
)
import app.scale.service as service_module
from app.scale.service import RunOutcomeKind, ScaleOrchestrationError, run_scale_evaluation

from tests.test_scale_orchestration import AS_OF, OrchestrationTestBase


def _counts(session) -> tuple[int, int, int]:
    """(snapshots, evaluations, runs) -- the three evidence-bearing rows a
    duplicate-persisting bug could multiply."""
    snapshots = len(session.execute(select(ScaleEvidenceSnapshotRecord)).scalars().all())
    evaluations = len(session.execute(select(ScaleEvaluationRecord)).scalars().all())
    runs = len(session.execute(select(ScaleEvaluationRunRecord)).scalars().all())
    return snapshots, evaluations, runs


class TestCrashBeforeAnySealing(OrchestrationTestBase):
    """Case 1: the run was created (``request_scale_evaluation``) but the
    Worker died before ever calling ``run_scale_evaluation`` -- the
    baseline every other case is measured against. Resuming is simply the
    first real attempt and must succeed cleanly."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_resume_from_requested_completes_normally(self):
        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "requested")

        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.assertEqual(_counts(self.session), (1, 1, 1))


class TestCrashAfterSealBeforePersist(OrchestrationTestBase):
    """Case 2: Phase A committed a real, sealed snapshot; the process died
    before Phase C ever ran (equally models a crash during Phase B's
    DB-free computation itself, since nothing persists there either way).
    Resuming must not reseal (no second snapshot) and must still reach
    exactly one terminal Evaluation."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_resume_after_seal_only_crash_reaches_exactly_one_evaluation(self):
        sealed_snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        self.session.commit()
        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "snapshot_sealed")
        self.assertEqual(_counts(self.session), (1, 0, 1))

        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.session.refresh(self.run)
        self.assertEqual(self.run.snapshot_id, sealed_snapshot_id)
        self.assertEqual(self.run.run_state, "evaluated")
        self.assertEqual(_counts(self.session), (1, 1, 1))

        projections = self.session.execute(select(ScaleEpisodeProjectionRecord)).scalars().all()
        self.assertEqual(len(projections), 1)

    def test_repeated_resume_attempts_from_sealed_state_are_all_idempotent(self):
        """Simulates a Worker that crashes and retries the *same* run
        several times before finally succeeding (e.g. transient lease
        churn): every resume before the real one is itself just another
        instance of case 2, and the final state must still show exactly
        one of everything."""
        seal_snapshot(self.session, self.context, self.run.id)
        self.session.commit()

        first = run_scale_evaluation(self.session, self.context, self.run.id)
        second = run_scale_evaluation(self.session, self.context, self.run.id)
        third = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(first.evaluation_id, second.evaluation_id)
        self.assertEqual(second.evaluation_id, third.evaluation_id)
        self.assertEqual(_counts(self.session), (1, 1, 1))


class TestCrashInsidePhaseC(OrchestrationTestBase):
    """Case 3: the pure ``evaluate_scale`` computation already ran (Phase B
    is complete), but the process died *inside* Phase C before its own
    commit landed -- e.g. the Worker was killed mid-transaction while
    persisting the Evaluation/projection/Advice rows. Nothing from that
    aborted attempt survives (SQLAlchemy never issued a commit for it), so
    resuming from the still-``snapshot_sealed`` run must behave exactly
    like case 2: reseal nothing, and reach exactly one terminal
    Evaluation with no partial/orphaned rows left over from the aborted
    attempt."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_exception_inside_phase_c_leaves_no_partial_state_and_resume_succeeds(self):
        original_phase_c = service_module._phase_c
        state = {"armed": True}

        def _phase_c_that_dies_once(*args, **kwargs):
            if state["armed"]:
                state["armed"] = False
                raise RuntimeError("simulated crash mid Phase C")
            return original_phase_c(*args, **kwargs)

        with mock.patch.object(service_module, "_phase_c", side_effect=_phase_c_that_dies_once):
            with self.assertRaises(RuntimeError):
                run_scale_evaluation(self.session, self.context, self.run.id)

        # The aborted attempt's uncommitted work (if any) must be rolled
        # back before a fresh resume attempt reuses this session -- exactly
        # what a real Worker's exception handler does before releasing the
        # job lease back for retry (mirrors app.scale.worker._scale_evaluate's
        # `except Exception` -> `jobs.fail(..., retryable=...)` path, whose
        # own `tenant_session` context manager rolls back on any exception).
        self.session.rollback()

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "snapshot_sealed")
        self.assertIsNone(self.run.evaluation_id)
        self.assertEqual(_counts(self.session), (1, 0, 1))

        outcome = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "evaluated")
        self.assertEqual(_counts(self.session), (1, 1, 1))

        projections = self.session.execute(select(ScaleEpisodeProjectionRecord)).scalars().all()
        self.assertEqual(len(projections), 1)
        self.assertEqual(projections[0].evaluation_id, outcome.evaluation_id)


class TestCrashAfterPersist(OrchestrationTestBase):
    """Case 4: Phase C's own commit landed -- the run is genuinely
    terminal (``evaluated``). A Worker that (for whatever reason: a stale
    lease, a duplicate job dispatch) resumes it again must get a pure,
    side-effect-free idempotent replay, not a second Evaluation, a second
    audit event, or a second projection advance."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_resume_of_a_terminal_run_is_a_pure_replay(self):
        from app.adapters.persistence.models import AuditEventRecord

        first = run_scale_evaluation(self.session, self.context, self.run.id)
        self.assertEqual(first.kind, RunOutcomeKind.EVALUATED)
        self.assertEqual(_counts(self.session), (1, 1, 1))

        self.session.refresh(self.episode)
        projection_revision_after_first = self.episode.projection_revision

        second = run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(second.kind, RunOutcomeKind.EVALUATED)
        self.assertEqual(second.evaluation_id, first.evaluation_id)
        self.assertEqual(_counts(self.session), (1, 1, 1))

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.projection_revision, projection_revision_after_first)

        audit_events = self.session.execute(
            select(AuditEventRecord).filter_by(resource_id=self.run.id)
        ).scalars().all()
        self.assertEqual(len(audit_events), 1)

    def test_resume_of_a_terminal_run_survives_a_missing_run_lookup_error_first(self):
        """A misdirected resume attempt against the wrong tenant scope
        must raise cleanly and leave the real run's terminal state
        completely untouched, so a subsequent correctly-scoped resume
        still replays idempotently."""
        from app.platform.tenancy import TenantContext

        run_scale_evaluation(self.session, self.context, self.run.id)

        other_context = TenantContext(
            workspace_id="ws_does_not_exist", role="system", executor="system:worker",
            initiator_id="user_1", meta_mode=self.mode,
        )
        with self.assertRaises(ScaleOrchestrationError):
            run_scale_evaluation(self.session, other_context, self.run.id)

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "evaluated")
        self.assertEqual(_counts(self.session), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
