"""Issue #92 against an isolated migrated PostgreSQL with the real Worker role.

A Scale evaluation whose sealed snapshot carries another delivery
comparability hash than the current Episode's (the delivery configuration
changed, spec §3.3 step 3) rolls the Episode over in Phase A: it closes the
old Episode and opens a new one (Scale contract §1 item 4, §16.2). Evaluation
runs only in the Worker (``scale_evaluate`` -> ``run_scale_evaluation``), and
``marketai_domain_worker`` held UPDATE but no INSERT on ``scale_episodes``,
so on PostgreSQL the rollover failed with ``permission denied for table
scale_episodes`` and rolled the whole Phase A transaction back. The SQLite
test (``tests/test_scale_orchestration.py::TestEpisodeRolloverDuringPhaseA``)
checks no grants. Here the same scenario runs as the Worker role, under
FORCE RLS, inside ``tenant_session`` exactly as the Job handler calls it:

* the run ends ``episode_rolled_over``; the old Episode is closed with
  ``delivery_configuration_changed``; the Enrollment points at a new
  ``collecting`` Episode carrying the new comparability hash; the new
  Episode's baseline delivery source and the old Episode's ``closed`` event
  are written in the same transaction; no Evaluation exists. Running the run
  again replays the outcome and opens no third Episode;
* another Workspace, or the same Workspace in another Meta mode, finds no
  run and changes nothing, and the Worker cannot insert an Episode into
  another Workspace;
* the privilege matrix of ``scale_episodes``;
* the migration: below it the Worker has no INSERT, and above it it has.
  Without that grant (the migration's downgrade statement, applied at head
  because later migrations add columns the evaluation reads, #109) the
  Worker is refused exactly as production was, and the refused evaluation
  leaves the run ``requested`` with no snapshot and the Enrollment on its
  old Episode; with it the same run rolls over with no manual step.

The fixture is seeded as the schema owner (BYPASSRLS), as the SQLite test
seeds it. It opens no Job or Operation. Its rows stay, because the Scale
evidence tables refuse DELETE; every Workspace id is new.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_WORKER_DATABASE_URL``,
``MARKETAI_DATABASE_URL`` (the owner) and ``MARKETAI_TENANT_CONTEXT_SECRET``
against a cluster released with ``make db-release``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.platform.tenancy import TenantContext, system_context
from app.scale.models import (
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeEventRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
)
from app.scale.service import ScaleOrchestrationError, run_scale_evaluation
from app.scale.types import RunOutcomeKind
from tests.test_scale_evidence import (
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

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
OWNER, WORKER = range(2)
ROOT = Path(__file__).resolve().parents[1]
#: The migration that grants the Worker INSERT on scale_episodes (#92), and the head it follows.
REVISION, DOWN_REVISION = "zzb1e2f3a4b5", "zza0d1e2f3a4"
#: role -> privileges it must hold on scale_episodes; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {"SELECT", "INSERT", "UPDATE"},
    "marketai_domain_worker": {"SELECT", "INSERT", "UPDATE"},
    "marketai_domain_readonly": {"SELECT"},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
MODE = "internal_validation"
#: The SQLite scenario's day and hashes: the Episode froze 'c' * 64, the sync saw 'd' * 64.
AS_OF = date(2026, 9, 5)
FROZEN, CHANGED = "c" * 64, "d" * 64


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ScaleEpisodeRolloverPostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.worker):
            self.addCleanup(engine.dispose)
        self.worker_sessions = sessionmaker(bind=self.worker, expire_on_commit=False, autoflush=False)

    # -- the fixture --------------------------------------------------------

    def _seed(self, prefix: str) -> dict:
        """TestEpisodeRolloverDuringPhaseA's rows, as the owner: an active
        Enrollment on a collecting Episode that froze ``FROZEN``, and a
        requested run over a complete manifest whose delivery source says
        ``CHANGED``."""
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        current_start = AS_OF - timedelta(days=6)
        current_end = AS_OF + timedelta(days=1)
        baseline_start = current_start - timedelta(days=7)
        with Session(self.owner) as session, session.begin():
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))  # the control plane opens it
            session.flush()
            enrollment = _enrollment(workspace_id, MODE)
            contract_set = ScaleMetricSemanticContractSetRecord(
                id=new_id("smcs"), workspace_id=workspace_id, meta_mode=MODE, created_by="user_1")
            session.add_all([enrollment, contract_set])
            session.flush()
            session.add_all([
                ScaleMetricSemanticContractSetMemberRecord(
                    id=new_id("smcm"), contract_set_id=contract_set.id, contract_type=kind,
                    observation_contract_generation="gen-1", contract_hash=f"{kind}-hash")
                for kind in ("daily", "exact_window", "delivery_configuration")
            ])
            episode = _episode(workspace_id, MODE, enrollment.id, contract_set.id,
                               delivery_comparability_hash=FROZEN)
            policy = _policy()
            session.add_all([episode, policy])
            session.flush()
            enrollment.current_episode_id = episode.id
            session.flush()

            days = [baseline_start + timedelta(days=offset) for offset in range((current_end - baseline_start).days)]
            daily = {day: _daily_source(workspace_id, MODE, episode.id, day) for day in days}
            exact = {role: _exact_source(workspace_id, MODE, episode.id, role, start, end)
                     for role, start, end in (("current_7d", current_start, current_end),
                                              ("baseline_7d", baseline_start, current_start),
                                              ("rolling_14d", baseline_start, current_end))}
            delivery = _delivery_source(workspace_id, MODE, episode.id)
            delivery.comparability_hash = CHANGED
            session.add_all([*daily.values(), *exact.values(), delivery])
            session.flush()
            manifest = _manifest(workspace_id, MODE, episode.id, AS_OF)
            session.add(manifest)
            session.flush()
            session.add_all([
                *(_item(workspace_id, MODE, manifest.id, "daily", row.id, local_day=day) for day, row in daily.items()),
                *(_item(workspace_id, MODE, manifest.id, "exact_window", row.id, window_role=role)
                  for role, row in exact.items()),
                _item(workspace_id, MODE, manifest.id, "delivery_configuration", delivery.id),
            ])
            session.flush()
            run = _run(workspace_id, MODE, episode.id, enrollment.id, manifest, AS_OF, policy_id=policy.id)
            run.freshness_reference_at = datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc)
            session.add(run)
            seeded = {"workspace_id": workspace_id, "enrollment_id": enrollment.id, "episode_id": episode.id,
                      "run_id": run.id, "delivery_ids": {delivery.id}}
        return seeded

    @staticmethod
    def _context(workspace_id: str, meta_mode: str = MODE) -> TenantContext:
        """What ``scale_evaluate`` runs under: the claimed system context, with the run's mode."""
        return replace(system_context(workspace_id, initiator_id="user_1"), meta_mode=meta_mode)

    def _evaluate(self, context: TenantContext, run_id: str):
        """``WorkerRuntime._scale_evaluate``'s call, as the Worker role."""
        with patch("app.adapters.persistence.database.SessionLocal", self.worker_sessions), \
                tenant_session(context) as session:
            return run_scale_evaluation(session, context, run_id)

    def _state(self, seeded: dict) -> dict:
        """Everything the rollover may have written for this Enrollment, read as the owner."""
        workspace_id = seeded["workspace_id"]
        with Session(self.owner) as session:
            run = session.get(ScaleEvaluationRunRecord, seeded["run_id"])
            return {
                "run": (run.run_state, run.outcome_kind, run.outcome_detail, run.snapshot_id, run.evaluation_id),
                "current_episode_id": session.get(ScaleEnrollmentRecord, seeded["enrollment_id"]).current_episode_id,
                "episodes": {
                    row.id: row for row in session.scalars(select(ScaleEpisodeRecord).where(
                        ScaleEpisodeRecord.workspace_id == workspace_id))
                },
                "delivery_sources": {
                    row.id: row for row in session.scalars(select(ScaleDeliveryConfigSourceRecord).where(
                        ScaleDeliveryConfigSourceRecord.workspace_id == workspace_id))
                },
                "episode_events": session.scalars(select(ScaleEpisodeEventRecord).where(
                    ScaleEpisodeEventRecord.workspace_id == workspace_id)).all(),
                "snapshots": session.scalar(select(func.count()).select_from(ScaleEvidenceSnapshotRecord).where(
                    ScaleEvidenceSnapshotRecord.workspace_id == workspace_id)),
                "evaluations": session.scalar(select(func.count()).select_from(ScaleEvaluationRecord).where(
                    ScaleEvaluationRecord.workspace_id == workspace_id)),
            }

    def _assert_untouched(self, seeded: dict) -> None:
        """Still the seeded state: requested run, one collecting Episode, no event, no snapshot."""
        state = self._state(seeded)
        self.assertEqual(state["run"], ("requested", None, None, None, None))
        self.assertEqual(state["current_episode_id"], seeded["episode_id"])
        self.assertEqual(list(state["episodes"]), [seeded["episode_id"]])
        old = state["episodes"][seeded["episode_id"]]
        self.assertEqual((old.phase, old.closure_reason, old.ended_at), ("collecting", None, None))
        self.assertEqual(set(state["delivery_sources"]), seeded["delivery_ids"])
        self.assertEqual(state["episode_events"], [])
        self.assertEqual((state["snapshots"], state["evaluations"]), (0, 0))

    def _assert_rolled_over(self, seeded: dict, outcome) -> None:
        self.assertEqual(outcome.kind, RunOutcomeKind.EPISODE_ROLLED_OVER)
        self.assertEqual((outcome.run_id, outcome.episode_id, outcome.detail),
                         (seeded["run_id"], seeded["episode_id"], "delivery_configuration_changed"))
        state = self._state(seeded)
        run_state, outcome_kind, detail, snapshot_id, evaluation_id = state["run"]
        self.assertEqual((run_state, outcome_kind, detail), ("episode_rolled_over", "episode_rolled_over",
                                                             "delivery_configuration_changed"))
        self.assertIsNotNone(snapshot_id)  # Phase A sealed the snapshot in the same transaction
        self.assertIsNone(evaluation_id)
        self.assertEqual((state["snapshots"], state["evaluations"]), (1, 0))

        # UPDATE scale_episodes: the old Episode is closed.
        old = state["episodes"][seeded["episode_id"]]
        self.assertEqual((old.phase, old.closure_reason), ("closed", "delivery_configuration_changed"))
        self.assertIsNotNone(old.ended_at)
        # INSERT scale_episodes: one new Episode, which UPDATE scale_enrollments made current.
        [new_id_] = set(state["episodes"]) - {seeded["episode_id"]}
        self.assertEqual(state["current_episode_id"], new_id_)
        new = state["episodes"][new_id_]
        self.assertEqual((new.workspace_id, new.meta_mode, new.enrollment_id),
                         (seeded["workspace_id"], MODE, seeded["enrollment_id"]))
        self.assertEqual((new.phase, new.revision, new.started_local_day, new.delivery_comparability_hash),
                         ("collecting", 1, AS_OF, CHANGED))
        # INSERT scale_delivery_config_sources: the new Episode's frozen baseline.
        [baseline_id] = set(state["delivery_sources"]) - seeded["delivery_ids"]
        self.assertEqual(new.delivery_config_baseline_source_id, baseline_id)
        baseline = state["delivery_sources"][baseline_id]
        self.assertEqual((baseline.episode_id, baseline.source_revision, baseline.comparability_hash),
                         (new_id_, 1, CHANGED))
        # INSERT scale_episode_events: the old Episode's closure.
        [event] = state["episode_events"]
        self.assertEqual((event.episode_id, event.enrollment_id, event.event_type, event.current_phase,
                          event.closure_reason, event.deduplication_key),
                         (seeded["episode_id"], seeded["enrollment_id"], "closed", "closed",
                          "delivery_configuration_changed",
                          f"close:{seeded['episode_id']}:rollover:{seeded['run_id']}"))

    # -- tests --------------------------------------------------------------

    def test_the_worker_rolls_the_episode_over_when_the_delivery_configuration_changes(self):
        seeded = self._seed("ws_rollover_pg")
        context = self._context(seeded["workspace_id"])
        self._assert_rolled_over(seeded, self._evaluate(context, seeded["run_id"]))

        # The run is terminal: running it again replays the outcome, no third Episode.
        before = self._state(seeded)
        replayed = self._evaluate(context, seeded["run_id"])
        self.assertEqual((replayed.kind, replayed.detail),
                         (RunOutcomeKind.EPISODE_ROLLED_OVER, "delivery_configuration_changed"))
        after = self._state(seeded)
        self.assertEqual(set(after["episodes"]), set(before["episodes"]))
        self.assertEqual(after["current_episode_id"], before["current_episode_id"])
        self.assertEqual(len(after["episode_events"]), 1)

        with self.worker.connect() as connection:  # no tenant context: no row
            self.assertEqual(connection.execute(text("SELECT count(*) FROM scale_episodes")).scalar(), 0)

    def test_another_workspace_or_mode_neither_sees_nor_rolls_the_episode_over(self):
        seeded = self._seed("ws_rollover_pg_owner")
        other = self._seed("ws_rollover_pg_other")
        for context in (self._context(other["workspace_id"]),
                        self._context(seeded["workspace_id"], meta_mode="customer_sandbox")):
            with self.subTest(workspace=context.workspace_id, mode=context.meta_mode):
                with self.assertRaises(ScaleOrchestrationError) as caught:
                    self._evaluate(context, seeded["run_id"])
                self.assertEqual(caught.exception.code, "run_not_found")
                self._assert_untouched(seeded)

        # The new INSERT stays inside the caller's Workspace: an Episode for
        # this Enrollment, written under the other Workspace's context, is refused.
        intruder = _episode(seeded["workspace_id"], MODE, seeded["enrollment_id"], "smcs_intruder",
                            delivery_comparability_hash=CHANGED)
        with self.assertRaises(DBAPIError) as caught:
            with patch("app.adapters.persistence.database.SessionLocal", self.worker_sessions), \
                    tenant_session(self._context(other["workspace_id"])) as session:
                session.add(intruder)
                session.flush()
        self.assertIn("row-level security", str(caught.exception))
        self._assert_untouched(seeded)

    def test_the_privilege_matrix_of_scale_episodes_holds(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'scale_episodes'"
            )).scalar()
            self.assertTrue(forced)
            for role, expected in GRANTED.items():
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, 'scale_episodes', :p)"),
                                              {"r": role, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege} scale_episodes")

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[OWNER]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _worker_inserts(self) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT has_table_privilege('marketai_domain_worker', 'scale_episodes', 'INSERT')")).scalar()

    def _set_worker_insert(self, granted: bool) -> None:
        """The migration's own statement: its upgrade (GRANT) or downgrade (REVOKE)."""
        statement = ("GRANT INSERT ON TABLE scale_episodes TO marketai_domain_worker" if granted
                     else "REVOKE INSERT ON TABLE scale_episodes FROM marketai_domain_worker")
        with self.owner.begin() as connection:
            connection.execute(text(statement))

    def test_the_migration_grants_insert_and_a_refused_rollover_recovers_on_the_next_run(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        seeded = self._seed("ws_rollover_pg_migration")

        # The migration grants the INSERT: below it the Worker has none.
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertFalse(self._worker_inserts())
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertTrue(self._worker_inserts())
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)

        # The evaluation itself runs against the head schema: later
        # migrations add columns the ORM reads (#109), so it cannot run
        # below them. Without the grant -- the migration's downgrade
        # statement -- the Worker is refused as production was, and the
        # whole Phase A transaction rolls back with it.
        self.addCleanup(self._set_worker_insert, True)
        self._set_worker_insert(False)
        with self.assertRaises(DBAPIError) as caught:
            self._evaluate(self._context(seeded["workspace_id"]), seeded["run_id"])
        self.assertIn("permission denied for table scale_episodes", str(caught.exception))
        self._assert_untouched(seeded)

        # With it, the next evaluation of the same run rolls the Episode over.
        self._set_worker_insert(True)
        self._assert_rolled_over(seeded, self._evaluate(self._context(seeded["workspace_id"]), seeded["run_id"]))
        self.test_the_privilege_matrix_of_scale_episodes_holds()


if __name__ == "__main__":
    unittest.main()
