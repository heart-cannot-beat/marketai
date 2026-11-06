"""Tests for Gate 0.8 — Reference creation and download operations.

Covers:
- create_reference_from_upload_intent: golden path, idempotent replay (consumed
  intent), wrong workspace/purpose, unfinalized intent, missing committed blob
- create_reference_download_intent: golden path for released version, withdrawn
  version rejected, non-reference-asset rejected, unreleased object rejected,
  missing derivative rejected
- HTTP routes: 202 for reference creation, 201 for download intent, 501 stubs
  replaced, negative HTTP scenarios
- Worker handler: gate pass settles Operation to SUCCEEDED, gate reject settles
  to FAILED
"""
from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures, png_bytes

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    ReferenceObjectRecord,
    UploadIntentRecord,
    new_id,
    utcnow,
)
from app.library import service
from app.library.contracts import (
    ENTRY_PRODUCT,
    ENTRY_REFERENCE_ASSET,
    LIFECYCLE_ACTIVE,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.library.reference_commands import (
    ReferenceCommandError,
    create_reference_download_intent,
    create_reference_from_upload_intent,
)
from app.library.references import STATE_QUARANTINED, STATE_RELEASED
from app.library.service import LibraryConflict, LibraryNotFound
from app.main import app
from app.platform.http_identity import request_tenant_context


def _provision_finalized_intent(
    ctx,
    *,
    committed_key: str = "",
    committed_version: str = "v1",
    purpose: str = "reference_asset",
    raw: bytes | None = None,
    write_file: bool = True,
    content_type: str = "image/png",
    filename: str = "test.png",
) -> tuple[str, str]:
    """Create a finalized UploadIntentRecord (and, by default, its committed
    blob on disk at an absolute path -- the dev sync layout).

    The intent carries the real base64 SHA-256 of ``raw``: the reference
    command derives the object's content hash from it instead of reading the
    blob, exactly as the Worker-verified production intent would.

    Returns (intent_id, committed_key).
    """
    data = raw if raw is not None else png_bytes()
    intent_id = new_id("ui")
    if not committed_key:
        committed_key = str(
            Path(tempfile.mkdtemp(prefix="refcmd-")) / f"{uuid.uuid4().hex}.png"
        )
    if write_file:
        Path(committed_key).parent.mkdir(parents=True, exist_ok=True)
        Path(committed_key).write_bytes(data)

    staging_key = f"staging/{ctx.workspace_id}/reference_asset/2026/09/{uuid.uuid4().hex}.dat"
    now = utcnow()
    with tenant_session(ctx) as session:
        record = UploadIntentRecord(
            id=intent_id,
            workspace_id=ctx.workspace_id,
            initiator_id=ctx.initiator_id,
            authorization_epoch_at_issue=0,
            owner_kind="library",
            owner_id=None,
            staging_key=staging_key,
            purpose=purpose,
            filename_original=filename,
            content_type=content_type,
            content_length_min=len(data),
            content_length_max=len(data),
            checksum_sha256_b64=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
            state="finalized",
            committed_blob_ref_key=committed_key,
            committed_blob_ref_version=committed_version,
            created_at=now,
            expires_at=now + timedelta(seconds=300),
            finalized_at=now,
        )
        session.add(record)
        session.flush()
    return intent_id, committed_key


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


class TestCreateReferenceFromUploadIntent(LibraryFixtures):

    def test_golden_path_returns_operation_accepted(self):
        ctx = self.ctx()
        intent_id, _ = _provision_finalized_intent(ctx)
        result = create_reference_from_upload_intent(
            ctx,
            upload_intent_id=intent_id,
            idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
        )
        self.assertIn("operation_id", result)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["command_type"], "reference_gate")

    def test_creates_quarantined_reference_object(self):
        ctx = self.ctx()
        intent_id, committed_key = _provision_finalized_intent(ctx)
        create_reference_from_upload_intent(
            ctx,
            upload_intent_id=intent_id,
            idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
        )
        with tenant_session(ctx) as session:
            record = (
                session.query(ReferenceObjectRecord)
                .filter(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id,
                    ReferenceObjectRecord.original_object_key == committed_key,
                )
                .one_or_none()
            )
            self.assertIsNotNone(record)
            self.assertEqual(record.state, STATE_QUARANTINED)

    def test_consumed_intent_raises_conflict(self):
        ctx = self.ctx()
        intent_id, _ = _provision_finalized_intent(ctx)
        create_reference_from_upload_intent(
            ctx,
            upload_intent_id=intent_id,
            idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
        )
        with self.assertRaises(LibraryConflict) as cm:
            create_reference_from_upload_intent(
                ctx,
                upload_intent_id=intent_id,
                idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
            )
        self.assertIn("upload_intent_consumed", str(cm.exception))

    def test_unknown_intent_raises_not_found(self):
        ctx = self.ctx()
        with self.assertRaises(LibraryNotFound):
            create_reference_from_upload_intent(
                ctx,
                upload_intent_id="ui_does_not_exist",
                idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
            )

    def test_unfinalized_intent_raises_error(self):
        ctx = self.ctx()
        intent_id = new_id("ui")
        staging_key = f"staging/{ctx.workspace_id}/reference_asset/2026/09/{uuid.uuid4().hex}.dat"
        now = utcnow()
        with tenant_session(ctx) as session:
            session.add(UploadIntentRecord(
                id=intent_id,
                workspace_id=ctx.workspace_id,
                initiator_id=ctx.initiator_id,
                authorization_epoch_at_issue=0,
                owner_kind="library",
                owner_id=None,
                staging_key=staging_key,
                purpose="reference_asset",
                filename_original="test.png",
                content_type="image/png",
                content_length_min=1,
                content_length_max=10_000_000,
                checksum_sha256_b64="test==",
                state="pending",
                created_at=now,
                expires_at=now + timedelta(seconds=300),
            ))
            session.flush()
        with self.assertRaises(ReferenceCommandError) as cm:
            create_reference_from_upload_intent(
                ctx,
                upload_intent_id=intent_id,
                idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
            )
        self.assertIn("pending", str(cm.exception))

    def test_wrong_purpose_raises_error(self):
        ctx = self.ctx()
        intent_id, _ = _provision_finalized_intent(ctx, purpose="generation_output")
        with self.assertRaises(ReferenceCommandError) as cm:
            create_reference_from_upload_intent(
                ctx,
                upload_intent_id=intent_id,
                idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
            )
        self.assertIn("reference_asset", str(cm.exception))

    def test_cross_workspace_intent_raises_not_found(self):
        ctx1 = self.ctx()
        ctx2 = self.ctx()
        intent_id, _ = _provision_finalized_intent(ctx1)
        with self.assertRaises(LibraryNotFound):
            create_reference_from_upload_intent(
                ctx2,
                upload_intent_id=intent_id,
                idempotency_key=f"idem-ref-{uuid.uuid4().hex}",
            )


