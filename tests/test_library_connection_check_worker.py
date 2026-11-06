"""The Worker's DataSource connection check and its read-back (#383).

The check is local: the effective Published Version, its content, whether its
adapter is implemented and the bound CredentialVersion's state. Every finished
check is ``succeeded`` with one append-only audit event as its result; the
conclusion lives in the event, which ``GET .../connection-checks/{check_id}``
serves. Replays reuse the Operation, and a Job rerun after its Operation
settled records nothing twice.
"""
from __future__ import annotations

import asyncio
import socket
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_library_datasource_api import _provision_credential_version

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, utcnow
from app.entitlements.credentials import CredentialVersionRecord
from app.library import connection_checks, service
from app.library.contracts import ENTRY_DATA_SOURCE, ROLE_MEMBER, ROLE_OWNER
from app.library.datasources import bind_credential
from app.main import app
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.operations import create_operation, get_operation
from app.worker_runtime import WorkerRuntime

JOB_TYPE = "library.connection_check"
DETAIL_KEYS = {
    "check_version", "health", "reason_code", "adapter_kind", "adapter_version",
    "execution_mode", "data_source_version_id", "bound_version_id",
    "bound_version_state", "requested_mode", "operation_id",
}


class ConnectionCheckFixtures(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def data_source(self, adapter_kind="manual", execution_mode="real", *, publish=True, ctx=None):
        ctx = ctx or self.ctx
        entry = service.create_entry(
            ctx, entry_type=ENTRY_DATA_SOURCE, display_name=f"DS {adapter_kind}",
            content={"adapter_kind": adapter_kind, "execution_mode": execution_mode, "config": {}},
        )
        if publish:
            service.publish(ctx, entry["id"], expected_revision=entry["draft"]["revision"])
        return entry["id"]

    def check(self, entry_id, key=None, mode="real"):
        return self.client.post(
            f"/api/v1/library/entries/{entry_id}/connection-checks",
            json={} if mode is None else {"execution_mode": mode},
            headers={"Idempotency-Key": key or f"ci-only-conncheck-{uuid.uuid4().hex}"},
        )

    def drain(self):
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"conncheck-{uuid.uuid4().hex[:6]}")
        ran = 0
        while asyncio.run(runtime.run_once(job_types=(JOB_TYPE,))):
            ran += 1
        return ran

    def operation(self, operation_id):
        response = self.client.get(f"/api/v1/operations/{operation_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def evidence(self, ctx=None):
        ctx = ctx or self.ctx
        with tenant_session(ctx) as session:
            return session.scalars(
                select(AuditEventRecord)
                .where(
                    AuditEventRecord.workspace_id == ctx.workspace_id,
                    AuditEventRecord.event_type == connection_checks.CHECK_EVENT,
                )
                .order_by(AuditEventRecord.created_at, AuditEventRecord.id)
            ).all()

    def checked(self, entry_id, mode="real"):
        """Check ``entry_id`` once, run the Worker and return (operation, details)."""
        accepted = self.check(entry_id, mode=mode)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.drain()
        operation = self.operation(accepted.json()["operation_id"])
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(
            operation["result"]["resource_type"], connection_checks.RESULT_RESOURCE_TYPE,
        )
        events = [e for e in self.evidence() if e.id == operation["result"]["resource_id"]]
        self.assertEqual(len(events), 1, "the result names the check's audit event")
        event = events[0]
        self.assertEqual((event.resource_type, event.resource_id), ("data_source", entry_id))
        self.assertEqual(set(event.details_json), DETAIL_KEYS)
        self.assertEqual(event.details_json["operation_id"], operation["operation_id"])
        return operation, event.details_json

    def jobs_for(self, entry_id):
        with tenant_session(self.ctx) as session:
            return session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id,
                    JobRecord.type == JOB_TYPE,
                    JobRecord.resource_id == entry_id,
                )
            ).all()


