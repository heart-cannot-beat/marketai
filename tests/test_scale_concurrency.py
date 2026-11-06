"""Race-condition tests for Scale orchestration and Enrollment lifecycle
(Issue #25, Task 18).

Spec: docs/superpowers/specs/2026-09-08-scale-section-3-evidence-advice-orchestration.md
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-18-brief.md §3

Every race below is reproduced *deterministically*, without real threads,
following the exact convention ``tests/test_localization_cas.py`` already
established for this codebase (see that module's own docstring for the
full rationale). The short version: ``app.scale.service._for_update`` and
``app.scale.enrollment._lock_enrollment``/``_lock_episode`` are documented
no-ops on SQLite (real row locking is PostgreSQL-only -- see
``tests/test_scale_migration.py::ScalePostgresConstraintTests`` and
``tests/test_scale_domain_pg.py`` for the partial-unique-index guarantee
that actually rejects a concurrent duplicate binding at the database
layer). SQLite additionally allows only one *writer* transaction at a
time, so a genuinely separate connection deliberately held open while a
second one tries to write would simply block forever with no thread free
to release it -- a real second connection is not safe to use here either.

Instead, each test below arms a ``before_cursor_execute`` hook on the
single shared connection that, the first time it sees the exact statement
the function under test issues at the race window's boundary, injects the
concurrent writer's effect via a raw SQL statement on that same
connection immediately before letting the intercepted statement proceed.
This reproduces the precise interleaving a genuine second transaction
would have produced -- deterministically, with no thread, no sleep, and
no flakiness.

Four scenarios (matching the task brief):

* ``TestActivationRace`` -- two Enrollments claiming the same remote
  binding; the second's own conflict-check must observe the first's
  concurrent activation and refuse.
* ``TestProjectionCASRace`` -- two evaluations for the same Episode,
  raced in *reverse* semantic order (the later-day run's cursor lands
  first); the earlier-day run's later-arriving projection attempt must
  not regress the cursor.
* ``TestConfigurationRolloverDuringEvaluation`` -- a configuration change
  is detected and rolls the Episode over while another run's Phase B
  (pure, DB-free) computation is already in flight.
* ``TestUpstreamWithdrawalDuringPhaseC`` -- the Enrollment closes while
  another run's Phase B computation is already in flight.

The latter two reuse the same "commit inside ``evaluate_scale``'s mocked
call" technique ``tests/test_scale_orchestration.py`` already introduced
for Phase B's genuinely DB-free window (spec §3.4): because Phase B holds
no open transaction at all, injecting a fully-committed concurrent write
there is not an approximation of concurrency -- it is exactly what a
second real transaction from a different process would have done in that
window. This module adds cross-cutting assertions
(``tests/test_scale_orchestration.py`` checks outcome classification; this
module additionally checks for duplicate/leaked evidence rows) rather
than re-deriving the scenarios from scratch.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.adapters.persistence.database import engine as db_engine, tenant_session
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord, new_id, utcnow
from app.scale.enrollment import EnrollmentLifecycleError, activate_enrollment
from app.scale.models import (
    ScaleEnrollmentEventRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeEventRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
)
from app.scale.service import RunOutcomeKind
import app.scale.service as service_module

from tests.test_scale_enrollment import ActivationFlowTestBase
from tests.test_scale_evidence import _daily_source
from tests.test_scale_orchestration import AS_OF, OrchestrationTestBase


# ---------------------------------------------------------------------------
# TestActivationRace
# ---------------------------------------------------------------------------


def _inject_concurrent_activation(table: str, sibling_enrollment_id: str):
    """A ``before_cursor_execute`` hook that, the first time it sees the
    *conflict-check* ``SELECT`` (the only statement in
    ``activate_enrollment`` whose ``WHERE`` clause references
    ``remote_binding_key`` -- ``_lock_enrollment``'s own ``SELECT`` filters
    only on ``id``, so this reliably distinguishes the two), flips a
    sibling Enrollment's `lifecycle` to ``active`` on the same connection
    immediately before letting the conflict check run. This reproduces
    "another activation committed in the exact window between our
    transaction's start and its own conflict check" without a second
    connection (see module docstring)."""
    state = {"armed": True}

    def _listener(conn, cursor, statement, parameters, context, executemany) -> None:
        del cursor, parameters, context, executemany
        if not state["armed"]:
            return
        upper = statement.strip().upper()
        if upper.startswith("SELECT") and "REMOTE_BINDING_KEY" in upper and table.upper() in upper:
            state["armed"] = False
            conn.execute(
                text(f"UPDATE {table} SET lifecycle = 'active' WHERE id = :id"),
                {"id": sibling_enrollment_id},
            )

    return _listener


class TestActivationRace(ActivationFlowTestBase):
    """Two Enrollments (siblings under one Workspace/mode, candidates
    against the exact same shared DeliveryTarget so they share a remote
    binding key) both attempt to activate. The one whose concurrent commit
    lands first must win; the other must observe the conflict and refuse --
    never both active for the same binding.

    Issue #86 Task 11: ``activate_enrollment`` now requires a genuine
    candidate-gated lineage chain plus a consumed
    ``ScaleActivationPreviewRecord``, so this race can no longer be
    reproduced against a lightweight in-memory engine (see
    ``ActivationFlowTestBase``) -- the ``before_cursor_execute`` hook is
    registered on the real shared ``engine``
    (``app.adapters.persistence.database``) every ``tenant_session`` call
    here uses; the same-connection interleaving trick (module docstring)
    is otherwise unchanged."""

    def test_second_activation_detects_concurrently_committed_conflict(self):
        enrollment_a_id, revision_a = self._draft()
        enrollment_b_id, revision_b = self._draft()
        preview_b_id = self._create_preview(enrollment_b_id, revision_b)

        listener = _inject_concurrent_activation("scale_enrollments", enrollment_a_id)
        event.listen(db_engine, "before_cursor_execute", listener)
        try:
            # Caught *inside* the `tenant_session` block, not around it: a
            # plain `EnrollmentLifecycleError` after a read-only conflict
            # check never poisons the session, so letting this block exit
            # normally lets `tenant_session` commit as usual -- durably
            # persisting the injected "concurrent" write on A exactly like
            # a genuinely separate, already-committed transaction would
            # have, rather than rolling it back along with B's own
            # (harmless, since B's attempt failed before writing anything
            # of its own) claim/preview-consumption rows.
            raised = None
            with tenant_session(self.ctx) as session:
                try:
                    activate_enrollment(
                        session, self.ctx,
                        enrollment_id=enrollment_b_id,
                        activation_preview_id=preview_b_id,
                        expected_revision=revision_b,
                        idempotency_key="activate-b",
                    )
                except EnrollmentLifecycleError as exc:
                    raised = exc
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)

        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "duplicate_binding")

        # B never advanced: no Episode was created, no lifecycle change,
        # and the injected "concurrent" activation on A is the only winner.
        with tenant_session(self.ctx) as session:
            b_reloaded = session.get(ScaleEnrollmentRecord, enrollment_b_id)
            self.assertEqual(b_reloaded.lifecycle, "draft")
            self.assertIsNone(b_reloaded.current_episode_id)

            episodes = session.execute(
                select(ScaleEpisodeRecord).filter_by(enrollment_id=enrollment_b_id)
            ).scalars().all()
            self.assertEqual(episodes, [])

            a_reloaded = session.get(ScaleEnrollmentRecord, enrollment_a_id)
            self.assertEqual(a_reloaded.lifecycle, "active")

    def test_race_resolves_the_same_way_regardless_of_which_sibling_is_called_second(self):
        """Symmetry check: activating A first (for real) then racing B
        against it (already exercised above) and activating B first then
        racing A against it must both land on exactly one active binding."""
        enrollment_a_id, revision_a = self._draft()
        enrollment_b_id, revision_b = self._draft()
        preview_a_id = self._create_preview(enrollment_a_id, revision_a)

        listener = _inject_concurrent_activation("scale_enrollments", enrollment_b_id)
        event.listen(db_engine, "before_cursor_execute", listener)
        try:
            raised = None
            with tenant_session(self.ctx) as session:
                try:
                    activate_enrollment(
                        session, self.ctx,
                        enrollment_id=enrollment_a_id,
                        activation_preview_id=preview_a_id,
                        expected_revision=revision_a,
                        idempotency_key="activate-a2",
                    )
                except EnrollmentLifecycleError as exc:
                    raised = exc
        finally:
            event.remove(db_engine, "before_cursor_execute", listener)

        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "duplicate_binding")

        with tenant_session(self.ctx) as session:
            active_count = sum(
                1
                for enrollment_id in (enrollment_a_id, enrollment_b_id)
                if session.get(ScaleEnrollmentRecord, enrollment_id).lifecycle == "active"
            )
        self.assertEqual(active_count, 1)


# ---------------------------------------------------------------------------
# TestProjectionCASRace
# ---------------------------------------------------------------------------


class TestProjectionCASRace(OrchestrationTestBase):
    """``tests/test_scale_orchestration.py::TestProjectionCursorOrdering``
    proves forward order (day 1 evaluated, then day 2): the later cursor
    wins and a stale rerun of day 1 does not regress it. This is the
    *reverse*-order race: day 2 (the semantically later cursor) is
    evaluated and accepted first; day 1's own evaluation -- computed and
    persisted as immutable evidence regardless -- must still fail to win
    the projection CAS once it reaches Phase C, because
    ``_decide_projection_eligibility`` compares cursors by content, not by
    arrival order."""

    def setUp(self) -> None:
        super().setUp()
        self.second_day = AS_OF + timedelta(days=1)
        self.manifest_1, self.daily_rows = self._seed_manifest(AS_OF, revision=1)
        extra_day = _daily_source(self.ws.id, self.mode, self.episode.id, self.second_day, revision=1)
        self.session.add(extra_day)
        self.session.flush()
        combined_rows = dict(self.daily_rows)
        combined_rows[self.second_day] = extra_day

        self.manifest_2, _ = self._seed_manifest(
            self.second_day, revision=2, daily_rows=combined_rows,
            current_start=self.second_day - timedelta(days=6), current_end=self.second_day + timedelta(days=1),
            baseline_start=self.second_day - timedelta(days=13), baseline_end=self.second_day - timedelta(days=6),
        )
        self.run_1 = self._seed_run(self.manifest_1, AS_OF, revision=1, freshness_hour=4)
        self.run_2 = self._seed_run(self.manifest_2, self.second_day, revision=2, freshness_hour=5)

    def test_later_day_evaluated_first_still_wins_when_earlier_day_arrives_second(self):
        outcome_2 = service_module.run_scale_evaluation(self.session, self.context, self.run_2.id)
        self.assertEqual(outcome_2.kind, RunOutcomeKind.EVALUATED)

        self.session.refresh(self.episode)
        self.assertEqual(self.episode.projection_as_of_local_day, self.second_day)
        self.assertEqual(self.episode.projection_revision, 1)
        self.assertEqual(self.episode.accepted_evaluation_id, outcome_2.evaluation_id)

        # The stale (day-1) run "arrives" second: it still evaluates and
        # persists its own immutable Evaluation record (it is real, sealed
        # evidence), but must not win the projection cursor.
        outcome_1 = service_module.run_scale_evaluation(self.session, self.context, self.run_1.id)
        self.assertEqual(outcome_1.kind, RunOutcomeKind.EVALUATED)
        self.assertNotEqual(outcome_1.evaluation_id, outcome_2.evaluation_id)

        self.session.refresh(self.episode)
        # Cursor is unchanged: still day 2, still revision 1 -- the day-1
        # evaluation did not regress it.
        self.assertEqual(self.episode.projection_as_of_local_day, self.second_day)
        self.assertEqual(self.episode.projection_revision, 1)
        self.assertEqual(self.episode.accepted_evaluation_id, outcome_2.evaluation_id)

        projection = self.session.execute(
            select(ScaleEpisodeProjectionRecord).filter_by(episode_id=self.episode.id)
        ).scalar_one()
        self.assertEqual(projection.evaluation_id, outcome_2.evaluation_id)
        self.assertEqual(projection.revision, 1)

        # Both Evaluations are real, distinct, immutable evidence rows --
        # the loser is not silently dropped, only not projected.
        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(len(evaluations), 2)
        self.assertEqual({e.id for e in evaluations}, {outcome_1.evaluation_id, outcome_2.evaluation_id})


# ---------------------------------------------------------------------------
# TestConfigurationRolloverDuringEvaluation
# ---------------------------------------------------------------------------


class TestConfigurationRolloverDuringEvaluation(OrchestrationTestBase):
    """A concurrent configuration rollover closes the Episode while THIS
    run's Phase B (pure, DB-free -- spec §3.4) is in flight.

    A genuinely *second live run* against the very same Episode cannot
    model this race: Phase A's own fencing (``seal_snapshot`` sets
    ``episode.phase_run_id`` to the sealing run's id and Phase C is the
    only step that clears it) means a second run's own Phase A would
    itself fail with ``failed_integrity`` while the first run still owns
    the fence -- proving that specific double-dispatch race is already
    closed by design, verified separately below. The *actual* rollover
    race this class targets is a different actor entirely: a completed
    evaluation for a *later* day, dispatched and fully resolved (Phase
    A/B/C, releasing the fence) before this run's Phase B finishes,
    closing the Episode out from under it -- reproduced the same way
    ``tests/test_scale_orchestration.py::TestPhaseCDistinguishesRolloverFromCancelled``
    does (direct Episode mutation standing in for that already-resolved
    concurrent writer), with this module's own "no duplicate/leaked
    records" assertions added on top."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

        second_day = AS_OF + timedelta(days=1)
        extra_day = _daily_source(self.ws.id, self.mode, self.episode.id, second_day, revision=1)
        self.session.add(extra_day)
        self.session.flush()
        combined_rows = dict(self.daily_rows)
        combined_rows[second_day] = extra_day
        self.rollover_manifest, _ = self._seed_manifest(
            second_day, revision=2, daily_rows=combined_rows,
            current_start=second_day - timedelta(days=6), current_end=second_day + timedelta(days=1),
            baseline_start=second_day - timedelta(days=13), baseline_end=second_day - timedelta(days=6),
            comparability_hash="d" * 64,
        )
        self.rollover_run = self._seed_run(self.rollover_manifest, second_day, revision=2, freshness_hour=5)

    def test_a_second_live_run_against_the_same_episode_is_fenced_not_raced(self):
        """Positive control for the docstring's claim: dispatching the
        second run for real, synchronously, while the first still holds
        the Episode's ``phase_run_id`` fence, fails integrity rather than
        silently interleaving -- exactly the corruption the fence exists
        to prevent."""
        first_outcome = service_module._phase_a(self.session, self.context, self.run.id)
        self.assertIsInstance(first_outcome, tuple)  # sealed, not yet terminal

        second_outcome = service_module.run_scale_evaluation(self.session, self.context, self.rollover_run.id)
        self.assertEqual(second_outcome.kind, RunOutcomeKind.FAILED_INTEGRITY)

        self.session.refresh(self.rollover_run)
        self.assertEqual(self.rollover_run.run_state, "failed_integrity")
        self.assertIsNone(self.rollover_run.evaluation_id)

    def test_concurrent_rollover_during_phase_b_is_detected_by_phase_c(self):
        original_evaluate = service_module.evaluate_scale

        def _evaluate_then_roll_over(snapshot):
            self.episode.phase = "closed"
            self.episode.closure_reason = "delivery_configuration_changed"
            self.episode.ended_at = utcnow()
            self.episode.revision += 1
            self.session.flush()
            self.session.commit()
            return original_evaluate(snapshot)

        with mock.patch.object(service_module, "evaluate_scale", side_effect=_evaluate_then_roll_over):
            outcome = service_module.run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.EPISODE_ROLLED_OVER)
        self.assertEqual(outcome.detail, "delivery_configuration_changed")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "episode_rolled_over")
        self.assertIsNone(self.run.evaluation_id)

        # No Evaluation was ever produced for the raced run.
        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])

        old_episode = self.session.get(ScaleEpisodeRecord, self.episode.id)
        self.assertEqual(old_episode.phase, "closed")
        self.assertEqual(old_episode.closure_reason, "delivery_configuration_changed")

        # Phase C's own rollover classification did not fabricate a second
        # Episode or touch the unrelated `rollover_run` fixture at all --
        # only `self.run` (the one actually raced) changed state.
        episodes = self.session.execute(select(ScaleEpisodeRecord)).scalars().all()
        self.assertEqual(len(episodes), 1)

        self.session.refresh(self.rollover_run)
        self.assertEqual(self.rollover_run.run_state, "requested")


