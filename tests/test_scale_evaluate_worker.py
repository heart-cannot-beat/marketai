"""Scale evaluation and Advice expiry Jobs on both Worker runtimes (#96, #97).

A claimed Job's context is frozen from the Job row, which records no Meta
mode, and every Scale table's RLS policy shows a row only to a context that
names one. So ``scale_evaluate`` and ``scale_advice_expire`` Jobs carry the
mode in their payload (``enqueue_scale_evaluation``,
``enqueue_advice_expiry``) and both runtimes' handlers read it back
(``evaluation_job_context``, ``advice_expire_job_context``); a payload
without a valid one fails as ``missing_input`` and touches nothing. The
deployed Worker now also forwards ``scale_advice_expire``.

``run_scale_evaluation`` commits between its phases. ``SET LOCAL`` ends with
each transaction, so every later transaction has to carry the context again
(#96): Phase A commits with ``commit_and_rebind`` and Phase C opens a bound
transaction, while Phase B's computation runs with no transaction open
(spec §3.4). SQLite has no RLS, so a statement run without the context
passes here; the pin below records which statements ran outside a bound
transaction instead. ``tests/test_scale_evaluate_worker_pg.py`` runs the
same Jobs as the real Worker role under FORCE RLS.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from unittest import mock

from sqlalchemy import event, func, select, update

import app.scale.service as service_module
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord
from app.platform import jobs, tenancy
from app.scale.advice import AdviceLifecycleError, enqueue_advice_expiry
from app.scale.models import (
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationSignalRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvidenceSnapshotRecord,
)
from app.scale.service import ScaleOrchestrationError, enqueue_scale_evaluation, run_scale_evaluation
from app.scale.types import RunOutcomeKind
from app.scale.worker import ScaleWorkerRuntime
from app.worker_runtime import WorkerRuntime
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.support.scale_evaluation import (
    MODE,
    SeededRun,
    seed_active_thread,
    seed_evaluable_run,
    worker_context,
)

RUNTIMES = {"WorkerRuntime": WorkerRuntime, "ScaleWorkerRuntime": ScaleWorkerRuntime}
#: The freshness deadline of a thread on the seeded Episode (run reference + 24 h).
DEADLINE = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)
MISSING_MODES = ({}, {"meta_mode": ""}, {"meta_mode": "production"})


class ScaleWorkerJobTestBase(LibraryFixtures):
    def _seed(self) -> SeededRun:
        workspace_id = f"ws_scale_eval_{uuid.uuid4().hex}"
        self.provision_workspace(workspace_id)
        self.addCleanup(cancel_workspace_jobs, workspace_id)
        with tenant_session(worker_context(workspace_id)) as session:
            return seed_evaluable_run(session, workspace_id)

    @staticmethod
    def _run_one(workspace_id: str, runtime_name: str = "WorkerRuntime") -> bool:
        """One tick of the named runtime, with every other Workspace's Job deferred."""
        quarantine_foreign_jobs(workspace_id)
        runtime = RUNTIMES[runtime_name](owner=f"ci-only-scale-{uuid.uuid4().hex[:6]}")
        handled = runtime.run_once()
        return asyncio.run(handled) if inspect.isawaitable(handled) else handled

    @staticmethod
    def _enqueue_evaluation(seeded: SeededRun) -> JobRecord:
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            job = enqueue_scale_evaluation(session, context, seeded.run_id)
            session.expunge(job)
            return job

    @staticmethod
    def _enqueue_raw(workspace_id: str, job_type: str, payload: dict) -> str:
        """A Job with a payload no enqueue function builds."""
        context = worker_context(workspace_id)
        with tenant_session(context) as session:
            return jobs.enqueue_in_session(
                session, context=context, job_type=job_type, payload=payload,
                idempotency_key=f"ci-only-{job_type}-{uuid.uuid4().hex}",
            ).id

    @staticmethod
    def _job(job_id: str) -> tuple[str, str]:
        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            return job.state, job.error_code

    @staticmethod
    def _run(seeded: SeededRun) -> tuple:
        with platform_session() as session:
            run = session.get(ScaleEvaluationRunRecord, seeded.run_id)
            return run.run_state, run.outcome_kind, run.snapshot_id, run.evaluation_id

    def _assert_untouched(self, seeded: SeededRun) -> None:
        self.assertEqual(self._run(seeded), ("requested", None, None, None))
        with platform_session() as session:
            episode = session.get(ScaleEpisodeRecord, seeded.episode_id)
            self.assertEqual((episode.phase, episode.phase_run_id), ("collecting", None))
            self.assertEqual(session.scalar(select(func.count()).select_from(ScaleEvidenceSnapshotRecord).where(
                ScaleEvidenceSnapshotRecord.workspace_id == seeded.workspace_id)), 0)

    def _assert_evaluated(self, seeded: SeededRun) -> str:
        run_state, outcome_kind, snapshot_id, evaluation_id = self._run(seeded)
        self.assertEqual((run_state, outcome_kind), ("evaluated", "evaluated"))
        self.assertIsNotNone(snapshot_id)
        with platform_session() as session:
            episode = session.get(ScaleEpisodeRecord, seeded.episode_id)
            self.assertEqual((episode.phase, episode.phase_run_id, episode.accepted_evaluation_id,
                              episode.projection_revision), ("collecting", None, evaluation_id, 1))

            def count(model, column, value):
                return session.scalar(select(func.count()).select_from(model).where(column == value))

            self.assertEqual(count(ScaleEvaluationSignalRecord, ScaleEvaluationSignalRecord.evaluation_id,
                                   evaluation_id), 10)
            self.assertEqual(count(ScaleEvaluationStreakCounterRecord,
                                   ScaleEvaluationStreakCounterRecord.evaluation_id, evaluation_id), 10)
            self.assertEqual(count(ScaleEpisodeProjectionRecord, ScaleEpisodeProjectionRecord.episode_id,
                                   seeded.episode_id), 1)
            self.assertEqual(session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.resource_id == seeded.run_id)).all(), ["scale_evaluation_evaluated"])
        return evaluation_id