class TestConnectionCheckConclusions(ConnectionCheckFixtures):
    def test_an_unpublished_data_source_fails_with_not_published(self):
        _, details = self.checked(self.data_source(publish=False))
        self.assertEqual((details["health"], details["reason_code"]), ("failed", "data_source_not_published"))
        self.assertIsNone(details["data_source_version_id"])
        self.assertIsNone(details["execution_mode"])

    def test_a_manual_source_needs_no_connection(self):
        entry_id = self.data_source("manual", "real")
        _, details = self.checked(entry_id)
        self.assertEqual((details["health"], details["reason_code"]), ("healthy", "manual_source_no_connection"))
        self.assertEqual(details["adapter_kind"], "manual")
        self.assertEqual(details["adapter_version"], "manual-v1")
        self.assertEqual(details["execution_mode"], "real")
        self.assertEqual(details["check_version"], connection_checks.CHECK_VERSION)
        self.assertEqual(
            details["data_source_version_id"],
            service.get_entry(self.ctx, entry_id)["current_version_id"],
        )

    def test_a_mock_source_is_marked_mock_whatever_the_client_asks(self):
        _, details = self.checked(self.data_source("mock", "mock"), mode="real")
        self.assertEqual((details["health"], details["reason_code"]), ("healthy", "controlled_mock_source"))
        self.assertEqual(details["adapter_version"], "mock-v1")
        self.assertEqual(details["execution_mode"], "mock")
        self.assertEqual(details["requested_mode"], "real")

    def test_a_shopify_source_is_not_enabled_and_never_falls_back_to_the_mock(self):
        _, details = self.checked(self.data_source("shopify", "real"), mode="mock")
        self.assertEqual((details["health"], details["reason_code"]), ("failed", "adapter_not_enabled"))
        self.assertEqual(details["adapter_kind"], "shopify")
        self.assertIsNone(details["adapter_version"])
        self.assertEqual(details["execution_mode"], "real")
        self.assertEqual(details["requested_mode"], "mock")

    def test_a_revoked_bound_credential_version_is_revoked(self):
        entry_id = self.data_source("manual", "real")
        with tenant_session(self.ctx) as session:
            cv_id = _provision_credential_version(self.ctx, session)
        bind_credential(self.ctx, entry_id, cv_id)
        with tenant_session(self.ctx) as session:
            session.execute(
                update(CredentialVersionRecord)
                .where(CredentialVersionRecord.id == cv_id)
                .values(state="revoked")
            )
        _, details = self.checked(entry_id)
        self.assertEqual((details["health"], details["reason_code"]), ("revoked", "credential_not_active"))
        self.assertEqual(details["bound_version_id"], cv_id)
        self.assertEqual(details["bound_version_state"], "revoked")

    def test_an_active_bound_credential_version_changes_nothing_and_leaks_nothing(self):
        entry_id = self.data_source("mock", "mock")
        with tenant_session(self.ctx) as session:
            cv_id = _provision_credential_version(self.ctx, session)
        bind_credential(self.ctx, entry_id, cv_id)
        _, details = self.checked(entry_id)
        self.assertEqual(details["reason_code"], "controlled_mock_source")
        self.assertEqual((details["bound_version_id"], details["bound_version_state"]), (cv_id, "active"))
        for forbidden in ("encrypted_secret", "encrypted_data_key", "nonce", "encryption_key_id"):
            self.assertNotIn(forbidden, details)
        self.assertNotIn("test-encrypted", repr(details))
        self.assertNotIn("[REDACTED]", repr(details), "no field is named so it gets masked")

    def test_a_check_makes_no_network_request(self):
        entry_id = self.data_source("shopify", "real")
        accepted = self.check(entry_id)
        # Only the Worker runs in here: the test client itself is an httpx client.
        refuse = AssertionError("a connection check must not touch the network")
        with patch.object(httpx.AsyncClient, "send", side_effect=refuse), \
             patch.object(httpx.Client, "send", side_effect=refuse), \
             patch.object(socket.socket, "connect", side_effect=refuse):
            self.assertEqual(self.drain(), 1)
        self.assertEqual(self.operation(accepted.json()["operation_id"])["state"], "succeeded")

    def test_a_member_can_still_check(self):
        entry_id = self.data_source("manual", "real")
        member = self._make_context(workspace_id=self.ctx.workspace_id, role=ROLE_MEMBER)
        app.dependency_overrides[request_tenant_context] = lambda: member
        _, details = self.checked(entry_id)
        self.assertEqual(details["reason_code"], "manual_source_no_connection")