# ---------------------------------------------------------------------------
# TestUpstreamWithdrawalDuringPhaseC
# ---------------------------------------------------------------------------


class TestUpstreamWithdrawalDuringPhaseC(OrchestrationTestBase):
    """The Enrollment closes (upstream withdrawal) while this run's Phase B
    is in flight, exactly like
    ``tests/test_scale_orchestration.py::TestUpstreamWithdrawalDuringPhaseC``,
    but additionally proves the withdrawal itself is a real, independently
    auditable state transition (not a bare field mutation) and that no
    Evaluation or duplicate audit trail leaks out of the raced run."""

    def setUp(self) -> None:
        super().setUp()
        self.manifest, self.daily_rows = self._seed_manifest(AS_OF)
        self.run = self._seed_run(self.manifest, AS_OF)

    def test_concurrent_withdrawal_during_phase_b_blocks_without_persisting(self):
        original_evaluate = service_module.evaluate_scale

        def _evaluate_then_withdraw(snapshot):
            self.enrollment.lifecycle = "closed"
            self.enrollment.transition_reason = "upstream_withdrawal"
            self.session.add(ScaleEnrollmentEventRecord(
                id=new_id("sene"),
                enrollment_id=self.enrollment.id,
                workspace_id=self.ws.id,
                meta_mode=self.mode,
                sequence=self.enrollment.revision + 1,
                event_type="closed",
                previous_lifecycle="active",
                current_lifecycle="closed",
                reason="upstream_withdrawal",
                deduplication_key="concurrent-withdrawal",
                executor_id="system:concurrent-writer",
                original_initiator_id="user_1",
                revision_at_event=self.enrollment.revision + 1,
            ))
            self.enrollment.revision += 1
            self.session.flush()
            self.session.commit()
            return original_evaluate(snapshot)

        with mock.patch.object(service_module, "evaluate_scale", side_effect=_evaluate_then_withdraw):
            outcome = service_module.run_scale_evaluation(self.session, self.context, self.run.id)

        self.assertEqual(outcome.kind, RunOutcomeKind.BLOCKED)
        self.assertEqual(outcome.detail, "upstream_withdrawal")

        self.session.refresh(self.run)
        self.assertEqual(self.run.run_state, "blocked")
        self.assertIsNotNone(self.run.snapshot_id)
        self.assertIsNone(self.run.evaluation_id)

        evaluations = self.session.execute(select(ScaleEvaluationRecord)).scalars().all()
        self.assertEqual(evaluations, [])

        withdrawal_events = self.session.execute(
            select(ScaleEnrollmentEventRecord).filter_by(
                enrollment_id=self.enrollment.id, deduplication_key="concurrent-withdrawal",
            )
        ).scalars().all()
        self.assertEqual(len(withdrawal_events), 1)


if __name__ == "__main__":
    unittest.main()