class TheEvaluationJob(ScaleWorkerJobTestBase):
    def test_the_job_names_its_run_and_meta_mode_under_the_spec_key(self):
        seeded = self._seed()
        job = self._enqueue_evaluation(seeded)

        self.assertEqual((job.type, job.state), ("scale_evaluate", "queued"))
        self.assertEqual(job.payload_json, {"evaluation_run_id": seeded.run_id, "meta_mode": MODE})
        self.assertEqual(job.idempotency_key, f"scale-eval:{seeded.run_id}:beta-v1")
        self.assertEqual((job.resource_type, job.resource_id), ("scale_evaluation_run", seeded.run_id))
        self.assertEqual(self._enqueue_evaluation(seeded).id, job.id, "the key names one Job")

    def test_both_runtimes_evaluate_a_queued_run(self):
        for runtime_name in RUNTIMES:
            with self.subTest(runtime=runtime_name):
                seeded = self._seed()
                job = self._enqueue_evaluation(seeded)

                self.assertTrue(self._run_one(seeded.workspace_id, runtime_name))

                self.assertEqual(self._job(job.id), ("succeeded", ""))
                self._assert_evaluated(seeded)

    def test_a_job_without_a_valid_meta_mode_fails_as_missing_input_and_changes_nothing(self):
        for runtime_name in RUNTIMES:
            for mode in MISSING_MODES:
                with self.subTest(runtime=runtime_name, payload=mode):
                    seeded = self._seed()
                    job_id = self._enqueue_raw(
                        seeded.workspace_id, "scale_evaluate", {"evaluation_run_id": seeded.run_id, **mode})

                    self.assertTrue(self._run_one(seeded.workspace_id, runtime_name))

                    self.assertEqual(self._job(job_id), ("failed", "missing_input"))
                    self._assert_untouched(seeded)

    def test_a_job_naming_another_mode_or_workspace_finds_no_run(self):
        seeded = self._seed()
        other = self._seed()
        for workspace_id, mode in ((seeded.workspace_id, "customer_sandbox"), (other.workspace_id, MODE)):
            with self.subTest(workspace=workspace_id, mode=mode):
                job_id = self._enqueue_raw(
                    workspace_id, "scale_evaluate", {"evaluation_run_id": seeded.run_id, "meta_mode": mode})

                self.assertTrue(self._run_one(workspace_id))

                self.assertEqual(self._job(job_id), ("failed", "run_not_found"))
                self._assert_untouched(seeded)

    def test_another_workspace_or_mode_cannot_enqueue_the_run(self):
        seeded = self._seed()
        other = self._seed()
        for context in (worker_context(other.workspace_id), worker_context(seeded.workspace_id, "customer_sandbox")):
            with self.subTest(workspace=context.workspace_id, mode=context.meta_mode):
                with self.assertRaises(ScaleOrchestrationError) as caught, tenant_session(context) as session:
                    enqueue_scale_evaluation(session, context, seeded.run_id)
                self.assertEqual(caught.exception.code, "run_not_found")
        with platform_session() as session:
            self.assertEqual(session.scalars(select(JobRecord.id).where(
                JobRecord.resource_id == seeded.run_id)).all(), [])


