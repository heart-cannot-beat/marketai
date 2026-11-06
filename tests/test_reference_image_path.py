"""Issue #158 -- the product reference-image path, end to end.

Covers the four production breaks the issue names:

1. The content-safety execution mode is decided by the server: a client
   ``execution_mode: "real"`` is ignored, the Job payload never carries it,
   and a deployment that selects ``real`` without an adapter fails closed
   with the stable ``content_safety_unavailable`` code (object stays
   quarantined, not a 500).
2. The Worker gate reads the committed original through ``BlobStore`` and
   writes the controlled derivative back through it -- exercised against the
   local filesystem adapter and the fake Railway bucket adapter; rejected
   originals are purged through the adapter; every check lands in the audit.
3. The derivative download intent issues a signed direct link from the
   adapter (``issue_download``) and records a ``DownloadIntentRecord``.
4. ``POST /library/reference-objects/{id}/promote`` publishes the
   ``reference_asset`` entry from the released object (rights carried from the
   upload declaration) and optionally binds it to a product -- idempotent,
   Workspace-scoped, Owner-only publish types untouched.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import tempfile
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library_fixtures import FUTURE, LibraryFixtures, jpeg_with_exif, png_bytes
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_blob_store_railway_bucket import _FakeRailwayClient
from tests.test_library_reference_commands import _provision_finalized_intent

from app import config
from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    DownloadIntentRecord,
    JobRecord,
    LibraryAuditRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    OperationRecord,
    ReferenceObjectRecord,
)
from app.library import references, service
from app.library import v1_router as library_v1_router
from app.library.contracts import (
    ENTRY_OFFER,
    ENTRY_PRODUCT,
    ENTRY_PROMPT_POLICY,
    ENTRY_REFERENCE_ASSET,
    OWNER_ONLY_PUBLISH,
    ROLE_MEMBER,
)
from app.library.reference_commands import (
    create_reference_from_upload_intent,
    get_reference_object,
)
from app.library.reference_gate_worker import run_reference_gate
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.worker_runtime import WorkerRuntime


RIGHTS = {"scope": "owned_product_photo", "valid_to": FUTURE}


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _committed_key(workspace_id: str, ext: str = "png") -> str:
    return f"committed/{workspace_id}/reference_asset/2026/10/10/{uuid.uuid4().hex}.{ext}"


def _drain_gate_jobs(ctx, store) -> WorkerRuntime:
    """Run a Worker over this Workspace's queued Jobs using ``store``."""
    quarantine_foreign_jobs(ctx.workspace_id)
    runtime = WorkerRuntime(owner=f"ref-gate-{uuid.uuid4().hex[:6]}")
    runtime.blob_store = store
    while asyncio.run(runtime.run_once()):
        pass
    return runtime


def _gate_job(ctx) -> JobRecord:
    with tenant_session(ctx) as session:
        return session.scalars(
            select(JobRecord).where(
                JobRecord.workspace_id == ctx.workspace_id,
                JobRecord.type == "library.reference_gate",
            ).order_by(JobRecord.created_at.desc())
        ).first()


def _object(ctx, object_id: str) -> ReferenceObjectRecord:
    with tenant_session(ctx) as session:
        record = session.get(ReferenceObjectRecord, object_id)
        session.expunge(record)
        return record


def _operation(ctx, operation_id: str) -> OperationRecord:
    with tenant_session(ctx) as session:
        op = session.get(OperationRecord, operation_id)
        session.expunge(op)
        return op


def _audits(ctx, command: str) -> list[LibraryAuditRecord]:
    with tenant_session(ctx) as session:
        rows = session.scalars(
            select(LibraryAuditRecord).where(
                LibraryAuditRecord.workspace_id == ctx.workspace_id,
                LibraryAuditRecord.command == command,
            ).order_by(LibraryAuditRecord.created_at)
        ).all()
        for row in rows:
            session.expunge(row)
        return rows


