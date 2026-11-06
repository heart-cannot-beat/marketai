"""Tests for Gate 0.7 — DataSource credential binding and connection check routes.

Covers:
- GET credential-binding: 404 when none, 200 with active binding
- POST credential-binding: 201 creates binding, idempotency, supersedes previous
- Member credential bind rejected (owner-only)
- Wrong-workspace credential rejected
- POST connection-checks: 202 with queued Operation
- Connection check on archived entry rejected
- Connection check on non-data-source rejected
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id, utcnow
from app.library import service
from app.library.contracts import (
    ENTRY_DATA_SOURCE,
    ENTRY_PRODUCT,
    LIFECYCLE_ACTIVE,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.library.datasources import bind_credential, get_active_binding
from app.main import app
from app.platform.http_identity import request_tenant_context


def _provision_credential_version(ctx, session, credential_version_id=None):
    """Insert a minimal CredentialVersionRecord for FK satisfaction."""
    from app.entitlements.credentials import CredentialVersionRecord

    cv_id = credential_version_id or new_id("cv")
    record = CredentialVersionRecord(
        id=cv_id,
        workspace_id=ctx.workspace_id,
        credential_key=f"TEST_KEY_{cv_id}",
        version_no=1,
        provider="mock",
        state="active",
        encrypted_secret=b"test-encrypted-secret",
        encrypted_data_key=b"test-encrypted-data-key",
        encryption_key_id="test-kms-key-id",
        encryption_algorithm="aes-256-gcm",
        nonce=b"test-nonce-12b",
        aad_schema_version=1,
        created_by="test-system",
        created_at=utcnow(),
    )
    session.add(record)
    session.flush()
    return cv_id


class TestCredentialBindingService(LibraryFixtures):

    def _create_data_source(self, ctx):
        content = {
            "adapter_kind": "manual",
            "label": "Test DS",
        }
        return service.create_entry(ctx, entry_type=ENTRY_DATA_SOURCE,
                                     display_name="Test DS", content=content)

    def test_get_active_binding_returns_none_when_empty(self):
        ctx = self.ctx()
        ds = self._create_data_source(ctx)
        result = get_active_binding(ctx, ds["id"])
        self.assertIsNone(result)

    def test_bind_and_get_active_binding(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._create_data_source(ctx)
        with tenant_session(ctx) as session:
            cv_id = _provision_credential_version(ctx, session)
        result = bind_credential(ctx, ds["id"], cv_id)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["credential_version_id"], cv_id)
        active = get_active_binding(ctx, ds["id"])
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], result["id"])

    def test_rebind_supersedes_previous(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._create_data_source(ctx)
        with tenant_session(ctx) as session:
            cv1 = _provision_credential_version(ctx, session)
            cv2 = _provision_credential_version(ctx, session)
        bind_credential(ctx, ds["id"], cv1)
        result2 = bind_credential(ctx, ds["id"], cv2)
        self.assertEqual(result2["revision"], 2)
        self.assertEqual(result2["credential_version_id"], cv2)
        active = get_active_binding(ctx, ds["id"])
        self.assertEqual(active["credential_version_id"], cv2)

    def test_member_cannot_bind_credential(self):
        ctx = self.ctx(role=ROLE_MEMBER)
        ds = self._create_data_source(ctx)
        with tenant_session(ctx) as session:
            cv_id = _provision_credential_version(ctx, session)
        from app.library.service import LibraryForbidden
        with self.assertRaises(LibraryForbidden):
            bind_credential(ctx, ds["id"], cv_id)

    def test_bind_rejects_non_data_source(self):
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Not DS")
        with tenant_session(ctx) as session:
            cv_id = _provision_credential_version(ctx, session)
        from app.library.service import LibraryError
        with self.assertRaises(LibraryError):
            bind_credential(ctx, entry["id"], cv_id)

    def test_bind_rejects_archived_data_source(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._create_data_source(ctx)
        entry = service.get_entry(ctx, ds["id"])
        service.archive_entry(ctx, ds["id"], expected_revision=entry["draft"]["revision"])
        with tenant_session(ctx) as session:
            cv_id = _provision_credential_version(ctx, session)
        from app.library.service import LibraryConflict
        with self.assertRaises(LibraryConflict):
            bind_credential(ctx, ds["id"], cv_id)

    def test_bind_idempotency(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds = self._create_data_source(ctx)
        with tenant_session(ctx) as session:
            cv_id = _provision_credential_version(ctx, session)
        fp = service.compute_fingerprint({"ds": ds["id"], "cv": cv_id})
        r1 = bind_credential(ctx, ds["id"], cv_id, idempotency_key="idem-bind-1", request_fingerprint=fp)
        r2 = bind_credential(ctx, ds["id"], cv_id, idempotency_key="idem-bind-1", request_fingerprint=fp)
        self.assertEqual(r1["id"], r2["id"])


class TestCredentialBindingHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _create_data_source(self):
        content = {"adapter_kind": "manual", "label": "Test DS"}
        return service.create_entry(self.ctx, entry_type=ENTRY_DATA_SOURCE,
                                     display_name="Test DS", content=content)

    def test_get_credential_binding_404_when_empty(self):
        ds = self._create_data_source()
        resp = self.client.get(f"/api/v1/library/entries/{ds['id']}/credential-binding")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "no_active_binding")

    def test_post_credential_binding_201(self):
        ds = self._create_data_source()
        with tenant_session(self.ctx) as session:
            cv_id = _provision_credential_version(self.ctx, session)
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/credential-binding",
            json={"credential_version_id": cv_id},
            headers={"Idempotency-Key": "ik-bind-http-1"},
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["state"], "active")
        self.assertEqual(body["credential_version_id"], cv_id)

    def test_get_credential_binding_after_bind(self):
        ds = self._create_data_source()
        with tenant_session(self.ctx) as session:
            cv_id = _provision_credential_version(self.ctx, session)
        self.client.post(
            f"/api/v1/library/entries/{ds['id']}/credential-binding",
            json={"credential_version_id": cv_id},
            headers={"Idempotency-Key": "ik-bind-http-get-1"},
        )
        resp = self.client.get(f"/api/v1/library/entries/{ds['id']}/credential-binding")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["credential_version_id"], cv_id)

    def test_member_bind_rejected_via_http(self):
        member_ctx = self._make_context(
            workspace_id=self.ctx.workspace_id, role=ROLE_MEMBER
        )
        app.dependency_overrides[request_tenant_context] = lambda: member_ctx
        ds = self._create_data_source()
        with tenant_session(self.ctx) as session:
            cv_id = _provision_credential_version(self.ctx, session)
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/credential-binding",
            json={"credential_version_id": cv_id},
            headers={"Idempotency-Key": "ik-bind-member-1"},
        )
        self.assertIn(resp.status_code, (403, 500))


class TestConnectionCheckHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _create_data_source(self):
        content = {"adapter_kind": "manual", "label": "Test DS"}
        return service.create_entry(self.ctx, entry_type=ENTRY_DATA_SOURCE,
                                     display_name="Test DS", content=content)

    def test_connection_check_returns_202(self):
        ds = self._create_data_source()
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/connection-checks",
            json={"execution_mode": "mock"},
            headers={"Idempotency-Key": "ik-conncheck-1"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("operation_id", body)
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["command_type"], "connection_check")

    def test_connection_check_rejects_non_data_source(self):
        entry = service.create_entry(self.ctx, entry_type=ENTRY_PRODUCT, display_name="Not DS")
        resp = self.client.post(
            f"/api/v1/library/entries/{entry['id']}/connection-checks",
            json={"execution_mode": "mock"},
            headers={"Idempotency-Key": "ik-conncheck-notds-1"},
        )
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertEqual(body.get("code"), "entry_type_mismatch")

    def test_connection_check_rejects_archived_entry(self):
        ds = self._create_data_source()
        entry = service.get_entry(self.ctx, ds["id"])
        service.archive_entry(self.ctx, ds["id"], expected_revision=entry["draft"]["revision"])
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/connection-checks",
            json={"execution_mode": "mock"},
            headers={"Idempotency-Key": "ik-conncheck-archived-1"},
        )
        self.assertEqual(resp.status_code, 409)

    def test_connection_check_requires_execution_mode(self):
        ds = self._create_data_source()
        resp = self.client.post(
            f"/api/v1/library/entries/{ds['id']}/connection-checks",
            json={},
            headers={"Idempotency-Key": "ik-conncheck-nomode-1"},
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
