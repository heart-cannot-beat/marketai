"""The Worker's manual fact import and its route's replay (#397).

An import turns one finalized upload into a new Manual Fact Set draft. The
draft and the Operation settle in one transaction under the Operation's row
lock, so the Job is safe to requeue after a crash: a rerun finds either both
written or neither. A replayed key returns the Operation it created when the
data source, the upload and the initiator are all the same; an upload already
imported under another key is ``upload_intent_consumed`` unless that import
failed or was cancelled.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    JobRecord,
    LibraryEntryRecord,
    UploadIntentRecord,
    new_id,
    utcnow,
)
from app.library import service
from app.library.contracts import ENTRY_DATA_SOURCE, ENTRY_MANUAL_FACT_SET, ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.operations import (
    OperationResultRef,
    OperationState,
    OperationStateEvent,
    create_operation,
    transition_operation,
)
from app.worker_runtime import WorkerRuntime

JOB_TYPE = "library.manual_fact_import"
RESULT_TYPE = "manual_fact_set_draft"
FACTS = {"facts": [{
    "fact_id": "f1", "fact_type": "price", "country_code": "US",
    "valid_from": "2026-01-01", "valid_to": "2027-01-01", "amount": "9.99", "currency": "USD",
}]}


class MemoryBlobStore:
    """Committed objects by key; ``on_read`` runs as the Worker opens one."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.on_read = None

    async def open_committed(self, ref):
        if self.on_read is not None:
            self.on_read()
        yield self.objects[ref.committed_key]


class ManualFactImportFixtures(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        self.act_as(self.ctx)
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.store = MemoryBlobStore()
        # Migration tests run Alembic in this process, and its `fileConfig`
        # disables every already-imported logger (see `tests.test_runtime_wiring`).
        logger = logging.getLogger("app.worker_runtime")
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False

    def act_as(self, ctx):
        app.dependency_overrides[request_tenant_context] = lambda: ctx

    def data_source(self, ctx=None):
        return service.create_entry(
            ctx or self.ctx, entry_type=ENTRY_DATA_SOURCE, display_name="DS manual",
            content={"adapter_kind": "manual", "execution_mode": "real", "config": {}},
        )["id"]

    def upload(self, data_source_id, body=FACTS, ctx=None):
        """A finalized fact-import upload of ``body`` owned by the data source; returns its id."""
        ctx = ctx or self.ctx
        raw = json.dumps(body).encode()
        committed_key = f"committed/{ctx.workspace_id}/library_manual_fact_import/{uuid.uuid4().hex}.json"
        self.store.objects[committed_key] = raw
        intent_id = new_id("ui")
        now = utcnow()
        with tenant_session(ctx) as session:
            session.add(UploadIntentRecord(
                id=intent_id, workspace_id=ctx.workspace_id, initiator_id=ctx.initiator_id,
                authorization_epoch_at_issue=0, owner_kind="data_source", owner_id=data_source_id,
                staging_key=f"staging/{ctx.workspace_id}/{uuid.uuid4().hex}.dat",
                purpose="library_manual_fact_import", filename_original="facts.json",
                content_type="application/json", content_length_min=len(raw), content_length_max=len(raw),
                checksum_sha256_b64=base64.b64encode(hashlib.sha256(raw).digest()).decode(),
                state="finalized", committed_blob_ref_key=committed_key, committed_blob_ref_version="v1",
                created_at=now, expires_at=now + timedelta(minutes=5), finalized_at=now,
            ))
        return intent_id

    def import_(self, data_source_id, upload_id, key=None, default_offer=None):
        body = {"upload_intent_id": upload_id}
        if default_offer is not None:
            body["default_offer_entry_id"] = default_offer
        return self.client.post(
            f"/api/v1/library/entries/{data_source_id}/manual-fact-imports",
            json=body,
            headers={"Idempotency-Key": key or f"ci-only-factimport-{uuid.uuid4().hex}"},
        )

    def drain(self):
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"factimport-{uuid.uuid4().hex[:6]}")
        runtime.blob_store = self.store
        ran = 0
        while asyncio.run(runtime.run_once(job_types=(JOB_TYPE,))):
            ran += 1
        return ran

    def operation(self, operation_id):
        response = self.client.get(f"/api/v1/operations/{operation_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def reasons(self, operation_id):
        with tenant_session(self.ctx) as session:
            return session.scalars(
                select(OperationStateEvent.reason)
                .where(OperationStateEvent.operation_id == operation_id)
                .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
            ).all()

    def fact_sets(self, ctx=None):
        ctx = ctx or self.ctx
        with tenant_session(ctx) as session:
            return session.scalars(
                select(LibraryEntryRecord.id).where(
                    LibraryEntryRecord.workspace_id == ctx.workspace_id,
                    LibraryEntryRecord.entry_type == ENTRY_MANUAL_FACT_SET,
                )
            ).all()

    def jobs(self, ctx=None):
        ctx = ctx or self.ctx
        with tenant_session(ctx) as session:
            return session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == ctx.workspace_id, JobRecord.type == JOB_TYPE,
                )
            ).all()

    def requeue_settled_job(self):
        """The Worker settled the Operation, then crashed before the Job; recovery requeued it."""
        with platform_session() as session:
            session.execute(
                update(JobRecord)
                .where(JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == JOB_TYPE)
                .values(state="queued", finished_at=None)
            )

    def assert_problem(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code)
        self.assertEqual(response.headers["content-type"], "application/problem+json")


