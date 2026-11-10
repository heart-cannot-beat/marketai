"""Advice expiry as a delayed Job (#111, the deadline-Job part of #101 S5).

``enqueue_in_session(not_before=...)`` writes ``next_attempt_at``, which the
SQLite claim path (``jobs.claim_next``) already filters in its selecting
query, as ``marketai_claim_next_job_v2`` does on PostgreSQL: a Job that is
not due is never leased, so it holds up no Job queued after it and a Worker
does not spin on it.

Phase C schedules one ``scale_advice_expire`` Job per active thread of the
Episode, keyed by thread and freshness deadline and not claimable before
that deadline, once its run is flushed as ``evaluated`` and before its one
commit: the thread changes, their Jobs and the run's terminal state land
together. A later evaluation that moves the deadline schedules a new Job
under a new key; the old one, once due, rechecks and changes nothing.

The threads here come from the evaluator itself: the seeded Episode's last
two days deliver nothing, so it reports a delivery anomaly and Phase C opens
an ``investigate_delivery_anomaly`` thread. Deadlines are the seeded run's
freshness reference plus the Policy's 24 hours: a reference in September is
long due, one taken now is due tomorrow. So every claim below runs on the
real clock, as it does on PostgreSQL (``tests/test_scale_advice_expiry_pg.py``
runs the same paths as the real Worker role).

A change of the Workspace's authorization epoch rejects a delayed Job at the
next claim, due or not, and the thread stays active. That is recorded here as
the current behavior: the fallback scan in the source-collection Job (#109)
is what will expire such a thread.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import func, select, update

import app.scale.service as service_module
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.platform import jobs
from app.scale.advice import AdviceLifecycleError, enqueue_advice_expiry, enqueue_episode_advice_expiries
from app.scale.models import ScaleAdviceThreadEventRecord, ScaleAdviceThreadRecord, ScaleEvaluationRunRecord
from app.scale.service import enqueue_scale_evaluation
from app.worker_runtime import WorkerRuntime
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.support.scale_evaluation import (
    FRESHNESS_LAG,
    FRESHNESS_REFERENCE_AT,
    MODE,
    SeededRun,
    seed_evaluable_run,
    seed_later_run,
    worker_context,
)

#: The deadline of a thread on a run seeded with the default reference: due.
DUE_DEADLINE = FRESHNESS_REFERENCE_AT + FRESHNESS_LAG
EXPIRE = ("scale_advice_expire",)
EVALUATE = ("scale_evaluate",)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns the stored UTC wall clock without its offset."""
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class DelayedJobTestBase(LibraryFixtures):
    def setUp(self):
        super().setUp()
        self.runtime = WorkerRuntime(owner=f"ci-only-delay-{uuid.uuid4().hex[:6]}")

    def _workspace(self) -> str:
        workspace_id = f"ws_advice_delay_{uuid.uuid4().hex}"
        self.provision_workspace(workspace_id)
        self.addCleanup(cancel_workspace_jobs, workspace_id)
        return workspace_id

    def _claim(self, workspace_id: str, job_types: tuple[str, ...]) -> jobs.ClaimedJob | None:
        """``jobs.claim_next`` with every other Workspace's open Job deferred."""
        quarantine_foreign_jobs(workspace_id)
        claimed = jobs.claim_next(self.runtime.owner, job_types=job_types)
        if claimed is not None:
            self.assertEqual(claimed.context.workspace_id, workspace_id)
        return claimed

    def _settle(self, claimed: jobs.ClaimedJob) -> None:
        """A claimed Job no handler runs here: end it, or its one-second test
        lease would put it back on the queue for a later test."""
        jobs.fail(claimed.context, claimed.id, self.runtime.owner, code="ci_only", detail="", retryable=False)

    @staticmethod
    def _job(job_id: str) -> JobRecord:
        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            session.expunge(job)
            return job

    @staticmethod
    def _leased_events(job_id: str) -> int:
        with platform_session() as session:
            return session.scalar(select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.resource_id == job_id, AuditEventRecord.event_type == "job.leased"))

    @staticmethod
    def _bump_epoch(workspace_id: str) -> None:
        """The control plane revokes a membership: the Workspace's epoch moves."""
        with platform_session() as session:
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == workspace_id).values(
                authorization_epoch=WorkspaceRecord.authorization_epoch + 1))