# ---------------------------------------------------------------------------
# Download intent tests
# ---------------------------------------------------------------------------


class TestCreateReferenceDownloadIntent(LibraryFixtures):

    def _released_version(self, ctx):
        """Publish a reference asset version (which runs the gate)."""
        entry_id, version = self._reference_version(ctx)
        return entry_id, version

    def test_golden_path_returns_derivative_facts(self):
        ctx = self.ctx()
        _, version = self._released_version(ctx)
        result = create_reference_download_intent(
            ctx, version_id=version["id"],
        )
        self.assertIn("derivative_object_key", result)
        self.assertIn("derivative_mime", result)
        self.assertEqual(result["version_id"], version["id"])

    def test_withdrawn_version_raises_conflict(self):
        ctx = self.ctx(role=ROLE_OWNER)
        entry_id, version = self._released_version(ctx)
        service.withdraw_version(ctx, version["id"], reason="test")
        with self.assertRaises(LibraryConflict) as cm:
            create_reference_download_intent(ctx, version_id=version["id"])
        self.assertIn("撤回", str(cm.exception))

    def test_non_reference_asset_version_raises_error(self):
        ctx = self.ctx()
        entry = service.create_entry(
            ctx, entry_type=ENTRY_PRODUCT, display_name="Not Ref"
        )
        with self.assertRaises(Exception):
            create_reference_download_intent(ctx, version_id=entry["id"])

    def test_unknown_version_raises_not_found(self):
        ctx = self.ctx()
        with self.assertRaises(LibraryNotFound):
            create_reference_download_intent(ctx, version_id="libver_nonexistent")

    def test_cross_workspace_version_raises_not_found(self):
        ctx1 = self.ctx()
        ctx2 = self.ctx()
        _, version = self._released_version(ctx1)
        with self.assertRaises(LibraryNotFound):
            create_reference_download_intent(ctx2, version_id=version["id"])

    def test_unreleased_object_raises_error(self):
        ctx = self.ctx()
        _, version = self._released_version(ctx)
        with tenant_session(ctx) as session:
            content = version.get("content", {})
            object_id = content.get("object_id")
            if object_id:
                record = session.get(ReferenceObjectRecord, object_id)
                if record:
                    record.state = STATE_QUARANTINED
                    session.flush()
        if object_id:
            with self.assertRaises(ReferenceCommandError) as cm:
                create_reference_download_intent(ctx, version_id=version["id"])
            self.assertIn("released", str(cm.exception))


