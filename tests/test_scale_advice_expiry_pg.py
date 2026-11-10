"""#111 against an isolated migrated PostgreSQL with the real Worker role.

Advice expiry is a delayed Job: Phase C schedules one ``scale_advice_expire``
Job per active thread of the Episode, with ``next_attempt_at`` at the
thread's freshness deadline, in the transaction that writes the thread and
the run's terminal state. ``marketai_claim_next_job_v2`` is unchanged: its
selecting query already skips a queued Job whose ``next_attempt_at`` is in
the future, against the database clock.

Here everything runs as ``marketai_domain_worker``: evaluations are enqueued
with ``enqueue_scale_evaluation``, claimed with ``jobs.claim_next`` and
handled by the deployed ``WorkerRuntime``, under FORCE RLS on every Scale
table and on ``jobs``. The seeded Episode's last two days deliver nothing,
so the evaluator itself reports a delivery anomaly and Phase C opens an
``investigate_delivery_anomaly`` thread. A deadline is the run's freshness
reference plus the Policy's 24 hours: a September reference is long due, one
taken now is due tomorrow, so every claim runs on the real clock.

* a due Job is claimed and expires the thread; one not yet due is never
  leased, and holds up no Job queued after it;
* a later evaluation schedules a new Job under a new key, and the old one,
  once due, changes nothing; the same deadline names the same Job;
* a crash after scheduling and before Phase C's commit leaves neither the
  thread nor its Job;
* a change of the Workspace's authorization epoch rejects the delayed Job at
  the next claim, and the thread stays active: recorded as the current
  behavior, the collection Job's fallback scan (#109) is what will expire it;
* another Workspace or Meta mode neither schedules for the Episode nor sees
  its Job.

The fixture is seeded as the schema owner (BYPASSRLS). Its rows stay,
because the Scale evidence tables refuse DELETE; every Workspace id is new.
Its Jobs do not stay: a Job not yet due would stay queued and count in the
cluster-wide queue depth that later CI steps read from
``marketai_health_stats_v1``, so after each test the owner deletes every
Job of the Workspaces that test seeded.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_WORKER_DATABASE_URL``,
``MARKETAI_DATABASE_URL`` (the owner) and ``MARKETAI_TENANT_CONTEXT_SECRET``
against a cluster released with ``make db-release``.
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

import app.scale.service as service_module
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.platform import jobs
from app.scale.advice import AdviceLifecycleError, enqueue_advice_expiry, enqueue_episode_advice_expiries
from app.scale.models import ScaleAdviceThreadEventRecord, ScaleAdviceThreadRecord, ScaleEvaluationRunRecord
from app.scale.service import enqueue_scale_evaluation
from app.worker_runtime import WorkerRuntime
from tests.support.scale_evaluation import (
    FRESHNESS_LAG,
    FRESHNESS_REFERENCE_AT,
    MODE,
    SeededRun,
    seed_evaluable_run,
    seed_later_run,
    worker_context,
)

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
SCALE_JOB_TYPES = ("scale_evaluate", "scale_advice_expire")
EXPIRE = ("scale_advice_expire",)
EVALUATE = ("scale_evaluate",)
#: The deadline of a thread on a run seeded with the default reference: due.
DUE_DEADLINE = FRESHNESS_REFERENCE_AT + FRESHNESS_LAG


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class AdviceExpiryAsADelayedJobPostgresTests(unittest.TestCase):
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
        self.runtime = WorkerRuntime(owner=f"ci-only-advice-delay-{uuid.uuid4().hex[:8]}")
        self._workspace_ids: list[str] = []
        self.addCleanup(self._delete_jobs)  # runs before the engines are disposed

    # -- the fixture, as the owner -------------------------------------------

    def _delete_jobs(self) -> None:
        """Delete every Job of the Workspaces this test seeded, whatever its state."""
        if not self._workspace_ids:
            return
        with Session(self.owner) as session, session.begin():
            session.execute(delete(JobRecord).where(JobRecord.workspace_id.in_(self._workspace_ids)))

    def _seed(self, prefix: str, *, freshness_reference_at: datetime = FRESHNESS_REFERENCE_AT) -> SeededRun:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        self._workspace_ids.append(workspace_id)
        with Session(self.owner) as session, session.begin():
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))  # the control plane opens it
            session.flush()
            return seed_evaluable_run(
                session, workspace_id, delivery_anomaly=True, freshness_reference_at=freshness_reference_at)

    def _seed_later_run(self, seeded: SeededRun, freshness_reference_at: datetime) -> str:
        with Session(self.owner) as session, session.begin():
            return seed_later_run(session, seeded, freshness_reference_at)

    def _bump_epoch(self, workspace_id: str) -> None:
        """The control plane revokes a membership: the Workspace's epoch moves."""
        with Session(self.owner) as session, session.begin():
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == workspace_id).values(
                authorization_epoch=WorkspaceRecord.authorization_epoch + 1))

    # -- the Job path, as the Worker ----------------------------------------

    def _claim(self, workspace_id: str, job_types: tuple[str, ...]) -> jobs.ClaimedJob | None:
        """``jobs.claim_next`` with every other Workspace's open Scale Job deferred
        (an earlier test's Job may wait); this Workspace's Jobs are left as they are."""
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord).where(
                JobRecord.type.in_(SCALE_JOB_TYPES), JobRecord.state.in_(("queued", "retry_wait")),
                JobRecord.workspace_id != workspace_id).values(next_attempt_at=utcnow() + timedelta(days=1)))
        claimed = jobs.claim_next(self.runtime.owner, job_types=job_types)
        if claimed is not None:
            self.assertEqual(claimed.context.workspace_id, workspace_id)
            self.assertIsNone(claimed.context.meta_mode, "the claimed context names no mode")
        return claimed

    def _handle(self, claimed: jobs.ClaimedJob) -> None:
        {"scale_evaluate": self.runtime._scale_evaluate,
         "scale_advice_expire": self.runtime._scale_advice_expire}[claimed.type](claimed)

    def _enqueue_evaluation(self, seeded: SeededRun, run_id: str | None = None) -> str:
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            return enqueue_scale_evaluation(session, context, run_id or seeded.run_id).id

    def _evaluate(self, seeded: SeededRun, run_id: str | None = None) -> None:
        job_id = self._enqueue_evaluation(seeded, run_id)
        claimed = self._claim(seeded.workspace_id, EVALUATE)
        self.assertEqual(claimed.id, job_id)
        self._handle(claimed)
        self.assertEqual(self._job(job_id), ("succeeded", ""))

    def _enqueue_raw(self, workspace_id: str, *, not_before: datetime | None = None) -> str:
        """An expiry Job no enqueue function builds: without a mode, its handler
        fails it as ``missing_input`` and it touches nothing."""
        context = worker_context(workspace_id)
        with tenant_session(context) as session:
            return jobs.enqueue_in_session(
                session, context=context, job_type="scale_advice_expire", payload={"thread_id": "sat_ci_only"},
                idempotency_key=f"ci-only-delay-{uuid.uuid4().hex}", not_before=not_before,
            ).id

    # -- reading back, as the owner -----------------------------------------

    def _job(self, job_id: str) -> tuple[str, str]:
        with Session(self.owner) as session:
            job = session.get(JobRecord, job_id)
            return job.state, job.error_code

    def _attempts(self, job_id: str) -> tuple[int, int]:
        """The Job's attempt count and its ``job.leased`` audit events."""
        with Session(self.owner) as session:
            return session.get(JobRecord, job_id).attempt, session.scalar(
                select(func.count()).select_from(AuditEventRecord).where(
                    AuditEventRecord.resource_id == job_id, AuditEventRecord.event_type == "job.leased"))

    def _threads(self, seeded: SeededRun) -> list[tuple[str, str]]:
        with Session(self.owner) as session:
            return [(row.advice_type, row.lifecycle) for row in session.scalars(
                select(ScaleAdviceThreadRecord).where(ScaleAdviceThreadRecord.episode_id == seeded.episode_id))]

    def _thread_id(self, seeded: SeededRun) -> str:
        with Session(self.owner) as session:
            return session.scalars(select(ScaleAdviceThreadRecord.id).where(
                ScaleAdviceThreadRecord.episode_id == seeded.episode_id)).one()

    def _expiry_jobs(self, thread_id: str) -> list[tuple]:
        with Session(self.owner) as session:
            return [(job.id, job.state, job.next_attempt_at, job.idempotency_key, job.payload_json)
                    for job in session.scalars(select(JobRecord).where(
                        JobRecord.type == "scale_advice_expire", JobRecord.resource_id == thread_id,
                    ).order_by(JobRecord.created_at, JobRecord.id))]

    def _expired_events(self, thread_id: str) -> list[str]:
        with Session(self.owner) as session:
            return session.scalars(select(ScaleAdviceThreadEventRecord.reason).where(
                ScaleAdviceThreadEventRecord.thread_id == thread_id,
                ScaleAdviceThreadEventRecord.event_type == "expired")).all()

    def _scheduled(self, thread_id: str, deadline: datetime) -> tuple:
        """The one queued Job at ``deadline``: due then, under the thread-and-deadline key."""
        return ("queued", deadline, f"scale-advice-expire:{thread_id}:{deadline.isoformat()}",
                {"thread_id": thread_id, "meta_mode": MODE})

    # -- tests --------------------------------------------------------------

    def test_phase_c_schedules_the_thread_at_its_deadline_and_the_due_job_expires_it(self):
        seeded = self._seed("ws_advice_due_pg")
        self._evaluate(seeded)

        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])
        thread_id = self._thread_id(seeded)
        [(job_id, *scheduled)] = self._expiry_jobs(thread_id)
        self.assertEqual(tuple(scheduled), self._scheduled(thread_id, DUE_DEADLINE))

        claimed = self._claim(seeded.workspace_id, EXPIRE)
        self.assertEqual(claimed.id, job_id, "the deadline has passed on the database clock")
        self._handle(claimed)

        self.assertEqual(self._job(job_id), ("succeeded", ""))
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "expired")])
        self.assertEqual(self._expired_events(thread_id), ["freshness_deadline_exceeded"])
        with self.worker.connect() as connection:  # no tenant context: no row
            self.assertEqual(connection.execute(text("SELECT count(*) FROM jobs")).scalar(), 0)

    def test_a_job_whose_deadline_is_ahead_is_never_leased(self):
        reference = utcnow()
        seeded = self._seed("ws_advice_ahead_pg", freshness_reference_at=reference)
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [(job_id, *scheduled)] = self._expiry_jobs(thread_id)
        self.assertEqual(tuple(scheduled), self._scheduled(thread_id, reference + FRESHNESS_LAG))

        for _ in range(3):
            self.assertIsNone(self._claim(seeded.workspace_id, EXPIRE))

        self.assertEqual(self._job(job_id), ("queued", ""))
        self.assertEqual(self._attempts(job_id), (0, 0), "never leased, so no Worker spins on it")
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])

    def test_a_delayed_job_does_not_hold_up_the_jobs_queued_after_it(self):
        seeded = self._seed("ws_advice_fifo_pg")
        delayed = self._enqueue_raw(seeded.workspace_id, not_before=utcnow() + timedelta(hours=1))
        first, second = self._enqueue_raw(seeded.workspace_id), self._enqueue_raw(seeded.workspace_id)

        order = []
        for _ in range(3):
            claimed = self._claim(seeded.workspace_id, EXPIRE)
            order.append(claimed.id if claimed else None)
            if claimed:
                self._handle(claimed)

        self.assertEqual(order, [first, second, None])
        self.assertEqual([self._job(job_id) for job_id in (first, second)], [("failed", "missing_input")] * 2)
        self.assertEqual((self._job(delayed), self._attempts(delayed)), (("queued", ""), (0, 0)))

    def test_a_later_evaluation_schedules_a_new_job_and_the_old_one_changes_nothing(self):
        seeded = self._seed("ws_advice_moved_pg")
        later = utcnow()
        later_run_id = self._seed_later_run(seeded, later)
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        self._evaluate(seeded, later_run_id)

        [(old_id, *old), (new_id, *new)] = self._expiry_jobs(thread_id)
        self.assertEqual(tuple(old), self._scheduled(thread_id, DUE_DEADLINE))
        self.assertEqual(tuple(new), self._scheduled(thread_id, later + FRESHNESS_LAG))

        claimed = self._claim(seeded.workspace_id, EXPIRE)
        self.assertEqual(claimed.id, old_id, "the old deadline has passed")
        self._handle(claimed)

        self.assertEqual(self._job(old_id), ("succeeded", ""))
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])
        self.assertEqual(self._expired_events(thread_id), [])
        self.assertIsNone(self._claim(seeded.workspace_id, EXPIRE), "the new deadline is still ahead")
        self.assertEqual(self._job(new_id), ("queued", ""))

    def test_scheduling_the_same_deadline_again_returns_the_same_job(self):
        seeded = self._seed("ws_advice_same_pg")
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [(job_id, *_)] = self._expiry_jobs(thread_id)
        context = worker_context(seeded.workspace_id)

        with tenant_session(context) as session:
            self.assertEqual(enqueue_advice_expiry(session, context, thread_id).id, job_id)
            self.assertEqual([job.id for job in enqueue_episode_advice_expiries(
                session, context, seeded.episode_id)], [job_id])

        self.assertEqual([row[0] for row in self._expiry_jobs(thread_id)], [job_id])

    def test_the_thread_and_its_job_commit_together(self):
        seeded = self._seed("ws_advice_atomic_pg")

        def schedule_then_crash(session, context, episode_id):
            self.assertEqual(len(enqueue_episode_advice_expiries(session, context, episode_id)), 1)
            raise RuntimeError("ci-only crash before Phase C commits")

        job_id = self._enqueue_evaluation(seeded)
        claimed = self._claim(seeded.workspace_id, EVALUATE)
        with patch.object(service_module, "enqueue_episode_advice_expiries", schedule_then_crash):
            self._handle(claimed)

        self.assertEqual(self._job(job_id), ("failed", "scale_evaluate_failed"))
        self.assertEqual(self._threads(seeded), [], "no thread without its Job")
        with Session(self.owner) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(JobRecord).where(
                JobRecord.workspace_id == seeded.workspace_id, JobRecord.type == "scale_advice_expire")), 0)
            self.assertIsNone(session.get(ScaleEvaluationRunRecord, seeded.run_id).outcome_kind)

    def test_a_stale_authorization_epoch_rejects_the_delayed_job_and_the_thread_stays_active(self):
        seeded = self._seed("ws_advice_epoch_pg", freshness_reference_at=utcnow())
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [(job_id, *_)] = self._expiry_jobs(thread_id)
        self._bump_epoch(seeded.workspace_id)

        self.assertIsNone(self._claim(seeded.workspace_id, EXPIRE))

        self.assertEqual(self._job(job_id), ("failed", "authorization_epoch_stale"))
        self.assertEqual(self._attempts(job_id), (0, 0))
        # Current behavior: nothing else expires this thread until the
        # collection Job's fallback scan (#109) exists.
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])

    def test_another_workspace_or_mode_neither_schedules_for_the_episode_nor_sees_its_job(self):
        seeded = self._seed("ws_advice_owner_pg")
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [(job_id, *_)] = self._expiry_jobs(thread_id)
        other = self._seed("ws_advice_other_pg")
        for context in (worker_context(other.workspace_id), worker_context(seeded.workspace_id, "customer_sandbox")):
            with self.subTest(workspace=context.workspace_id, mode=context.meta_mode):
                with tenant_session(context) as session:
                    self.assertEqual(enqueue_episode_advice_expiries(session, context, seeded.episode_id), [])
                with self.assertRaises(AdviceLifecycleError) as caught, tenant_session(context) as session:
                    enqueue_advice_expiry(session, context, thread_id)
                self.assertEqual(caught.exception.code, "thread_not_found")
        with tenant_session(worker_context(other.workspace_id)) as session:  # RLS on jobs
            self.assertIsNone(session.get(JobRecord, job_id))
        self.assertEqual([row[0] for row in self._expiry_jobs(thread_id)], [job_id])


if __name__ == "__main__":
    unittest.main()