class _RailwayStoreMixin:
    def _railway_store(self) -> tuple[_FakeRailwayClient, RailwayBucketBlobStore]:
        client = _FakeRailwayClient()
        store = RailwayBucketBlobStore(
            bucket="marketai-test", endpoint_url="https://storage.invalid", client=client,
        )
        return client, store

    def _stage_in_bucket(self, ctx, client, raw: bytes, *, content_type="image/png", ext="png") -> tuple[str, str]:
        key = _committed_key(ctx.workspace_id, ext)
        client.put_test_object(key, raw, content_type=content_type)
        intent_id, _ = _provision_finalized_intent(
            ctx, committed_key=key, committed_version=f"sha256:{hashlib.sha256(raw).hexdigest()}:{len(raw)}",
            raw=raw, write_file=False, content_type=content_type,
            filename=f"charger.{ext}",
        )
        return intent_id, key


# ---------------------------------------------------------------------------
# 1. Server-decided content safety mode
# ---------------------------------------------------------------------------


class TestServerDecidedContentSafety(_RailwayStoreMixin, LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role=ROLE_MEMBER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_client_execution_mode_is_ignored_and_never_reaches_the_job(self):
        bucket, store = self._railway_store()
        intent_id, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes())
        resp = self.client.post(
            "/api/v1/library/reference-objects",
            json={
                "upload_intent_id": intent_id,
                "filename": "charger.png",
                "declared_mime": "image/png",
                # The previous Web release sent these; they must change nothing.
                "execution_mode": "real",
                "safety_scenario": "flagged",
                "rights": RIGHTS,
            },
            headers={"Idempotency-Key": f"ik-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        job = _gate_job(self.ctx)
        self.assertNotIn("execution_mode", job.payload_json)
        self.assertNotIn("safety_scenario", job.payload_json)

        _drain_gate_jobs(self.ctx, store)
        operation = _operation(self.ctx, resp.json()["operation_id"])
        self.assertEqual(operation.state, "succeeded")
        record = _object(self.ctx, operation.result_resource_id)
        self.assertEqual(record.state, references.STATE_RELEASED)
        # Deployment default (mock/clean) decided the outcome, not the client.
        self.assertEqual(record.execution_mode, "mock")
        self.assertEqual(record.checks_json["safety_scenario"], "clean")

    def test_real_mode_without_adapter_fails_closed_with_stable_code(self):
        bucket, store = self._railway_store()
        intent_id, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes())
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        with patch.object(config, "LIBRARY_CONTENT_SAFETY_MODE", "real"):
            _drain_gate_jobs(self.ctx, store)

        operation = _operation(self.ctx, accepted["operation_id"])
        self.assertEqual(operation.state, "failed")
        job = _gate_job(self.ctx)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, references.FAILURE_CONTENT_SAFETY_UNAVAILABLE)
        # The browser can resolve the object from the failed Operation and
        # read a stable code instead of a bare "operation failed".
        self.assertEqual(operation.result_resource_type, "reference_object")
        detail = get_reference_object(self.ctx, operation.result_resource_id)
        self.assertEqual(detail["state"], references.STATE_QUARANTINED)
        self.assertEqual(detail["failure_code"], references.FAILURE_CONTENT_SAFETY_UNAVAILABLE)
        self.assertTrue(detail["content_safety_unavailable"])
        self.assertIn("内容安全服务未配置", detail["failure_detail"])
        # The checks that did run are evidence, and the HTTP read exposes them.
        self.assertIn("decode", {check["name"] for check in detail["checks"]})
        resp = self.client.get(f"/api/v1/library/reference-objects/{operation.result_resource_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["failure_code"], references.FAILURE_CONTENT_SAFETY_UNAVAILABLE)
        # Nothing was committed and the original was not purged: the object
        # can be re-gated once the deployment wires an adapter.
        self.assertFalse(any(k.startswith(f"committed/{self.ctx.workspace_id}/reference_derivative") for k in bucket.objects))
        self.assertEqual(bucket.deleted, [])
        audit = _audits(self.ctx, "reference_gate")[-1]
        self.assertEqual(audit.result, "unavailable")
        self.assertEqual(audit.detail_json["failure_code"], references.FAILURE_CONTENT_SAFETY_UNAVAILABLE)

        # Once the adapter situation is fixed, re-uploading the same bytes
        # re-gates the same identity instead of tripping the uniqueness fence.
        intent_id_2, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes())
        again = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id_2, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)
        self.assertEqual(_operation(self.ctx, again["operation_id"]).state, "succeeded")
        self.assertEqual(_object(self.ctx, operation.result_resource_id).state, references.STATE_RELEASED)

    def test_sync_upload_path_reports_content_safety_unavailable_without_rejecting(self):
        with patch.object(config, "LIBRARY_CONTENT_SAFETY_MODE", "real"):
            with self.assertRaises(references.ContentSafetyUnavailable) as caught:
                service.upload_reference_object(
                    self.ctx, raw=png_bytes(), declared_mime="image/png", filename="x.png",
                )
        self.assertEqual(caught.exception.failure_code, references.FAILURE_CONTENT_SAFETY_UNAVAILABLE)
        with tenant_session(self.ctx) as session:
            row = session.scalars(
                select(ReferenceObjectRecord).where(ReferenceObjectRecord.workspace_id == self.ctx.workspace_id)
            ).one()
            self.assertEqual(row.state, references.STATE_QUARANTINED)
            self.assertEqual(references.gate_failure(row)[0], references.FAILURE_CONTENT_SAFETY_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 2. Gate through the BlobStore (local adapter + fake Railway bucket)
# ---------------------------------------------------------------------------


class TestGateThroughBlobStore(_RailwayStoreMixin, LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role=ROLE_MEMBER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def test_local_adapter_reads_committed_original_and_writes_derivative(self):
        root = Path(tempfile.mkdtemp(prefix="refgate-local-"))
        store = LocalFileBlobStore(
            root=root, public_base_url="http://localhost:9000", signing_secret=b"test-secret",
        )
        # Unique bytes: the sync-upload fixtures elsewhere in the suite write
        # the default jpeg_with_exif() into LIBRARY_OBJECT_DIR under the
        # same content hash, which would mask the "no local copy" assertion.
        raw = jpeg_with_exif(size=(121, 91))
        key = _committed_key(self.ctx.workspace_id, "jpg")
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(raw)
        intent_id, _ = _provision_finalized_intent(
            self.ctx, committed_key=key, committed_version="1:2:3:4", raw=raw,
            write_file=False, content_type="image/jpeg", filename="charger.jpg",
        )
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)

        operation = _operation(self.ctx, accepted["operation_id"])
        self.assertEqual(operation.state, "succeeded")
        record = _object(self.ctx, operation.result_resource_id)
        self.assertEqual(record.state, references.STATE_RELEASED)
        self.assertEqual(record.original_object_key, key)  # untouched, no local copy
        self.assertEqual(record.byte_size, len(raw))
        self.assertTrue(record.had_exif)
        self.assertTrue(record.derivative_object_key.startswith(
            f"committed/{self.ctx.workspace_id}/reference_derivative/"
        ))
        self.assertTrue(record.derivative_storage_version_id)
        derivative_path = root / record.derivative_object_key
        self.assertTrue(derivative_path.exists())
        self.assertEqual(hashlib.sha256(derivative_path.read_bytes()).hexdigest(), record.derivative_sha256)
        from PIL import Image

        with Image.open(derivative_path) as derived:
            self.assertEqual(derived.format, "JPEG")
            self.assertFalse(dict(derived.getexif()))
        # The Library object directory never receives a copy of the original.
        self.assertEqual(list(config.LIBRARY_OBJECT_DIR.rglob(f"*{record.content_sha256}*")), [])

    def test_railway_bucket_gate_releases_and_audits_every_check(self):
        bucket, store = self._railway_store()
        raw = png_bytes(size=(1600, 1200))
        intent_id, key = self._stage_in_bucket(self.ctx, bucket, raw)
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, filename="charger-front.png",
            rights=RIGHTS, idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)

        operation = _operation(self.ctx, accepted["operation_id"])
        self.assertEqual(operation.state, "succeeded")
        record = _object(self.ctx, operation.result_resource_id)
        self.assertEqual(record.state, references.STATE_RELEASED)
        self.assertEqual((record.width, record.height), (1600, 1200))
        self.assertEqual(max(record.derivative_width, record.derivative_height), config.LIBRARY_DERIVATIVE_MAX_SIDE)
        stored = bucket.objects[record.derivative_object_key]
        self.assertEqual(hashlib.sha256(stored).hexdigest(), record.derivative_sha256)
        self.assertEqual(bucket.content_types[record.derivative_object_key], "image/jpeg")
        self.assertEqual(record.derivative_storage_version_id, f"sha256:{hashlib.sha256(_b64(stored).encode('ascii')).hexdigest()}:{len(stored)}")
        self.assertIn(key, bucket.objects)  # originals stay private, never purged on release

        audit = _audits(self.ctx, "reference_gate")[-1]
        self.assertEqual(audit.result, "ok")
        self.assertEqual(audit.initiator_id, self.ctx.initiator_id)
        self.assertEqual(
            {check["name"] for check in audit.detail_json["checks"]},
            {"byte_size", "object_integrity", "malware_scan", "decode",
             "declared_mime_matches_decoded", "pixel_limits", "exif_privacy", "content_safety"},
        )
        self.assertTrue(all(check["passed"] for check in audit.detail_json["checks"]))
        self.assertEqual(audit.detail_json["derivative_sha256"], record.derivative_sha256)

    def test_railway_bucket_rejection_purges_the_original_through_the_adapter(self):
        bucket, store = self._railway_store()
        raw = references.EICAR_SIGNATURE + b"\x00" * 64
        intent_id, key = self._stage_in_bucket(self.ctx, bucket, raw)
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)

        operation = _operation(self.ctx, accepted["operation_id"])
        self.assertEqual(operation.state, "failed")
        job = _gate_job(self.ctx)
        self.assertEqual((job.state, job.error_code), ("failed", references.FAILURE_GATE_REJECTED))
        record = _object(self.ctx, operation.result_resource_id)
        self.assertEqual(record.state, references.STATE_REJECTED)
        self.assertTrue(record.original_purged)
        self.assertEqual(record.original_object_key, "")
        self.assertIn(key, bucket.deleted)
        self.assertNotIn(key, bucket.objects)
        self.assertEqual(references.gate_failure(record)[0], references.FAILURE_GATE_REJECTED)
        audit = _audits(self.ctx, "reference_gate")[-1]
        self.assertEqual(audit.result, "blocked")
        self.assertIn("malware_scan", audit.detail_json["failed_checks"])
        self.assertTrue(audit.detail_json["original_purged"])
        detail = get_reference_object(self.ctx, record.id)
        self.assertEqual(detail["failure_code"], references.FAILURE_GATE_REJECTED)
        self.assertIn("malware_scan", detail["failure_detail"])

    def test_missing_original_fails_the_gate_with_source_code(self):
        bucket, store = self._railway_store()
        raw = png_bytes()
        intent_id, key = self._stage_in_bucket(self.ctx, bucket, raw)
        del bucket.objects[key]  # vanished between finalize and gate
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)
        self.assertEqual(_operation(self.ctx, accepted["operation_id"]).state, "failed")
        job = _gate_job(self.ctx)
        self.assertEqual((job.state, job.error_code), ("failed", references.FAILURE_SOURCE_UNAVAILABLE))

    def test_transient_derivative_commit_failure_retries_and_then_releases(self):
        bucket, store = self._railway_store()
        intent_id, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes(colour=(9, 99, 199)))
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        object_id = _gate_job(self.ctx).payload_json["object_id"]

        async def failing_commit(*_args, **_kwargs):
            from app.platform.blob_store import BlobStoreError

            raise BlobStoreError("Railway Bucket commit generated artifact failed (503)")

        with patch.object(store, "commit_generated_artifact", failing_commit):
            outcome = asyncio.run(run_reference_gate(self.ctx, store, object_id))
        self.assertEqual(outcome.kind, "storage_unavailable")
        self.assertTrue(outcome.retryable)
        # Nothing was decided: the object is still quarantined and re-gateable.
        self.assertEqual(_object(self.ctx, object_id).state, references.STATE_QUARANTINED)

        # The Worker fence keeps the Operation running across the retry budget
        # and the next attempt, with storage back, releases the object.
        _drain_gate_jobs(self.ctx, store)
        self.assertEqual(_operation(self.ctx, accepted["operation_id"]).state, "succeeded")
        self.assertEqual(_object(self.ctx, object_id).state, references.STATE_RELEASED)

    def test_gate_worker_is_idempotent_for_released_objects(self):
        bucket, store = self._railway_store()
        intent_id, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes())
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)
        object_id = _operation(self.ctx, accepted["operation_id"]).result_resource_id
        before = _object(self.ctx, object_id)
        outcome = asyncio.run(run_reference_gate(self.ctx, store, object_id))
        self.assertEqual(outcome.kind, "released")
        after = _object(self.ctx, object_id)
        self.assertEqual(after.derivative_object_key, before.derivative_object_key)
        self.assertEqual(after.derivative_storage_version_id, before.derivative_storage_version_id)