class EachPhaseCarriesTheTenantContext(ScaleWorkerJobTestBase):
    """#96 on SQLite: every statement of an evaluation runs in a transaction
    that carries the session's context, and Phase B computes with none open."""

    def test_no_statement_runs_outside_a_bound_transaction(self):
        seeded = self._seed()
        context = worker_context(seeded.workspace_id)
        unbound: list[str] = []
        computing_in_transaction: list[bool] = []
        write_settings = tenancy._write_settings
        evaluate_scale = service_module.evaluate_scale

        with tenant_session(context) as session:
            bound = {"now": True}  # tenant_session bound its first transaction

            def wrote_settings(target, values):
                if target is session:
                    bound["now"] = True
                return write_settings(target, values)

            def transaction_ended(_session, transaction):
                if transaction.parent is None:
                    bound["now"] = False

            def statement(_conn, _cursor, sql, *_args):
                if not bound["now"]:
                    unbound.append(sql.split("\n", 1)[0])

            def evaluate(snapshot):
                computing_in_transaction.append(session.in_transaction())
                return evaluate_scale(snapshot)

            engine = session.get_bind()
            event.listen(session, "after_transaction_end", transaction_ended)
            event.listen(engine, "before_cursor_execute", statement)
            try:
                with mock.patch.object(tenancy, "_write_settings", side_effect=wrote_settings), \
                        mock.patch.object(service_module, "evaluate_scale", side_effect=evaluate):
                    outcome = run_scale_evaluation(session, context, seeded.run_id)
            finally:
                event.remove(engine, "before_cursor_execute", statement)
                event.remove(session, "after_transaction_end", transaction_ended)

        self.assertEqual(outcome.kind, RunOutcomeKind.EVALUATED)
        self.assertEqual(computing_in_transaction, [False])
        self.assertEqual(unbound, [])
        self._assert_evaluated(seeded)


