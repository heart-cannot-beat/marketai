"""#96 and #97 against an isolated migrated PostgreSQL with the real Worker role.

Every Scale table's RLS policy is
``marketai_meta_context_is_current(workspace_id, meta_mode)``, and FORCE RLS
binds the Worker role too. Two gaps kept every evaluation from completing on
PostgreSQL, while the SQLite suite (no RLS) passed:

* #97: a claimed Job's context is frozen from the Job row, which records no
  Meta mode. The deployed Worker handed it to the Scale services as it was,
  so ``scale_evaluate`` failed as ``meta_mode_required``, and the deployed
  Worker did not forward ``scale_advice_expire`` at all;
* #96: ``run_scale_evaluation`` commits between its phases, and ``SET LOCAL``
  ends with the transaction. Phase B then read the snapshot Phase A had just
  sealed with no context, found nothing (``snapshot_not_found``), could not
  read its own run to fail it either (``NoResultFound``), and left the run
  ``snapshot_sealed`` with its Episode ``evaluating`` and owned by the run.

Here everything runs as ``marketai_domain_worker``: the Jobs are enqueued
through ``enqueue_scale_evaluation`` / ``enqueue_advice_expiry``, claimed
with ``jobs.claim_next`` and handled by the deployed ``WorkerRuntime``:

* a queued evaluation runs through Phase A, B and C to ``evaluated`` and
  writes its signals, streak counters, projection and audit event;
* a queued Advice expiry expires a thread past its freshness deadline;
* a Job without a valid mode fails as ``missing_input`` and changes nothing;
  another Workspace or mode can neither enqueue nor evaluate the run;
* recovery: a run stranded the way #96 left it (reproduced by committing
  Phase A without re-binding) completes when it is run again: Phase A reuses
  the sealed snapshot and the Episode returns to ``collecting``.

The fixture is seeded as the schema owner (BYPASSRLS). Its rows stay,
because the Scale evidence tables refuse DELETE; every Workspace id is new.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_WORKER_DATABASE_URL``,
``MARKETAI_DATABASE_URL`` (the owner) and ``MARKETAI_TENANT_CONTEXT_SECRET``
against a cluster released with ``make db-release``.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.platform import jobs
from app.scale.advice import AdviceLifecycleError, enqueue_advice_expiry
from app.scale.models import (
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationSignalRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvidenceSnapshotRecord,
)
from app.scale.service import ScaleOrchestrationError, enqueue_scale_evaluation, run_scale_evaluation
from app.worker_runtime import WorkerRuntime
from tests.support.scale_evaluation import (
    MODE,
    SeededRun,
    seed_active_thread,
    seed_evaluable_run,
    worker_context,
)

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
OWNER, WORKER = range(2)
SCALE_JOB_TYPES = ("scale_evaluate", "scale_advice_expire")
DEADLINE = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)


def _commit_without_rebinding(session) -> None:
    """``run_scale_evaluation``'s Phase A commit before #96."""
    session.commit()


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ScaleJobsOnTheWorkerRolePostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.worker):
            self.addCleanup(engine.dispose)
        worker_sessions = sessionmaker(bind=self.worker, expire_on_commit=False, autoflush=False)
        # The deployed Worker's two ways into the database, both as the Worker role.
        for patcher in (patch("app.adapters.persistence.database.SessionLocal", worker_sessions),
                        patch("app.platform.jobs.engine", self.worker)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = WorkerRuntime(owner=f"ci-only-scale-worker-{uuid.uuid4().hex[:8]}")

    # -- the fixture --------------------------------------------------------

    def _seed(self, prefix: str) -> SeededRun:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))  # the control plane opens it
            session.flush()
            return seed_evaluable_run(session, workspace_id)

    # -- the Job path, as the Worker ----------------------------------------

    def _enqueue_evaluation(self, seeded: SeededRun) -> str:
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            return enqueue_scale_evaluation(session, context, seeded.run_id).id

    def _enqueue_raw(self, workspace_id: str, job_type: str, payload: dict, key: str | None = None) -> str:
        context = worker_context(workspace_id)
        with tenant_session(context) as session:
            return jobs.enqueue_in_session(
                session, context=context, job_type=job_type, payload=payload,
                idempotency_key=key or f"ci-only-{job_type}-{uuid.uuid4().hex}",
            ).id

    def _claim_and_handle(self, job_id: str) -> None:
        """``jobs.claim_next`` for exactly this Job, then the deployed Worker's handler."""
        with Session(self.owner) as session, session.begin():  # an earlier failed test's Job waits
            session.execute(update(JobRecord).where(
                JobRecord.type.in_(SCALE_JOB_TYPES), JobRecord.state.in_(("queued", "retry_wait")),
                JobRecord.id != job_id).values(next_attempt_at=utcnow() + timedelta(days=1)))
        claimed = jobs.claim_next(self.runtime.owner, job_types=SCALE_JOB_TYPES)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job_id)
        self.assertIsNone(claimed.context.meta_mode, "the claimed context names no mode")
        {"scale_evaluate": self.runtime._scale_evaluate,
         "scale_advice_expire": self.runtime._scale_advice_expire}[claimed.type](claimed)

    # -- reading back, as the owner -----------------------------------------

    def _job(self, job_id: str) -> tuple[str, str]:
        with Session(self.owner) as session:
            job = session.get(JobRecord, job_id)
            return job.state, job.error_code

    def _run(self, seeded: SeededRun) -> tuple:
        with Session(self.owner) as session:
            run = session.get(ScaleEvaluationRunRecord, seeded.run_id)
            return run.run_state, run.outcome_kind, run.snapshot_id, run.evaluation_id

    def _episode(self, seeded: SeededRun) -> tuple:
        with Session(self.owner) as session:
            episode = session.get(ScaleEpisodeRecord, seeded.episode_id)
            return episode.phase, episode.phase_run_id

    def _count(self, model, column, value) -> int:
        with Session(self.owner) as session:
            return session.scalar(select(func.count()).select_from(model).where(column == value))

    def _assert_untouched(self, seeded: SeededRun) -> None:
        self.assertEqual(self._run(seeded), ("requested", None, None, None))
        self.assertEqual(self._episode(seeded), ("collecting", None))
        self.assertEqual(self._count(ScaleEvidenceSnapshotRecord, ScaleEvidenceSnapshotRecord.workspace_id,
                                     seeded.workspace_id), 0)

    def _assert_evaluated(self, seeded: SeededRun, snapshot_id: str | None = None) -> str:
        run_state, outcome_kind, sealed_id, evaluation_id = self._run(seeded)
        self.assertEqual((run_state, outcome_kind), ("evaluated", "evaluated"))
        if snapshot_id is not None:
            self.assertEqual(sealed_id, snapshot_id, "the sealed snapshot is reused, not resealed")
        self.assertEqual(self._count(ScaleEvidenceSnapshotRecord, ScaleEvidenceSnapshotRecord.workspace_id,
                                     seeded.workspace_id), 1)
        self.assertEqual(self._count(ScaleEvaluationRecord, ScaleEvaluationRecord.workspace_id,
                                     seeded.workspace_id), 1)
        self.assertEqual(self._episode(seeded), ("collecting", None))
        with Session(self.owner) as session:
            episode = session.get(ScaleEpisodeRecord, seeded.episode_id)
            self.assertEqual((episode.accepted_evaluation_id, episode.projection_revision), (evaluation_id, 1))
            self.assertEqual(session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.resource_id == seeded.run_id)).all(), ["scale_evaluation_evaluated"])
        self.assertEqual(self._count(ScaleEvaluationSignalRecord, ScaleEvaluationSignalRecord.evaluation_id,
                                     evaluation_id), 10)
        self.assertEqual(self._count(ScaleEvaluationStreakCounterRecord,
                                     ScaleEvaluationStreakCounterRecord.evaluation_id, evaluation_id), 10)
        self.assertEqual(self._count(ScaleEpisodeProjectionRecord, ScaleEpisodeProjectionRecord.episode_id,
                                     seeded.episode_id), 1)
        return evaluation_id

    # -- tests --------------------------------------------------------------

    def test_a_queued_evaluation_runs_through_all_three_phases_as_the_worker(self):
        seeded = self._seed("ws_scale_eval_pg")
        job_id = self._enqueue_evaluation(seeded)
        with Session(self.owner) as session:
            job = session.get(JobRecord, job_id)
            self.assertEqual((job.type, job.payload_json, job.idempotency_key),
                             ("scale_evaluate", {"evaluation_run_id": seeded.run_id, "meta_mode": MODE},
                              f"scale-eval:{seeded.run_id}:beta-v1"))

        self._claim_and_handle(job_id)

        self.assertEqual(self._job(job_id), ("succeeded", ""))
        self._assert_evaluated(seeded)
        # The key names one Job: enqueuing the run again changes nothing.
        self.assertEqual(self._enqueue_evaluation(seeded), job_id)
        with self.worker.connect() as connection:  # no tenant context: no row
            self.assertEqual(connection.execute(text("SELECT count(*) FROM scale_evaluation_runs")).scalar(), 0)

    def test_a_queued_advice_expiry_expires_the_thread_as_the_worker(self):
        seeded = self._seed("ws_scale_expire_pg")
        self._claim_and_handle(self._enqueue_evaluation(seeded))
        evaluation_id = self._assert_evaluated(seeded)
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            thread_id = seed_active_thread(session, context, seeded, evaluation_id)
        with tenant_session(context) as session:
            job = enqueue_advice_expiry(session, context, thread_id)
            job_id = job.id
            self.assertEqual((job.payload_json, job.idempotency_key),
                             ({"thread_id": thread_id, "meta_mode": MODE},
                              f"scale-advice-expire:{thread_id}:{DEADLINE.isoformat()}"))

        self._claim_and_handle(job_id)

        self.assertEqual(self._job(job_id), ("succeeded", ""))
        with Session(self.owner) as session:
            thread = session.get(ScaleAdviceThreadRecord, thread_id)
            self.assertEqual((thread.lifecycle, thread.lifecycle_reason),
                             ("expired", "freshness_deadline_exceeded"))
            self.assertEqual(session.scalars(select(ScaleAdviceThreadEventRecord.event_type).where(
                ScaleAdviceThreadEventRecord.thread_id == thread_id,
                ScaleAdviceThreadEventRecord.event_type == "expired")).all(), ["expired"])
        with tenant_session(context) as session:  # an expired thread has no deadline to schedule
            self.assertIsNone(enqueue_advice_expiry(session, context, thread_id))

    def test_a_job_without_a_valid_meta_mode_fails_as_missing_input_and_changes_nothing(self):
        seeded = self._seed("ws_scale_nomode_pg")
        for job_type, payload in (("scale_evaluate", {"evaluation_run_id": seeded.run_id}),
                                  ("scale_evaluate", {"evaluation_run_id": seeded.run_id, "meta_mode": "x"}),
                                  ("scale_advice_expire", {"thread_id": "sat_ci_only"})):
            with self.subTest(job_type=job_type, payload=payload):
                job_id = self._enqueue_raw(seeded.workspace_id, job_type, payload)
                self._claim_and_handle(job_id)
                self.assertEqual(self._job(job_id), ("failed", "missing_input"))
                self._assert_untouched(seeded)

    def test_another_workspace_or_mode_neither_enqueues_nor_evaluates_the_run(self):
        seeded = self._seed("ws_scale_owner_pg")
        other = self._seed("ws_scale_other_pg")
        for context in (worker_context(other.workspace_id), worker_context(seeded.workspace_id, "customer_sandbox")):
            with self.subTest(workspace=context.workspace_id, mode=context.meta_mode):
                with self.assertRaises(ScaleOrchestrationError) as caught, tenant_session(context) as session:
                    enqueue_scale_evaluation(session, context, seeded.run_id)
                self.assertEqual(caught.exception.code, "run_not_found")
                with self.assertRaises(AdviceLifecycleError) as caught, tenant_session(context) as session:
                    enqueue_advice_expiry(session, context, "sat_ci_only")
                self.assertEqual(caught.exception.code, "thread_not_found")

                # Only a forged payload can name the run from there.
                job_id = self._enqueue_raw(context.workspace_id, "scale_evaluate",
                                           {"evaluation_run_id": seeded.run_id, "meta_mode": context.meta_mode})
                self._claim_and_handle(job_id)
                self.assertEqual(self._job(job_id), ("failed", "run_not_found"))
                self._assert_untouched(seeded)

    def test_a_run_stranded_by_the_unbound_phase_commit_completes_when_run_again(self):
        seeded = self._seed("ws_scale_recover_pg")
        job_id = self._enqueue_evaluation(seeded)

        # Before #96: Phase A committed without re-binding the context.
        with patch("app.scale.service.commit_and_rebind", _commit_without_rebinding):
            self._claim_and_handle(job_id)
            self.assertEqual(self._job(job_id), ("failed", "scale_evaluate_failed"))
            run_state, outcome_kind, snapshot_id, evaluation_id = self._run(seeded)
            self.assertEqual((run_state, outcome_kind, evaluation_id), ("snapshot_sealed", None, None))
            self.assertIsNotNone(snapshot_id)
            self.assertEqual(self._episode(seeded), ("evaluating", seeded.run_id))

            # Running it again failed at the same point and changed nothing.
            context = worker_context(seeded.workspace_id)
            with self.assertRaises(NoResultFound), tenant_session(context) as session:
                run_scale_evaluation(session, context, seeded.run_id)
            self.assertEqual(self._run(seeded), ("snapshot_sealed", None, snapshot_id, None))
            self.assertEqual(self._episode(seeded), ("evaluating", seeded.run_id))

        # The scheduler's enqueue names the failed Job again (same key), so
        # the run is driven by a Job of its own.
        self.assertEqual(self._enqueue_evaluation(seeded), job_id)
        self.assertEqual(self._job(job_id), ("failed", "scale_evaluate_failed"))
        recovery_id = self._enqueue_raw(
            seeded.workspace_id, "scale_evaluate", {"evaluation_run_id": seeded.run_id, "meta_mode": MODE},
            key=f"scale-eval:{seeded.run_id}:beta-v1:ci-only-recovery")

        self._claim_and_handle(recovery_id)

        self.assertEqual(self._job(recovery_id), ("succeeded", ""))
        self._assert_evaluated(seeded, snapshot_id=snapshot_id)


if __name__ == "__main__":
    unittest.main()