class TestManualFactImportReplay(ManualFactImportFixtures):
    def test_a_replayed_key_returns_the_same_operation_and_one_job(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        key = "ci-only-factimport-replay"
        first = self.import_(data_source, upload, key)
        second = self.import_(data_source, upload, key)
        self.assertEqual((first.status_code, second.status_code), (202, 202), second.text)
        self.assertEqual(second.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(len(self.jobs()), 1)
        self.drain()
        third = self.import_(data_source, upload, key)
        self.assertEqual(third.status_code, 202, third.text)
        self.assertEqual(third.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(third.json()["state"], "succeeded", "a replay reports where the import is")
        self.assertEqual(len(self.fact_sets()), 1)

    def test_a_replay_after_the_data_source_was_archived_still_returns_the_operation(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        key = "ci-only-factimport-archived-replay"
        first = self.import_(data_source, upload, key)
        entry = service.get_entry(self.ctx, data_source)
        service.archive_entry(self.ctx, data_source, expected_revision=entry["draft"]["revision"])
        replay = self.import_(data_source, upload, key)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["operation_id"], first.json()["operation_id"])

    def test_a_key_reused_with_another_upload_is_a_conflict(self):
        data_source = self.data_source()
        key = "ci-only-factimport-other-upload"
        self.assertEqual(self.import_(data_source, self.upload(data_source), key).status_code, 202)
        self.assert_problem(
            self.import_(data_source, self.upload(data_source), key), 409, "idempotency_key_reused",
        )
        self.assertEqual(len(self.jobs()), 1)

    def test_a_key_reused_for_another_data_source_is_a_conflict(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        key = "ci-only-factimport-other-source"
        self.assertEqual(self.import_(data_source, upload, key).status_code, 202)
        self.assert_problem(self.import_(self.data_source(), upload, key), 409, "idempotency_key_reused")
        self.assertEqual(len(self.jobs()), 1)

    def test_a_key_reused_by_another_member_is_a_conflict(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        key = "ci-only-factimport-other-member"
        self.assertEqual(self.import_(data_source, upload, key).status_code, 202)
        self.act_as(self._make_context(workspace_id=self.ctx.workspace_id, role=ROLE_MEMBER, actor="user_b"))
        self.assert_problem(self.import_(data_source, upload, key), 409, "idempotency_key_reused")
        self.assertEqual(len(self.jobs()), 1)


class TestManualFactImportUploadConsumed(ManualFactImportFixtures):
    def test_an_upload_cannot_be_imported_again_under_another_key(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        self.assertEqual(self.import_(data_source, upload).status_code, 202)
        while_queued = self.import_(data_source, upload)
        self.drain()
        after = self.import_(data_source, upload)
        for response in (while_queued, after):
            self.assert_problem(response, 409, "upload_intent_consumed")
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(self.fact_sets()), 1)

    def test_an_upload_whose_import_failed_can_be_imported_again(self):
        data_source = self.data_source()
        upload = self.upload(data_source, body={"facts": []})
        first = self.import_(data_source, upload)
        self.drain()
        self.assertEqual(self.operation(first.json()["operation_id"])["state"], "failed")
        again = self.import_(data_source, upload)
        self.assertEqual(again.status_code, 202, again.text)
        self.assertNotEqual(again.json()["operation_id"], first.json()["operation_id"])

    def test_an_upload_whose_import_was_cancelled_can_be_imported_again(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        self.assertEqual(self.import_(data_source, upload).status_code, 202)
        cancel_workspace_jobs(self.ctx.workspace_id)
        again = self.import_(data_source, upload)
        self.assertEqual(again.status_code, 202, again.text)


class TestManualFactImportWorker(ManualFactImportFixtures):
    def test_an_import_names_the_fact_set_it_created(self):
        data_source = self.data_source()
        upload = self.upload(data_source)
        accepted = self.import_(data_source, upload)
        self.assertEqual(self.drain(), 1)
        operation = self.operation(accepted.json()["operation_id"])
        [fact_set] = self.fact_sets()
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(operation["result"], {"resource_type": RESULT_TYPE, "resource_id": fact_set})
        self.assertEqual(service.get_entry(self.ctx, fact_set)["draft"]["content"]["upload_intent_id"], upload)
        [job] = self.jobs()
        self.assertEqual(job.state, "succeeded")

    def test_a_bad_file_fails_with_a_stable_reason_and_writes_nothing(self):
        data_source = self.data_source()
        accepted = self.import_(data_source, self.upload(data_source, body={"facts": []}))
        operation_id = accepted.json()["operation_id"]
        # A file the user has to fix is an expected outcome, not a Worker fault.
        with self.assertNoLogs("app.worker_runtime", level="WARNING"):
            self.drain()
        self.assertEqual(self.operation(operation_id)["state"], "failed")
        self.assertEqual(self.reasons(operation_id)[-1], "manual_fact_import_failed")
        [job] = self.jobs()
        self.assertEqual((job.state, job.error_code), ("failed", "manual_fact_import_failed"))
        self.assertIn("至少需要一条事实", job.error_detail, "the diagnostic stays on the Job")
        self.assertEqual(self.fact_sets(), [])

    def test_a_fault_before_the_operation_settles_leaves_no_fact_set(self):
        data_source = self.data_source()
        accepted = self.import_(data_source, self.upload(data_source))
        operation_id = accepted.json()["operation_id"]

        def settling_fails(session, operation_id, to_state, *args, **kwargs):
            if to_state == OperationState.SUCCEEDED:
                raise RuntimeError("ci-only simulated fault")
            return transition_operation(session, operation_id, to_state, *args, **kwargs)

        with patch("app.worker_runtime.transition_operation", side_effect=settling_fails), \
             self.assertLogs("app.worker_runtime", level="WARNING") as logged:
            self.drain()
        self.assertEqual(self.fact_sets(), [], "the draft commits with the Operation or not at all")
        self.assertEqual(self.operation(operation_id)["state"], "failed")
        self.assertEqual(self.reasons(operation_id)[-1], "manual_fact_import_failed")
        [message] = logged.output
        self.assertIn(f"operation={operation_id}", message)
        self.assertIn(f"data_source={data_source}", message)
        self.assertIn("error=RuntimeError", message)

    def test_the_job_is_requeued_after_a_crash_and_imports_once(self):
        data_source = self.data_source()
        accepted = self.import_(data_source, self.upload(data_source))
        operation_id = accepted.json()["operation_id"]
        [job] = self.jobs()
        self.assertTrue(job.safe_to_retry_after_crash)
        # A Worker took the Job, started the Operation and died.
        quarantine_foreign_jobs(self.ctx.workspace_id)
        claimed = jobs.claim_next("crashed-worker", job_types=(JOB_TYPE,))
        self.assertEqual(claimed.id, job.id)
        jobs.mark_running(claimed.context, claimed.id, "crashed-worker")
        with tenant_session(claimed.context) as session:
            transition_operation(
                session, operation_id, OperationState.RUNNING, reason=None, actor=claimed.context.executor,
            )
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == job.id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
        jobs.recover_expired_jobs()
        [job] = self.jobs()
        self.assertEqual((job.state, job.error_code), ("queued", "lease_expired"),
                         "an import is requeued, not left uncertain")
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.operation(operation_id)["state"], "succeeded")
        self.assertEqual(len(self.fact_sets()), 1)

    def test_a_rerun_after_the_operation_settled_imports_nothing_twice(self):
        data_source = self.data_source()
        accepted = self.import_(data_source, self.upload(data_source))
        self.drain()
        before = self.operation(accepted.json()["operation_id"])
        self.requeue_settled_job()
        self.assertEqual(self.drain(), 1)
        [job] = self.jobs()
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(self.operation(accepted.json()["operation_id"]), before)
        self.assertEqual(len(self.fact_sets()), 1)

    def test_a_rerun_after_the_import_failed_keeps_the_upload_importable(self):
        data_source = self.data_source()
        upload = self.upload(data_source, body={"facts": []})
        accepted = self.import_(data_source, upload)
        self.drain()
        self.requeue_settled_job()
        self.assertEqual(self.drain(), 1)
        [job] = self.jobs()
        self.assertEqual((job.state, job.error_code), ("failed", "manual_fact_import_failed"))
        self.assertEqual(self.operation(accepted.json()["operation_id"])["state"], "failed")
        self.assertEqual(self.import_(data_source, upload).status_code, 202)

    def test_a_run_that_finds_the_operation_settled_meanwhile_writes_nothing(self):
        data_source = self.data_source()
        accepted = self.import_(data_source, self.upload(data_source))
        operation_id = accepted.json()["operation_id"]

        def another_run_settles_it():
            # A second Worker took over the expired lease and finished first.
            with tenant_session(self.ctx) as session:
                transition_operation(
                    session, operation_id, OperationState.SUCCEEDED, reason=None, actor="system:worker",
                    result_ref=OperationResultRef(resource_type=RESULT_TYPE, resource_id="lib_other_run"),
                )

        self.store.on_read = another_run_settles_it
        self.drain()
        self.assertEqual(self.fact_sets(), [])
        operation = self.operation(operation_id)
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(operation["result"]["resource_id"], "lib_other_run")
        [job] = self.jobs()
        self.assertEqual(job.state, "succeeded")


class TestManualFactImportTenancy(ManualFactImportFixtures):
    def setUp(self):
        super().setUp()
        self.other = self._make_context(role=ROLE_OWNER, actor="user_b")
        self.addCleanup(cancel_workspace_jobs, self.other.workspace_id)

    def test_another_workspaces_data_source_is_not_found(self):
        foreign = self.data_source(ctx=self.other)
        response = self.import_(foreign, self.upload(foreign, ctx=self.other))
        self.assert_problem(response, 404, "library_not_found")
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.jobs(self.other), [])

    def test_another_workspaces_upload_imports_nothing(self):
        foreign = self.data_source(ctx=self.other)
        foreign_upload = self.upload(foreign, ctx=self.other)
        accepted = self.import_(self.data_source(), foreign_upload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.drain()
        operation_id = accepted.json()["operation_id"]
        self.assertEqual(self.operation(operation_id)["state"], "failed")
        self.assertEqual(self.reasons(operation_id)[-1], "manual_fact_import_failed")
        self.assertEqual(self.fact_sets(), [])
        self.assertEqual(self.fact_sets(self.other), [])

    def test_a_forged_job_cannot_import_into_another_workspaces_data_source(self):
        foreign = self.data_source(ctx=self.other)
        # Only a forged payload could name it: the route reads the entry first.
        upload = self.upload(foreign)
        with tenant_session(self.ctx) as session:
            operation = create_operation(session, command_type="manual_fact_import")
            jobs.enqueue_in_session(
                session, context=self.ctx, job_type=JOB_TYPE,
                payload={"data_source_entry_id": foreign, "operation_id": operation.id,
                         "upload_intent_id": upload},
                idempotency_key="manual_fact_import:ci-only-forged",
                resource_type="data_source", resource_id=foreign,
                safe_to_retry_after_crash=True, max_attempts=1,
            )
            operation_id = operation.id
        self.drain()
        self.assertEqual(self.operation(operation_id)["state"], "failed")
        [job] = self.jobs()
        self.assertEqual((job.state, job.error_code), ("failed", "manual_fact_import_failed"))
        self.assertEqual(self.fact_sets(), [])
        self.assertEqual(self.fact_sets(self.other), [])