class TheAdviceExpiryJob(ScaleWorkerJobTestBase):
    def _seed_thread(self) -> tuple[SeededRun, str]:
        seeded = self._seed()
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            evaluation_id = run_scale_evaluation(session, context, seeded.run_id).evaluation_id
        with tenant_session(context) as session:
            return seeded, seed_active_thread(session, context, seeded, evaluation_id)

    @staticmethod
    def _enqueue_expiry(seeded: SeededRun, thread_id: str) -> JobRecord | None:
        context = worker_context(seeded.workspace_id)
        with tenant_session(context) as session:
            job = enqueue_advice_expiry(session, context, thread_id)
            if job is not None:
                session.expunge(job)
            return job

    @staticmethod
    def _thread(thread_id: str) -> tuple:
        with platform_session() as session:
            thread = session.get(ScaleAdviceThreadRecord, thread_id)
            events = session.scalars(select(ScaleAdviceThreadEventRecord.event_type).where(
                ScaleAdviceThreadEventRecord.thread_id == thread_id,
                ScaleAdviceThreadEventRecord.event_type == "expired")).all()
            return thread.lifecycle, thread.lifecycle_reason if thread.lifecycle == "expired" else None, events

    def test_the_job_names_its_thread_and_meta_mode_under_a_deadline_key(self):
        seeded, thread_id = self._seed_thread()
        job = self._enqueue_expiry(seeded, thread_id)

        self.assertEqual((job.type, job.state), ("scale_advice_expire", "queued"))
        self.assertEqual(job.payload_json, {"thread_id": thread_id, "meta_mode": MODE})
        self.assertEqual(job.idempotency_key, f"scale-advice-expire:{thread_id}:{DEADLINE.isoformat()}")
        self.assertEqual((job.resource_type, job.resource_id), ("scale_advice_thread", thread_id))
        self.assertEqual(self._enqueue_expiry(seeded, thread_id).id, job.id, "the key names one Job")

    def test_both_runtimes_expire_a_thread_past_its_deadline(self):
        for runtime_name in RUNTIMES:
            with self.subTest(runtime=runtime_name):
                seeded, thread_id = self._seed_thread()
                job = self._enqueue_expiry(seeded, thread_id)

                self.assertTrue(self._run_one(seeded.workspace_id, runtime_name))

                self.assertEqual(self._job(job.id), ("succeeded", ""))
                self.assertEqual(self._thread(thread_id),
                                 ("expired", "freshness_deadline_exceeded", ["expired"]))
                self.assertIsNone(self._enqueue_expiry(seeded, thread_id), "an expired thread has no deadline")

    def test_a_job_without_a_valid_meta_mode_fails_as_missing_input_and_changes_nothing(self):
        seeded, thread_id = self._seed_thread()
        for runtime_name in RUNTIMES:
            for mode in MISSING_MODES:
                with self.subTest(runtime=runtime_name, payload=mode):
                    job_id = self._enqueue_raw(
                        seeded.workspace_id, "scale_advice_expire", {"thread_id": thread_id, **mode})

                    self.assertTrue(self._run_one(seeded.workspace_id, runtime_name))

                    self.assertEqual(self._job(job_id), ("failed", "missing_input"))
                    self.assertEqual(self._thread(thread_id), ("active", None, []))

    def test_another_workspace_or_mode_neither_enqueues_nor_expires_the_thread(self):
        seeded, thread_id = self._seed_thread()
        other = self._seed()
        for context in (worker_context(other.workspace_id), worker_context(seeded.workspace_id, "customer_sandbox")):
            with self.subTest(workspace=context.workspace_id, mode=context.meta_mode):
                with self.assertRaises(AdviceLifecycleError) as caught, tenant_session(context) as session:
                    enqueue_advice_expiry(session, context, thread_id)
                self.assertEqual(caught.exception.code, "thread_not_found")

                job_id = self._enqueue_raw(context.workspace_id, "scale_advice_expire",
                                           {"thread_id": thread_id, "meta_mode": context.meta_mode})
                self.assertTrue(self._run_one(context.workspace_id))
                self.assertEqual(self._job(job_id), ("failed", "thread_not_found"))
                self.assertEqual(self._thread(thread_id), ("active", None, []))

    def test_a_job_before_the_deadline_changes_nothing(self):
        seeded, thread_id = self._seed_thread()
        job = self._enqueue_expiry(seeded, thread_id)
        with platform_session() as session:  # newer evidence moved the deadline
            session.execute(update(ScaleEvaluationRunRecord).where(
                ScaleEvaluationRunRecord.id == seeded.run_id).values(
                freshness_reference_at=datetime.now(timezone.utc)))

        self.assertTrue(self._run_one(seeded.workspace_id))

        self.assertEqual(self._job(job.id), ("succeeded", ""))
        self.assertEqual(self._thread(thread_id), ("active", None, []))