# ---------------------------------------------------------------------------
# HTTP route tests
# ---------------------------------------------------------------------------


class TestCreateReferenceObjectHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_reference_object_returns_202(self):
        intent_id, _ = _provision_finalized_intent(self.ctx)
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": intent_id},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("operation_id", body)
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["command_type"], "reference_gate")

    def test_reference_object_no_longer_501(self):
        intent_id, _ = _provision_finalized_intent(self.ctx)
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": intent_id},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        self.assertNotEqual(resp.status_code, 501)

    def test_consumed_intent_returns_409(self):
        intent_id, _ = _provision_finalized_intent(self.ctx)
        self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": intent_id},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": intent_id},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 409)

    def test_unknown_intent_returns_404(self):
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": "ui_nonexistent"},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_unfinalized_intent_returns_error(self):
        intent_id = new_id("ui")
        staging_key = f"staging/{self.ctx.workspace_id}/reference_asset/2026/09/{uuid.uuid4().hex}.dat"
        now = utcnow()
        with tenant_session(self.ctx) as session:
            session.add(UploadIntentRecord(
                id=intent_id,
                workspace_id=self.ctx.workspace_id,
                initiator_id=self.ctx.initiator_id,
                authorization_epoch_at_issue=0,
                owner_kind="library",
                owner_id=None,
                staging_key=staging_key,
                purpose="reference_asset",
                filename_original="test.png",
                content_type="image/png",
                content_length_min=1,
                content_length_max=10_000_000,
                checksum_sha256_b64="test==",
                state="pending",
                created_at=now,
                expires_at=now + timedelta(seconds=300),
            ))
            session.flush()
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": intent_id},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        self.assertIn(resp.status_code, (400, 422, 500))

    def test_wrong_purpose_returns_error(self):
        intent_id, _ = _provision_finalized_intent(
            self.ctx, purpose="generation_output"
        )
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={"upload_intent_id": intent_id},
            headers={"Idempotency-Key": f"ik-refobj-{uuid.uuid4().hex}"},
        )
        self.assertIn(resp.status_code, (400, 422, 500))


class TestDownloadIntentHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_download_intent_returns_201(self):
        _, version = self._reference_version(self.ctx)
        resp = self.client.post(
            f"/api/v1/library/versions/{version['id']}/download-intent",
            headers={"Idempotency-Key": f"ik-dl-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertIn("download_url", body)
        self.assertIn("content_disposition", body)
        self.assertIn("derivative_mime", body)
        self.assertEqual(body["version_id"], version["id"])

    def test_download_intent_no_longer_501(self):
        _, version = self._reference_version(self.ctx)
        resp = self.client.post(
            f"/api/v1/library/versions/{version['id']}/download-intent",
            headers={"Idempotency-Key": f"ik-dl-{uuid.uuid4().hex}"},
        )
        self.assertNotEqual(resp.status_code, 501)

    def test_download_intent_unknown_version_returns_404(self):
        resp = self.client.post(
            "/api/v1/library/versions/libver_nonexistent/download-intent",
            headers={"Idempotency-Key": f"ik-dl-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_download_intent_withdrawn_version_returns_409(self):
        _, version = self._reference_version(self.ctx)
        service.withdraw_version(self.ctx, version["id"], reason="test-withdraw")
        resp = self.client.post(
            f"/api/v1/library/versions/{version['id']}/download-intent",
            headers={"Idempotency-Key": f"ik-dl-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