# ---------------------------------------------------------------------------
# 3. Signed derivative download
# ---------------------------------------------------------------------------


class TestDerivativeDownload(_RailwayStoreMixin, LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role=ROLE_MEMBER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _released_and_promoted(self, bucket, store) -> tuple[str, dict]:
        intent_id, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes(size=(300, 200)))
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, filename="charger.png", rights=RIGHTS,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(self.ctx, store)
        object_id = _operation(self.ctx, accepted["operation_id"]).result_resource_id
        promoted = service.promote_reference_object(
            self.ctx, object_id, idempotency_key=f"ik-{uuid.uuid4().hex}",
            request_fingerprint="fp-promote",
        )
        return object_id, promoted

    def test_download_intent_issues_adapter_signed_link_for_bucket_derivative(self):
        bucket, store = self._railway_store()
        object_id, promoted = self._released_and_promoted(bucket, store)
        with patch.object(library_v1_router, "_blob_store", store):
            key = f"ik-dl-{uuid.uuid4().hex}"
            resp = self.client.post(
                f"/api/v1/library/versions/{promoted['version_id']}/download-intent",
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(resp.status_code, 201, resp.text)
            body = resp.json()
            record = _object(self.ctx, object_id)
            self.assertTrue(body["download_url"].startswith("https://bucket.storage.invalid/"))
            self.assertIn(record.derivative_object_key, body["download_url"])
            self.assertEqual(body["derivative_mime"], "image/jpeg")
            self.assertEqual(body["object_id"], object_id)
            self.assertIn("charger.jpg", body["content_disposition"])
            presign = bucket.presigned_params[-1]
            self.assertEqual(presign["operation"], "get_object")
            self.assertEqual(presign["Params"]["Key"], record.derivative_object_key)
            # The response never leaks the storage object key itself.
            self.assertNotIn("derivative_object_key", body)

            # Replay with the same key reuses the one audit DownloadIntentRecord.
            again = self.client.post(
                f"/api/v1/library/versions/{promoted['version_id']}/download-intent",
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(again.status_code, 201, again.text)
        with tenant_session(self.ctx) as session:
            intents = session.scalars(
                select(DownloadIntentRecord).where(
                    DownloadIntentRecord.workspace_id == self.ctx.workspace_id,
                    DownloadIntentRecord.owner_id == promoted["version_id"],
                )
            ).all()
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0].owner_kind, "reference_asset_version")
            self.assertEqual(intents[0].committed_blob_ref_key, record.derivative_object_key)

    def test_sync_path_derivative_is_materialized_into_the_blob_store_once(self):
        _entry_id, version = self._reference_version(self.ctx)
        object_id = version["content"]["object_id"]
        local_key = _object(self.ctx, object_id).derivative_object_key
        self.assertFalse(references.is_blob_object_key(local_key))
        bucket, store = self._railway_store()
        with patch.object(library_v1_router, "_blob_store", store):
            for _ in range(2):
                resp = self.client.post(
                    f"/api/v1/library/versions/{version['id']}/download-intent",
                    headers={"Idempotency-Key": f"ik-dl-{uuid.uuid4().hex}"},
                )
                self.assertEqual(resp.status_code, 201, resp.text)
        record = _object(self.ctx, object_id)
        self.assertTrue(references.is_blob_object_key(record.derivative_object_key))
        self.assertEqual(hashlib.sha256(bucket.objects[record.derivative_object_key]).hexdigest(), record.derivative_sha256)
        self.assertEqual(hashlib.sha256(Path(local_key).read_bytes()).hexdigest(), record.derivative_sha256)

    def test_cross_workspace_version_download_is_not_found(self):
        other = self._make_context()
        _entry_id, version = self._reference_version(other)
        resp = self.client.post(
            f"/api/v1/library/versions/{version['id']}/download-intent",
            headers={"Idempotency-Key": f"ik-dl-{uuid.uuid4().hex}"},
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# 4. Promote: publish + optional bind, idempotent, Workspace-scoped
# ---------------------------------------------------------------------------


class TestPromoteReferenceObject(_RailwayStoreMixin, LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context(role=ROLE_MEMBER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _released_object(self, ctx=None, *, rights=RIGHTS, raw=None, filename="charger-front.png") -> str:
        ctx = ctx or self.ctx
        bucket, store = self._railway_store()
        intent_id, _ = self._stage_in_bucket(ctx, bucket, raw if raw is not None else png_bytes())
        accepted = create_reference_from_upload_intent(
            ctx, upload_intent_id=intent_id, filename=filename, rights=rights,
            idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        _drain_gate_jobs(ctx, store)
        operation = _operation(ctx, accepted["operation_id"])
        assert operation.state == "succeeded", operation.state
        return operation.result_resource_id

    def _product(self, ctx=None) -> dict:
        ctx = ctx or self.ctx
        return service.create_entry(
            ctx, entry_type=ENTRY_PRODUCT, display_name="Alpha 充电器",
            content={"name": "Alpha 充电器", "description": "65W", "audience": "差旅"},
        )

    def _promote(self, object_id: str, **body):
        return self.client.post(
            f"/api/v1/library/reference-objects/{object_id}/promote",
            json=body,
            headers={
                "Idempotency-Key": f"ik-promote-{uuid.uuid4().hex}",
                "X-Command-Identity": f"command.promote.{uuid.uuid4().hex}",
            },
        )

    def test_promote_publishes_reference_asset_from_gate_evidence(self):
        object_id = self._released_object()
        resp = self._promote(object_id)
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["object_id"], object_id)
        self.assertEqual(body["display_name"], "charger-front")
        self.assertIsNone(body["binding"])
        version = service.get_version(self.ctx, body["version_id"])
        self.assertEqual(version["entry_type"], ENTRY_REFERENCE_ASSET)
        self.assertEqual(version["state"], "effective")
        content = version["content"]
        record = _object(self.ctx, object_id)
        self.assertEqual(content["object_id"], object_id)
        self.assertEqual(content["file"]["content_sha256"], record.content_sha256)
        self.assertEqual(content["file"]["filename"], "charger-front.png")
        self.assertEqual(content["derivative"]["sha256"], record.derivative_sha256)
        self.assertEqual(content["gate_checks"], record.checks_json["checks"])
        self.assertEqual(content["execution_mode"], "mock")
        # Rights carried from the upload declaration; declared_by is the uploader.
        self.assertEqual(content["rights"]["state"], "granted")
        self.assertEqual(content["rights"]["scope"], RIGHTS["scope"])
        self.assertEqual(content["rights"]["valid_to"], FUTURE)
        self.assertEqual(content["rights"]["declared_by"], self.ctx.initiator_id)
        self.assertEqual(record.promoted_entry_id, body["entry_id"])
        entry = service.get_entry(self.ctx, body["entry_id"])
        self.assertEqual(entry["current_version_id"], body["version_id"])
        self.assertEqual(get_reference_object(self.ctx, object_id)["promoted_entry_id"], body["entry_id"])
        audit = _audits(self.ctx, "promote_reference")[-1]
        self.assertEqual(audit.result, "ok")
        self.assertTrue(audit.detail_json["created"])
        self.assertEqual(audit.detail_json["object_id"], object_id)
        # Provider payload resolves through the promoted version.
        payload = service.provider_reference_payload(self.ctx, body["version_id"])
        self.assertEqual(payload["object_key"], record.derivative_object_key)

    def test_promote_is_idempotent_per_object(self):
        object_id = self._released_object()
        first = self._promote(object_id, display_name="第一次").json()
        second = self._promote(object_id, display_name="第二次").json()
        self.assertEqual((first["entry_id"], first["version_id"]), (second["entry_id"], second["version_id"]))
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        with tenant_session(self.ctx) as session:
            versions = session.scalars(
                select(LibraryVersionRecord).where(
                    LibraryVersionRecord.workspace_id == self.ctx.workspace_id,
                    LibraryVersionRecord.entry_type == ENTRY_REFERENCE_ASSET,
                )
            ).all()
            self.assertEqual(len(versions), 1)
            entries = session.scalars(
                select(LibraryEntryRecord).where(
                    LibraryEntryRecord.workspace_id == self.ctx.workspace_id,
                    LibraryEntryRecord.entry_type == ENTRY_REFERENCE_ASSET,
                )
            ).all()
            self.assertEqual(len(entries), 1)

    def test_same_idempotency_key_replays_the_stored_response(self):
        object_id = self._released_object()
        headers = {
            "Idempotency-Key": f"ik-promote-{uuid.uuid4().hex}",
            "X-Command-Identity": f"command.promote.{uuid.uuid4().hex}",
        }
        first = self.client.post(f"/api/v1/library/reference-objects/{object_id}/promote", json={}, headers=headers)
        second = self.client.post(f"/api/v1/library/reference-objects/{object_id}/promote", json={}, headers=headers)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertTrue(second.json()["created"])  # replayed verbatim

    def test_promote_binds_to_product_draft_with_defaults_and_stays_idempotent(self):
        product = self._product()
        object_id = self._released_object()
        resp = self._promote(object_id, bind={"entry_id": product["id"]})
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        binding = body["binding"]
        self.assertEqual(binding["entry_id"], product["id"])
        self.assertEqual(binding["purpose"], service.PROMOTE_DEFAULT_PURPOSE)
        self.assertEqual(binding["viewpoint"], service.PROMOTE_DEFAULT_VIEWPOINT)
        self.assertEqual(binding["draft_revision"], 2)
        self.assertFalse(binding["already_bound"])
        draft = service.get_entry(self.ctx, product["id"])["draft"]
        self.assertEqual(draft["revision"], 2)
        self.assertEqual(
            draft["content"]["reference_bindings"],
            [{
                "reference_version_id": body["version_id"],
                "purpose": service.PROMOTE_DEFAULT_PURPOSE,
                "viewpoint": service.PROMOTE_DEFAULT_VIEWPOINT,
                "priority": 0,
                "note": "",
                "conditions": {},
            }],
        )
        # Re-promoting with the same bind does not duplicate the binding or
        # bump the Draft again.
        again = self._promote(object_id, bind={"entry_id": product["id"]}).json()
        self.assertFalse(again["created"])
        self.assertTrue(again["binding"]["already_bound"])
        self.assertEqual(again["binding"]["draft_revision"], 2)
        self.assertEqual(service.get_entry(self.ctx, product["id"])["draft"]["revision"], 2)
        # The product Draft publishes with the server-made binding.
        published = service.publish(
            self.ctx, product["id"], 2,
        )
        bindings = service.list_bindings(self.ctx, published["id"])
        self.assertEqual([b["reference_version_id"] for b in bindings], [body["version_id"]])
        self.assertEqual(bindings[0]["gate_status"], references.STATE_RELEASED)

    def test_promote_bind_honours_explicit_purpose_viewpoint_and_revision(self):
        product = self._product()
        object_id = self._released_object()
        stale = self._promote(object_id, bind={"entry_id": product["id"], "expected_revision": 7})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "revision_conflict")
        ok = self._promote(
            object_id,
            bind={"entry_id": product["id"], "purpose": "hero", "viewpoint": "front", "expected_revision": 1},
        )
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual((ok.json()["binding"]["purpose"], ok.json()["binding"]["viewpoint"]), ("hero", "front"))

    def test_promote_rejects_bind_to_non_bindable_entry(self):
        owner = self.owner_ctx(self.ctx)
        offer = service.create_entry(owner, entry_type=ENTRY_OFFER, display_name="Offer")
        object_id = self._released_object()
        resp = self._promote(object_id, bind={"entry_id": offer["id"]})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["code"], "library_bind_target_invalid")

    def test_promote_without_any_rights_declaration_is_blocked(self):
        object_id = self._released_object(rights=None)
        resp = self._promote(object_id)
        self.assertEqual(resp.status_code, 422, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], "library_reference_rights_required")
        self.assertIn("reference_rights_required", {f["code"] for f in body["report"]["blocking"]})
        self.assertIsNone(_object(self.ctx, object_id).promoted_entry_id)
        # A declaration supplied at promote time unblocks it; declared_by is the promoter.
        resp = self._promote(object_id, rights={"scope": "licensed", "valid_to": FUTURE, "evidence": "contract #12"})
        self.assertEqual(resp.status_code, 201, resp.text)
        rights = service.get_version(self.ctx, resp.json()["version_id"])["content"]["rights"]
        self.assertEqual((rights["scope"], rights["evidence"], rights["declared_by"]), ("licensed", "contract #12", self.ctx.initiator_id))

    def test_promote_rejects_expired_rights_declaration(self):
        object_id = self._released_object(rights=None)
        past = (date.today() - timedelta(days=1)).isoformat()
        resp = self._promote(object_id, rights={"scope": "owned", "valid_to": past})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["code"], "library_rights_invalid")

    def test_promote_refuses_unreleased_object_with_gate_failure_code(self):
        bucket, store = self._railway_store()
        intent_id, _ = self._stage_in_bucket(self.ctx, bucket, png_bytes())
        create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id, rights=RIGHTS, idempotency_key=f"ik-{uuid.uuid4().hex}",
        )
        object_id = _gate_job(self.ctx).payload_json["object_id"]
        resp = self._promote(object_id)  # still quarantined: the Worker has not run
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["conflict_info"]["code"], "reference_object_not_released")
        self.assertEqual(resp.json()["conflict_info"]["state"], references.STATE_QUARANTINED)

    def test_cross_workspace_object_id_does_not_leak_existence(self):
        other = self._make_context()
        foreign_object_id = self._released_object(other)
        resp = self._promote(foreign_object_id)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "reference_object_not_found")
        unknown = self._promote("refobj_does_not_exist")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(resp.json()["detail"], unknown.json()["detail"])
        self.assertIsNone(_object(other, foreign_object_id).promoted_entry_id)
        read = self.client.get(f"/api/v1/library/reference-objects/{foreign_object_id}")
        self.assertEqual(read.status_code, 404)
        self.assertEqual(read.json()["code"], "reference_object_not_found")
        # Binding a foreign product from this Workspace is equally "not found".
        own_object = self._released_object()
        foreign_product = self._product(other)
        bound = self._promote(own_object, bind={"entry_id": foreign_product["id"]})
        self.assertEqual(bound.status_code, 404, bound.text)
        self.assertEqual(service.get_entry(other, foreign_product["id"])["draft"]["revision"], 1)

    def test_owner_and_member_may_promote(self):
        object_id = self._released_object()
        member = self._promote(object_id)
        self.assertEqual(member.status_code, 201, member.text)
        owner_ctx = self.owner_ctx(self.ctx)
        # Different bytes: identical content would reuse the first object
        # (§7.1 object reuse) and replay its promotion instead of creating.
        other_object = self._released_object(raw=png_bytes(colour=(200, 10, 10)))
        app.dependency_overrides[request_tenant_context] = lambda: owner_ctx
        owner = self._promote(other_object)
        self.assertEqual(owner.status_code, 201, owner.text)
        self.assertTrue(owner.json()["created"])

    def test_owner_only_publish_boundary_is_unchanged(self):
        self.assertNotIn(ENTRY_REFERENCE_ASSET, OWNER_ONLY_PUBLISH)
        self.assertIn(ENTRY_OFFER, OWNER_ONLY_PUBLISH)
        self.assertIn(ENTRY_PROMPT_POLICY, OWNER_ONLY_PUBLISH)
        # Promote can only ever create reference_asset entries. MFA is checked
        # only at login (ADR 0015), so its audit rows carry no step-up marker.
        object_id = self._released_object()
        promoted = self._promote(object_id)
        self.assertEqual(promoted.status_code, 201, promoted.text)
        promote_audit = _audits(self.ctx, "promote_reference")[-1]
        self.assertNotIn("step_up", promote_audit.detail_json)
        publish_audit = [
            row for row in _audits(self.ctx, "publish")
            if row.detail_json.get("object_id") == object_id
        ][-1]
        self.assertNotIn("step_up", publish_audit.detail_json)
        self.assertTrue(publish_audit.detail_json["promoted"])
        self.assertEqual(publish_audit.version_id, promoted.json()["version_id"])

    def test_openapi_exposes_the_new_routes_additively(self):
        spec = app.openapi()
        paths = spec["paths"]
        self.assertIn("/api/v1/library/reference-objects/{object_id}", paths)
        self.assertIn("/api/v1/library/reference-objects/{object_id}/promote", paths)
        self.assertEqual(
            paths["/api/v1/library/reference-objects/{object_id}/promote"]["post"]["operationId"],
            "promote_library_reference_object",
        )
        self.assertEqual(
            paths["/api/v1/library/reference-objects/{object_id}"]["get"]["operationId"],
            "get_library_reference_object",
        )
        create_schema = spec["components"]["schemas"]["CreateReferenceObjectInput"]["properties"]
        self.assertTrue(create_schema["execution_mode"].get("deprecated"))
        self.assertIn("rights", create_schema)


if __name__ == "__main__":
    unittest.main()