class TestConnectionCheckRead(ConnectionCheckFixtures):
    def read(self, entry_id, check_id):
        return self.client.get(f"/api/v1/library/entries/{entry_id}/connection-checks/{check_id}")

    def test_the_result_names_the_check_the_read_serves(self):
        entry_id = self.data_source("mock", "mock")
        operation, details = self.checked(entry_id, mode=None)
        self.assertIsNone(details["requested_mode"], "a client that sends no mode records none")
        response = self.read(entry_id, operation["result"]["resource_id"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {
            "check_id", "data_source_entry_id", "health", "reason_code", "adapter_kind",
            "adapter_version", "execution_mode", "data_source_version_id", "credential_state",
            "checked_at",
        })
        self.assertEqual(body["check_id"], operation["result"]["resource_id"])
        self.assertEqual(body["data_source_entry_id"], entry_id)
        self.assertEqual((body["health"], body["reason_code"]), ("healthy", "controlled_mock_source"))
        self.assertEqual((body["adapter_kind"], body["adapter_version"], body["execution_mode"]),
                         ("mock", "mock-v1", "mock"))
        self.assertEqual(body["data_source_version_id"], details["data_source_version_id"])
        self.assertIsNone(body["credential_state"])
        self.assertTrue(body["checked_at"])

    def test_the_read_reports_the_bound_credential_state(self):
        entry_id = self.data_source("manual", "real")
        with tenant_session(self.ctx) as session:
            cv_id = _provision_credential_version(self.ctx, session)
        bind_credential(self.ctx, entry_id, cv_id)
        with tenant_session(self.ctx) as session:
            session.execute(
                update(CredentialVersionRecord)
                .where(CredentialVersionRecord.id == cv_id)
                .values(state="rotated")
            )
        operation, _ = self.checked(entry_id)
        body = self.read(entry_id, operation["result"]["resource_id"]).json()
        self.assertEqual((body["health"], body["reason_code"], body["credential_state"]),
                         ("revoked", "credential_not_active", "rotated"))

    def test_a_member_can_read(self):
        entry_id = self.data_source()
        operation, _ = self.checked(entry_id)
        member = self._make_context(workspace_id=self.ctx.workspace_id, role=ROLE_MEMBER)
        app.dependency_overrides[request_tenant_context] = lambda: member
        self.assertEqual(self.read(entry_id, operation["result"]["resource_id"]).status_code, 200)

    def assert_not_found(self, response):
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["code"], "connection_check_not_found")
        self.assertEqual(response.headers["content-type"], "application/problem+json")

    def test_an_unknown_id_is_not_found(self):
        self.assert_not_found(self.read(self.data_source(), "audit_ci_only_missing"))

    def test_another_data_sources_check_is_not_found(self):
        entry_id = self.data_source()
        operation, _ = self.checked(entry_id)
        self.assert_not_found(self.read(self.data_source(), operation["result"]["resource_id"]))

    def test_another_workspaces_check_is_not_found_like_an_unknown_id(self):
        entry_id = self.data_source()
        operation, _ = self.checked(entry_id)
        other = self._make_context(role=ROLE_OWNER)
        app.dependency_overrides[request_tenant_context] = lambda: other
        self.assert_not_found(self.read(entry_id, operation["result"]["resource_id"]))

    def test_another_audit_event_is_not_found(self):
        entry_id = self.data_source()
        self.assertEqual(self.check(entry_id).status_code, 202)  # appends job.enqueued
        with tenant_session(self.ctx) as session:
            other_event = session.scalar(
                select(AuditEventRecord.id).where(
                    AuditEventRecord.workspace_id == self.ctx.workspace_id,
                    AuditEventRecord.event_type != connection_checks.CHECK_EVENT,
                )
            )
        self.assertIsNotNone(other_event)
        self.assert_not_found(self.read(entry_id, other_event))


class TestConnectionCheckEvaluate(unittest.TestCase):
    """The conclusion order, on content no publish would let through."""

    def outcome(self, content, *, bound=(None, None)):
        return connection_checks.evaluate(
            version_id="ver_1", content=content,
            bound_version_id=bound[0], bound_version_state=bound[1],
        )

    def test_invalid_content_is_config_invalid(self):
        for content in ("not-an-object", {"adapter_kind": "manual"},
                        {"adapter_kind": "ftp", "execution_mode": "real"},
                        {"adapter_kind": "manual", "execution_mode": "anything-goes"},
                        {"adapter_kind": "manual", "execution_mode": "real", "config": []}):
            with self.subTest(content=content):
                outcome = self.outcome(content)
                self.assertEqual((outcome.health, outcome.reason_code), ("failed", "data_source_config_invalid"))
                self.assertIsNone(outcome.execution_mode)

    def test_an_unimplemented_adapter_wins_over_a_revoked_credential(self):
        outcome = self.outcome(
            {"adapter_kind": "shopify", "execution_mode": "real"}, bound=("cv_1", "revoked"),
        )
        self.assertEqual(outcome.reason_code, "adapter_not_enabled")

    def test_a_rotated_or_missing_bound_version_is_not_active(self):
        for state in ("rotated", "missing"):
            with self.subTest(state=state):
                outcome = self.outcome(
                    {"adapter_kind": "manual", "execution_mode": "real"}, bound=("cv_1", state),
                )
                self.assertEqual((outcome.health, outcome.reason_code), ("revoked", "credential_not_active"))

    def test_every_reason_maps_to_a_contract_health(self):
        self.assertTrue(
            set(connection_checks.REASON_HEALTH.values())
            <= {"unknown", "healthy", "degraded", "failed", "revoked"}
        )


