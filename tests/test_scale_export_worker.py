"""The deployed Worker runs Scale exports (#55).

Production and docker compose run one Worker, ``app.worker_runtime.
WorkerRuntime`` (``python -m worker.main``). It used to fail every
``scale_export`` Job as ``unknown_job_type``, so each export's Operation
stayed ``queued``. These cases drive the Job through that Worker's own queue
claim on SQLite. ``tests/test_job_crash_recovery.py`` crashes it, and
``tests/test_postgres_architecture.py`` runs it as the Worker role under
FORCE RLS.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest import mock

from sqlalchemy import select, update

from tests import test_scale_exports as scale_export_tests
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import JobRecord
from app.platform import jobs
from app.platform.operations import OperationRecord, OperationStateEvent
from app.platform.tenancy import TenantContext
from app.scale.exports import create_export
from app.scale.models import ScaleExportRecord
from app.worker_runtime import WorkerRuntime

SETTLED = [(None, "queued"), ("queued", "running"), ("running", "succeeded")]


class TestScaleExportOnTheDeployedWorker(LibraryFixtures):
    def setUp(self):
        self.ctx = self._scale_context()

    def _scale_context(self) -> TenantContext:
        workspace_id = f"ws_scale_export_{uuid.uuid4().hex}"
        self.provision_workspace(workspace_id)
        self.addCleanup(cancel_workspace_jobs, workspace_id)
        return scale_export_tests._owner_context(workspace_id)

    def _request_export(self, ctx: TenantContext) -> tuple[str, str, str]:
        with tenant_session(ctx) as session:
            enrollment_id, episode_id = scale_export_tests._seed_enrollment_and_episode(
                session, ctx.workspace_id,
            )
            result = create_export(
                session, ctx, enrollment_id=enrollment_id, episode_id=episode_id,
                export_kind="evidence_summary", idempotency_key=f"ci-only-export-{uuid.uuid4().hex}",
            )
            job_id = session.scalars(
                select(JobRecord.id).where(
                    JobRecord.workspace_id == ctx.workspace_id,
                    JobRecord.type == "scale_export",
                    JobRecord.resource_id == result.export_id,
                )
            ).one()
        return result.export_id, result.operation_id, job_id

    def _run_one(self, workspace_id: str, runtime: WorkerRuntime | None = None) -> bool:
        """One tick of the deployed Worker, with every other Workspace's Job deferred."""
        quarantine_foreign_jobs(workspace_id)
        runtime = runtime or WorkerRuntime(owner=f"ci-only-worker-{uuid.uuid4().hex[:6]}")
        return asyncio.run(runtime.run_once())

    def _job(self, job_id: str) -> JobRecord:
        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            session.expunge(job)
            return job

    def _observe(self, ctx: TenantContext, export_id: str, operation_id: str) -> dict:
        with tenant_session(ctx) as session:
            export = session.get(ScaleExportRecord, export_id)
            operation = session.get(OperationRecord, operation_id)
            events = [
                (row.from_state, row.to_state)
                for row in session.scalars(
                    select(OperationStateEvent)
                    .where(OperationStateEvent.operation_id == operation_id)
                    .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
                )
            ]
            return {
                "completed": export.completed_at is not None,
                "result": export.result_object_reference,
                "operation": operation.state,
                "result_ref": (operation.result_resource_type, operation.result_resource_id),
                "operation_events": events,
            }

    def test_the_deployed_worker_completes_a_requested_export(self):
        export_id, operation_id, job_id = self._request_export(self.ctx)

        self.assertTrue(self._run_one(self.ctx.workspace_id))

        job = self._job(job_id)
        self.assertEqual((job.state, job.error_code), ("succeeded", ""))
        observed = self._observe(self.ctx, export_id, operation_id)
        self.assertTrue(observed["completed"])
        self.assertTrue(observed["result"])
        self.assertEqual(observed["operation"], "succeeded")
        self.assertEqual(observed["result_ref"], ("scale_export", export_id))
        self.assertEqual(observed["operation_events"], SETTLED)

    def test_the_job_names_the_meta_mode_its_export_lives_in(self):
        # The claimed context is frozen from the Job row, which has no
        # meta_mode; scale_exports' RLS policy shows a row only to a context
        # that names its mode, so the Job has to carry it (as Launch's do).
        export_id, _, job_id = self._request_export(self.ctx)

        self.assertEqual(
            self._job(job_id).payload_json,
            {"export_id": export_id, "meta_mode": "customer_sandbox"},
        )

    def test_a_job_without_a_meta_mode_fails_and_writes_nothing(self):
        # What the API enqueued before #55: under RLS such a run would see no
        # export at all, so it fails instead of succeeding without one.
        export_id, operation_id, job_id = self._request_export(self.ctx)
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == job_id)
                .values(payload_json={"export_id": export_id})
            )

        self.assertTrue(self._run_one(self.ctx.workspace_id))

        job = self._job(job_id)
        self.assertEqual((job.state, job.error_code), ("failed", "missing_input"))
        observed = self._observe(self.ctx, export_id, operation_id)
        self.assertFalse(observed["completed"])
        self.assertEqual(observed["operation_events"], [(None, "queued")])

    def test_another_workspaces_job_cannot_complete_this_workspaces_export(self):
        export_id, operation_id, job_id = self._request_export(self.ctx)
        other = self._scale_context()
        # Only a forged payload could name this Workspace's export from another.
        with tenant_session(other) as session:
            forged = jobs.enqueue_in_session(
                session, context=other, job_type="scale_export",
                payload={"export_id": export_id, "meta_mode": "customer_sandbox"},
                idempotency_key=f"ci-only-forged-{uuid.uuid4().hex}",
                resource_type="scale_export", resource_id=export_id,
            ).id

        self.assertTrue(self._run_one(other.workspace_id))

        self.assertIn(self._job(forged).state, ("succeeded", "failed"), "the forged Job is closed")
        observed = self._observe(self.ctx, export_id, operation_id)
        self.assertFalse(observed["completed"])
        self.assertEqual(observed["operation"], "queued")
        self.assertEqual(observed["operation_events"], [(None, "queued")])
        self.assertEqual(self._job(job_id).state, "queued")

        # The Workspace's own Job, deferred while the other one ran, still
        # settles its export.
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == job_id).values(next_attempt_at=None)
            )
        self.assertTrue(self._run_one(self.ctx.workspace_id))
        self.assertEqual(self._observe(self.ctx, export_id, operation_id)["operation_events"], SETTLED)

    def test_an_unknown_job_type_still_fails_as_unknown_job_type(self):
        job_id = f"job_{uuid.uuid4().hex}"
        with tenant_session(self.ctx) as session:
            session.add(JobRecord(
                id=job_id, workspace_id=self.ctx.workspace_id,
                initiator_id=self.ctx.initiator_id, initiator_role=self.ctx.audit_initiator_role,
                type="ci-only.unregistered", payload_json={},
                idempotency_key=f"ci-only-unknown-{uuid.uuid4().hex}", state="queued",
            ))

        self.assertTrue(self._run_one(self.ctx.workspace_id))

        job = self._job(job_id)
        self.assertEqual((job.state, job.error_code), ("failed", "unknown_job_type"))

    def test_an_export_never_reaches_the_workers_meta_clients(self):
        # Scale never writes to Meta: the shared Worker's Meta clients stay untouched.
        meta_client, launch_adapter = mock.Mock(), mock.Mock()
        runtime = WorkerRuntime(
            owner=f"ci-only-worker-{uuid.uuid4().hex[:6]}",
            meta_client=meta_client, launch_adapter=launch_adapter,
        )
        export_id, operation_id, _ = self._request_export(self.ctx)

        self.assertTrue(self._run_one(self.ctx.workspace_id, runtime))

        self.assertEqual(self._observe(self.ctx, export_id, operation_id)["operation"], "succeeded")
        self.assertEqual(meta_client.mock_calls, [])
        self.assertEqual(launch_adapter.mock_calls, [])