class DelayedJobsOnTheQueue(DelayedJobTestBase):
    @staticmethod
    def _enqueue(workspace_id: str, *, not_before: datetime | None = None, key: str | None = None) -> str:
        context = worker_context(workspace_id)
        with tenant_session(context) as session:
            return jobs.enqueue_in_session(
                session, context=context, job_type="scale_advice_expire",
                payload={"thread_id": "sat_ci_only", "meta_mode": MODE},
                idempotency_key=key or f"ci-only-delay-{uuid.uuid4().hex}", not_before=not_before,
            ).id

    def test_a_delayed_job_is_not_claimed_before_it_is_due(self):
        workspace_id = self._workspace()
        due = utcnow() + timedelta(hours=1)
        job_id = self._enqueue(workspace_id, not_before=due)

        self.assertEqual(_aware(self._job(job_id).next_attempt_at), due)
        self.assertIsNone(self._claim(workspace_id, EXPIRE))
        job = self._job(job_id)
        self.assertEqual((job.state, job.attempt), ("queued", 0))
        self.assertEqual(self._leased_events(job_id), 0, "never leased, so no Worker spins on it")

        with mock.patch.object(jobs, "utcnow", return_value=due):
            claimed = self._claim(workspace_id, EXPIRE)
        self.assertEqual(claimed.id, job_id)
        self._settle(claimed)

    def test_a_delayed_job_does_not_hold_up_the_jobs_queued_after_it(self):
        workspace_id = self._workspace()
        due = utcnow() + timedelta(hours=1)
        delayed = self._enqueue(workspace_id, not_before=due)
        first, second = self._enqueue(workspace_id), self._enqueue(workspace_id)

        order = []
        for _ in range(3):
            claimed = self._claim(workspace_id, EXPIRE)
            order.append(claimed.id if claimed else None)
            if claimed:
                self._settle(claimed)

        self.assertEqual(order, [first, second, None])
        self.assertEqual((self._job(delayed).state, self._leased_events(delayed)), ("queued", 0))
        with mock.patch.object(jobs, "utcnow", return_value=due + timedelta(seconds=1)):
            claimed = self._claim(workspace_id, EXPIRE)
        self.assertEqual(claimed.id, delayed)
        self._settle(claimed)

    def test_the_same_key_names_one_delayed_job(self):
        workspace_id = self._workspace()
        due = utcnow() + timedelta(hours=1)
        key = f"ci-only-delay-{uuid.uuid4().hex}"
        job_id = self._enqueue(workspace_id, not_before=due, key=key)

        self.assertEqual(self._enqueue(workspace_id, not_before=due, key=key), job_id)

        with platform_session() as session:
            details = session.scalars(select(AuditEventRecord.details_json).where(
                AuditEventRecord.resource_id == job_id, AuditEventRecord.event_type == "job.enqueued")).all()
        self.assertEqual([detail["not_before"] for detail in details], [due.isoformat()])

    def test_not_before_is_stored_in_utc_and_must_name_its_offset(self):
        workspace_id = self._workspace()
        due = datetime(2031, 1, 2, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        job_id = self._enqueue(workspace_id, not_before=due)
        self.assertEqual(_aware(self._job(job_id).next_attempt_at), datetime(2031, 1, 2, 4, 0, tzinfo=timezone.utc))

        key = f"ci-only-naive-{uuid.uuid4().hex}"
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._enqueue(workspace_id, not_before=datetime(2031, 1, 2, 4, 0), key=key)
        with platform_session() as session:
            self.assertIsNone(session.scalar(select(JobRecord.id).where(JobRecord.idempotency_key == key)))

    def test_a_stale_authorization_epoch_rejects_a_delayed_job_before_it_is_due(self):
        workspace_id = self._workspace()
        job_id = self._enqueue(workspace_id, not_before=utcnow() + timedelta(hours=1))
        self._bump_epoch(workspace_id)

        self.assertIsNone(self._claim(workspace_id, EXPIRE))

        job = self._job(job_id)
        self.assertEqual((job.state, job.error_code), ("failed", "authorization_epoch_stale"))
        self.assertEqual(self._leased_events(job_id), 0)


class PhaseCSchedulesAdviceExpiry(DelayedJobTestBase):
    def _seed(self, *, freshness_reference_at: datetime = FRESHNESS_REFERENCE_AT) -> SeededRun:
        workspace_id = self._workspace()
        with tenant_session(worker_context(workspace_id)) as session:
            return seed_evaluable_run(
                session, workspace_id, delivery_anomaly=True, freshness_reference_at=freshness_reference_at)

    def _evaluate(self, seeded: SeededRun, run_id: str | None = None) -> None:
        """Enqueue the run's ``scale_evaluate`` Job, claim it as the only
        evaluation and run the deployed Worker's handler."""
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            job_id = enqueue_scale_evaluation(session, context, run_id or seeded.run_id).id
        claimed = self._claim(seeded.workspace_id, EVALUATE)
        self.assertEqual(claimed.id, job_id)
        self.runtime._scale_evaluate(claimed)
        self.assertEqual(self._job(job_id).state, "succeeded")

    def _run_one(self, workspace_id: str) -> bool:
        """One tick of the deployed Worker loop."""
        quarantine_foreign_jobs(workspace_id)
        handled = self.runtime.run_once()
        return asyncio.run(handled) if inspect.isawaitable(handled) else handled

    @staticmethod
    def _threads(seeded: SeededRun) -> list[tuple[str, str]]:
        with platform_session() as session:
            return [(row.advice_type, row.lifecycle) for row in session.scalars(
                select(ScaleAdviceThreadRecord).where(ScaleAdviceThreadRecord.episode_id == seeded.episode_id))]

    @staticmethod
    def _thread_id(seeded: SeededRun) -> str:
        with platform_session() as session:
            return session.scalars(select(ScaleAdviceThreadRecord.id).where(
                ScaleAdviceThreadRecord.episode_id == seeded.episode_id)).one()

    @staticmethod
    def _expiry_jobs(thread_id: str) -> list[JobRecord]:
        with platform_session() as session:
            rows = session.scalars(select(JobRecord).where(
                JobRecord.type == "scale_advice_expire", JobRecord.resource_id == thread_id,
            ).order_by(JobRecord.created_at, JobRecord.id)).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    @staticmethod
    def _expired_events(thread_id: str) -> list[str]:
        with platform_session() as session:
            return session.scalars(select(ScaleAdviceThreadEventRecord.reason).where(
                ScaleAdviceThreadEventRecord.thread_id == thread_id,
                ScaleAdviceThreadEventRecord.event_type == "expired")).all()

    def _assert_scheduled(self, thread_id: str, deadline: datetime) -> JobRecord:
        [job] = [job for job in self._expiry_jobs(thread_id) if _aware(job.next_attempt_at) == deadline]
        self.assertEqual((job.state, job.attempt), ("queued", 0))
        self.assertEqual(job.idempotency_key, f"scale-advice-expire:{thread_id}:{deadline.isoformat()}")
        self.assertEqual(job.payload_json, {"thread_id": thread_id, "meta_mode": MODE})
        self.assertEqual((job.resource_type, job.resource_id), ("scale_advice_thread", thread_id))
        return job

    def test_phase_c_schedules_the_new_thread_at_its_deadline_and_the_due_job_expires_it(self):
        seeded = self._seed()
        self._evaluate(seeded)

        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])
        thread_id = self._thread_id(seeded)
        [job] = self._expiry_jobs(thread_id)
        self.assertEqual(self._assert_scheduled(thread_id, DUE_DEADLINE).id, job.id)

        self.assertTrue(self._run_one(seeded.workspace_id), "the deadline has passed: the Worker claims it")

        self.assertEqual((self._job(job.id).state, self._job(job.id).error_code), ("succeeded", ""))
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "expired")])
        self.assertEqual(self._expired_events(thread_id), ["freshness_deadline_exceeded"])

    def test_a_job_whose_deadline_is_ahead_is_not_claimed(self):
        reference = utcnow()
        seeded = self._seed(freshness_reference_at=reference)
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        job = self._assert_scheduled(thread_id, reference + FRESHNESS_LAG)

        self.assertFalse(self._run_one(seeded.workspace_id), "nothing is due: the Worker idles")

        self.assertEqual((self._job(job.id).state, self._leased_events(job.id)), ("queued", 0))
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])

    def test_a_later_evaluation_schedules_a_new_job_and_the_old_one_changes_nothing(self):
        seeded = self._seed()
        later = utcnow()
        with tenant_session(worker_context(seeded.workspace_id)) as session:
            later_run_id = seed_later_run(session, seeded, later)
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        self._evaluate(seeded, later_run_id)

        old = self._assert_scheduled(thread_id, DUE_DEADLINE)
        new = self._assert_scheduled(thread_id, later + FRESHNESS_LAG)
        self.assertEqual([job.id for job in self._expiry_jobs(thread_id)], [old.id, new.id])

        self.assertTrue(self._run_one(seeded.workspace_id), "the old deadline has passed")

        self.assertEqual(self._job(old.id).state, "succeeded")
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])
        self.assertEqual(self._expired_events(thread_id), [])
        self.assertFalse(self._run_one(seeded.workspace_id), "the new deadline is still ahead")
        self.assertEqual(self._job(new.id).state, "queued")

    def test_scheduling_the_same_deadline_again_returns_the_same_job(self):
        seeded = self._seed()
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [job] = self._expiry_jobs(thread_id)
        context = worker_context(seeded.workspace_id)

        with tenant_session(context) as session:
            self.assertEqual(enqueue_advice_expiry(session, context, thread_id).id, job.id)
            self.assertEqual([row.id for row in enqueue_episode_advice_expiries(
                session, context, seeded.episode_id)], [job.id])

        self.assertEqual([row.id for row in self._expiry_jobs(thread_id)], [job.id])

    def test_the_thread_and_its_job_commit_together(self):
        seeded = self._seed()

        def schedule_then_crash(session, context, episode_id):
            scheduled = enqueue_episode_advice_expiries(session, context, episode_id)
            self.assertEqual(len(scheduled), 1, "the thread was flushed and its Job scheduled")
            raise RuntimeError("ci-only crash before Phase C commits")

        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            job_id = enqueue_scale_evaluation(session, context, seeded.run_id).id
        claimed = self._claim(seeded.workspace_id, EVALUATE)
        with mock.patch.object(service_module, "enqueue_episode_advice_expiries", schedule_then_crash):
            self.runtime._scale_evaluate(claimed)

        self.assertEqual((self._job(job_id).state, self._job(job_id).error_code),
                         ("failed", "scale_evaluate_failed"))
        self.assertEqual(self._threads(seeded), [], "no thread without its Job")
        with platform_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(JobRecord).where(
                JobRecord.workspace_id == seeded.workspace_id, JobRecord.type == "scale_advice_expire")), 0)
            run = session.get(ScaleEvaluationRunRecord, seeded.run_id)
            self.assertIsNone(run.outcome_kind)

    def test_a_stale_authorization_epoch_rejects_the_delayed_job_and_the_thread_stays_active(self):
        seeded = self._seed(freshness_reference_at=utcnow())
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [job] = self._expiry_jobs(thread_id)
        self._bump_epoch(seeded.workspace_id)

        self.assertFalse(self._run_one(seeded.workspace_id))

        self.assertEqual((self._job(job.id).state, self._job(job.id).error_code),
                         ("failed", "authorization_epoch_stale"))
        # Current behavior: nothing else expires this thread until the
        # collection Job's fallback scan (#109) exists.
        self.assertEqual(self._threads(seeded), [("investigate_delivery_anomaly", "active")])

    def test_another_workspace_or_mode_schedules_nothing_for_the_episode(self):
        seeded = self._seed()
        self._evaluate(seeded)
        thread_id = self._thread_id(seeded)
        [job] = self._expiry_jobs(thread_id)
        other = self._seed()
        for context in (worker_context(other.workspace_id), worker_context(seeded.workspace_id, "customer_sandbox")):
            with self.subTest(workspace=context.workspace_id, mode=context.meta_mode):
                with tenant_session(context) as session:
                    self.assertEqual(enqueue_episode_advice_expiries(session, context, seeded.episode_id), [])
                with self.assertRaises(AdviceLifecycleError) as caught, tenant_session(context) as session:
                    enqueue_advice_expiry(session, context, thread_id)
                self.assertEqual(caught.exception.code, "thread_not_found")
        self.assertEqual([row.id for row in self._expiry_jobs(thread_id)], [job.id])