class TestConnectionCheckReplayAndRecovery(ConnectionCheckFixtures):
    def test_a_replayed_key_returns_the_same_operation_and_one_job(self):
        entry_id = self.data_source()
        key = "ci-only-conncheck-replay"
        first = self.check(entry_id, key)
        second = self.check(entry_id, key, mode="mock")
        self.assertEqual((first.status_code, second.status_code), (202, 202), second.text)
        self.assertEqual(first.json()["operation_id"], second.json()["operation_id"])
        self.assertEqual(len(self.jobs_for(entry_id)), 1)
        self.drain()
        third = self.check(entry_id, key)
        self.assertEqual(third.status_code, 202, third.text)
        self.assertEqual(third.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(third.json()["state"], "succeeded", "a replay reports where the check is")
        self.assertEqual(len(self.evidence()), 1)

    def test_a_key_reused_for_another_data_source_is_a_conflict(self):
        key = "ci-only-conncheck-reused"
        self.assertEqual(self.check(self.data_source(), key).status_code, 202)
        other = self.data_source()
        response = self.check(other, key)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(self.jobs_for(other), [])

    def test_the_job_is_safe_to_retry_after_a_crash(self):
        entry_id = self.data_source()
        accepted = self.check(entry_id)
        [job] = self.jobs_for(entry_id)
        self.assertTrue(job.safe_to_retry_after_crash)
        # A Worker took the Job and died before settling anything.
        quarantine_foreign_jobs(self.ctx.workspace_id)
        claimed = jobs.claim_next("crashed-worker", job_types=(JOB_TYPE,))
        self.assertEqual(claimed.id, job.id)
        jobs.mark_running(claimed.context, claimed.id, "crashed-worker")
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == job.id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
        jobs.recover_expired_jobs()
        [job] = self.jobs_for(entry_id)
        self.assertEqual(job.state, "queued", "a local check is requeued, not left uncertain")
        self.drain()
        self.assertEqual(self.operation(accepted.json()["operation_id"])["state"], "succeeded")
        self.assertEqual(len(self.evidence()), 1)

    def test_a_rerun_after_the_operation_settled_records_nothing_twice(self):
        entry_id = self.data_source("mock", "mock")
        accepted = self.check(entry_id)
        self.drain()
        before = self.operation(accepted.json()["operation_id"])
        # The Worker settled the Operation, then crashed before the Job; recovery requeued it.
        with platform_session() as session:
            session.execute(
                update(JobRecord)
                .where(JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == JOB_TYPE)
                .values(state="queued", finished_at=None)
            )
        self.assertEqual(self.drain(), 1)
        [job] = self.jobs_for(entry_id)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(self.operation(accepted.json()["operation_id"]), before)
        self.assertEqual(len(self.evidence()), 1)


class TestConnectionCheckTenancy(ConnectionCheckFixtures):
    def test_a_job_cannot_check_another_workspaces_data_source(self):
        other = self._make_context(role=ROLE_OWNER)
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        foreign_entry = self.data_source("mock", "mock", ctx=other)
        with tenant_session(other) as session:
            cv_id = _provision_credential_version(other, session)
        bind_credential(other, foreign_entry, cv_id)
        # Only a forged payload could name it: the route reads the entry first.
        self.assertEqual(self.check(foreign_entry).status_code, 404)
        with tenant_session(self.ctx) as session:
            operation = create_operation(session, command_type="connection_check")
            jobs.enqueue_in_session(
                session, context=self.ctx, job_type=JOB_TYPE,
                payload={"data_source_entry_id": foreign_entry, "operation_id": operation.id,
                         "execution_mode": "real"},
                idempotency_key="connection_check:ci-only-forged",
                resource_type="data_source", resource_id=foreign_entry,
                safe_to_retry_after_crash=True,
            )
            operation_id = operation.id
        self.drain()
        with tenant_session(self.ctx) as session:
            self.assertEqual(get_operation(session, operation_id).state, "failed")
        [job] = self.jobs_for(foreign_entry)
        self.assertEqual((job.state, job.error_code), ("failed", "connection_check_failed"))
        self.assertEqual(self.evidence(), [])
        self.assertEqual(self.evidence(other), [])


if __name__ == "__main__":
    unittest.main()
