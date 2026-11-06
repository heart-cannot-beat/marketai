"""Upload/download intent endpoints and Worker verification (spec §7.5-§7.12).

Flat in ``tests/`` (not ``tests/platform/``), matching every prior task's
test module in this repo (see the note on ``tests/test_operation_polling.py``
about a nested package shadowing stdlib modules for whatever imports after
it).

Three layers are covered:

* The ``UploadIntentState`` state machine (pure function, no DB) --
  spec §7.9, exactly the brief's Step 1 tests.
* ``POST /api/v1/upload-intents``, ``.../finalize``, and
  ``POST /api/v1/download-intents`` through the real FastAPI app and
  ``TestClient``, against the shared migrated engine -- mirroring
  ``tests/test_operation_polling.py``'s pattern (real ``tenant_session``
  units of work, not a throwaway engine), because the behavior under test
  (idempotent replay, Problem Details shapes, the cross-Workspace object-key
  guard) lives in the ``app/main.py`` HTTP handlers.
* ``app.platform.upload_intents.verify_upload_intent`` (spec §7.8's Worker
  sequence) against a real ``LocalFileBlobStore`` writing to a temporary
  directory, so ``inspect_upload``/``commit_upload`` run against actual
  files, not a fake.
"""
from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.blob_store_local import LocalFileBlobStore, resolve_key_path, sidecar_path  # noqa: E402
from app.adapters.persistence.database import (  # noqa: E402
    engine,
    platform_session,
    tenant_session,
)
from app.adapters.persistence.models import JobRecord, WorkspaceRecord, utcnow  # noqa: E402
from app.main import app  # noqa: E402
from app.platform.blob_store import StagedBlobRef, derive_committed_key, validate_object_key  # noqa: E402
from app.platform.download_intents import get_download_intent  # noqa: E402
from app.platform.http_identity import request_tenant_context  # noqa: E402
from app.platform.purpose_policies import PURPOSE_POLICIES  # noqa: E402
from app.platform.tenancy import TenantContext  # noqa: E402
from app.platform.upload_intents import (  # noqa: E402
    UploadIntentLeaseLostError,
    UploadIntentNotFoundError,
    VALID_TRANSITIONS,
    build_staging_key,
    get_upload_intent,
    sniff_mime,
    validate_intent_transition,
    verify_upload_intent,
)
from app.platform.workspace_admission import WorkspaceAdmission, WorkspaceNotFoundError  # noqa: E402


# ---------------------------------------------------------------------------
# UploadIntent state machine (brief Step 1, spec §7.9) -- no DB needed.
# ---------------------------------------------------------------------------


class TestUploadIntentStateMachine(unittest.TestCase):
    def test_pending_to_verifying_valid(self):
        self.assertTrue(validate_intent_transition("pending", "verifying"))

    def test_verifying_to_finalized_valid(self):
        self.assertTrue(validate_intent_transition("verifying", "finalized"))

    def test_verifying_to_rejected_valid(self):
        self.assertTrue(validate_intent_transition("verifying", "rejected"))

    def test_verifying_to_pending_valid(self):
        self.assertTrue(validate_intent_transition("verifying", "pending"))

    def test_pending_to_expired_valid(self):
        self.assertTrue(validate_intent_transition("pending", "expired"))

    def test_finalized_to_anything_invalid(self):
        self.assertFalse(validate_intent_transition("finalized", "pending"))
        self.assertFalse(validate_intent_transition("finalized", "verifying"))

    def test_rejected_and_expired_are_terminal(self):
        for to_state in VALID_TRANSITIONS:
            self.assertFalse(validate_intent_transition("rejected", to_state))
            self.assertFalse(validate_intent_transition("expired", to_state))

    def test_pending_cannot_skip_to_finalized(self):
        self.assertFalse(validate_intent_transition("pending", "finalized"))
        self.assertFalse(validate_intent_transition("pending", "rejected"))


class TestPurposePolicies(unittest.TestCase):
    def test_every_policy_has_a_nonempty_allowlist_and_positive_limits(self):
        for name, policy in PURPOSE_POLICIES.items():
            with self.subTest(purpose=name):
                self.assertTrue(policy.allowed_mimes)
                self.assertGreater(policy.max_size_bytes, 0)
                self.assertGreater(policy.default_ttl_seconds, 0)

    def test_export_package_is_zip_only(self):
        self.assertEqual(PURPOSE_POLICIES["export_package"].allowed_mimes, frozenset({"application/zip"}))

    def test_manual_fact_import_accepts_only_json_and_csv(self):
        policy = PURPOSE_POLICIES["library_manual_fact_import"]
        self.assertEqual(policy.allowed_mimes, frozenset({"application/json", "text/csv"}))
        self.assertEqual(policy.max_size_bytes, 10 * 1024 * 1024)


class TestMimeSniff(unittest.TestCase):
    def test_png_magic_bytes(self):
        self.assertEqual(sniff_mime(b"\x89PNG\r\n\x1a\n" + b"rest"), "image/png")

    def test_jpeg_magic_bytes(self):
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0" + b"rest"), "image/jpeg")

    def test_gif_magic_bytes(self):
        self.assertEqual(sniff_mime(b"GIF89a" + b"rest"), "image/gif")

    def test_webp_magic_bytes(self):
        data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"
        self.assertEqual(sniff_mime(data), "image/webp")

    def test_zip_magic_bytes(self):
        self.assertEqual(sniff_mime(b"PK\x03\x04" + b"rest"), "application/zip")

    def test_unknown_bytes_fall_back_to_octet_stream(self):
        self.assertEqual(sniff_mime(b"not a real file"), "application/octet-stream")

    def test_json_text_is_sniffed_without_trusting_declared_mime(self):
        self.assertEqual(sniff_mime(b'  {"facts": []}'), "application/json")

    def test_utf8_bom_json_is_sniffed(self):
        self.assertEqual(sniff_mime(b'\xef\xbb\xbf{"facts": []}'), "application/json")

    def test_csv_text_is_sniffed_without_trusting_declared_mime(self):
        self.assertEqual(
            sniff_mime(b"fact_id,fact_type,country_code\nf1,price,US\n"),
            "text/csv",
        )


class TestStagingKey(unittest.TestCase):
    def test_build_staging_key_is_well_formed_and_validates(self):
        key = build_staging_key("ws_abc123", "generation_output")
        self.assertTrue(key.startswith("staging/ws_abc123/generation_output/"))
        self.assertTrue(key.endswith(".dat"))
        validate_object_key(key)  # must not raise

    def test_build_staging_key_is_unique_per_call(self):
        first = build_staging_key("ws_abc123", "generation_output")
        second = build_staging_key("ws_abc123", "generation_output")
        self.assertNotEqual(first, second)

    def test_derive_committed_key_swaps_prefix_and_extension(self):
        staging = build_staging_key("ws_abc123", "generation_output")
        committed = derive_committed_key(staging, "image/png")
        self.assertTrue(committed.startswith("committed/ws_abc123/generation_output/"))
        self.assertTrue(committed.endswith(".png"))


# ---------------------------------------------------------------------------
# Shared migration bootstrap (mirrors tests/test_operation_polling.py).
# ---------------------------------------------------------------------------


def _migrate_to_head() -> None:
    db_file = engine.url.database
    if db_file and db_file != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


def _ensure_workspace(workspace_id: str) -> None:
    with platform_session() as session:
        if session.get(WorkspaceRecord, workspace_id) is None:
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id, created_at=utcnow()))


def _checksum_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"deterministic-test-fixture-bytes" * 4


# ---------------------------------------------------------------------------
# Endpoint tests -- real app, real tenant_session (spec §7.6-§7.12, §7.18).
# ---------------------------------------------------------------------------

WORKSPACE_ID = "ws_upload_intents"
OTHER_WORKSPACE_ID = "ws_upload_intents_other"

OWNER = TenantContext(workspace_id=WORKSPACE_ID, role="owner", initiator_id="actor_a")
OTHER_ACTOR = TenantContext(workspace_id=WORKSPACE_ID, role="owner", initiator_id="actor_b")
OTHER_WORKSPACE_OWNER = TenantContext(
    workspace_id=OTHER_WORKSPACE_ID, role="owner", initiator_id="actor_other_ws"
)


def _upload_body(**overrides) -> dict:
    body = {
        "purpose": "generation_output",
        "owner_kind": "generation",
        "owner_id": None,
        "filename": "output.png",
        "content_type": "image/png",
        "content_length_min": 10,
        "content_length_max": 1000,
        "checksum_sha256_b64": "deadbeef==",
    }
    body.update(overrides)
    return body


class UploadDownloadIntentEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _migrate_to_head()
        _ensure_workspace(WORKSPACE_ID)
        _ensure_workspace(OTHER_WORKSPACE_ID)

    def setUp(self) -> None:
        self._override = OWNER
        app.dependency_overrides[request_tenant_context] = lambda: self._override
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _idem(self) -> dict:
        return {"Idempotency-Key": f"idem-{uuid.uuid4().hex}"}

    # -- create_upload_intent ------------------------------------------------

    def test_create_upload_intent_returns_201_with_grant(self):
        response = self.client.post(
            "/api/v1/upload-intents", json=_upload_body(), headers=self._idem()
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["state"], "pending")
        self.assertEqual(body["purpose"], "generation_output")
        self.assertEqual(body["upload_grant"]["kind"], "signed_put")
        self.assertTrue(body["upload_grant"]["url"])
        self.assertIn("intent_id", body)

    def test_create_upload_intent_requires_idempotency_key(self):
        response = self.client.post("/api/v1/upload-intents", json=_upload_body())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "idempotency_key_required")

    def test_create_upload_intent_rejects_unknown_purpose(self):
        response = self.client.post(
            "/api/v1/upload-intents", json=_upload_body(purpose="not_a_purpose"), headers=self._idem()
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unknown_purpose")

    def test_create_upload_intent_rejects_disallowed_content_type(self):
        response = self.client.post(
            "/api/v1/upload-intents",
            json=_upload_body(purpose="export_package", content_type="image/png"),
            headers=self._idem(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "content_type_not_allowed")

    def test_create_upload_intent_rejects_content_length_over_policy(self):
        response = self.client.post(
            "/api/v1/upload-intents",
            json=_upload_body(content_length_max=10**12),
            headers=self._idem(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "content_length_exceeds_policy")
        # The interface renders the limit from params (#253), not the detail.
        self.assertGreater(response.json()["params"]["limit_mb"], 0)

    def test_create_upload_intent_replay_returns_same_intent_with_fresh_grant(self):
        headers = self._idem()
        first = self.client.post("/api/v1/upload-intents", json=_upload_body(), headers=headers)
        second = self.client.post("/api/v1/upload-intents", json=_upload_body(), headers=headers)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["intent_id"], second.json()["intent_id"])
        # Re-issued, not reused (spec §7.11): every call -- first or replay
        # -- signs a fresh capability against the same persisted
        # staging_key, rather than storing/returning the original grant.
        first_query = first.json()["upload_grant"]["url"].split("staging/", 1)[1]
        second_query = second.json()["upload_grant"]["url"].split("staging/", 1)[1]
        self.assertEqual(first_query.split("?", 1)[0], second_query.split("?", 1)[0])

    def test_create_upload_intent_reused_key_different_body_is_409(self):
        headers = self._idem()
        self.client.post("/api/v1/upload-intents", json=_upload_body(), headers=headers)
        response = self.client.post(
            "/api/v1/upload-intents", json=_upload_body(filename="different.png"), headers=headers
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")

    def _create_intent(self, **overrides) -> str:
        response = self.client.post(
            "/api/v1/upload-intents", json=_upload_body(**overrides), headers=self._idem()
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["intent_id"]

    def test_create_upload_intent_expired_returns_410_on_replay(self):
        headers = self._idem()
        first = self.client.post("/api/v1/upload-intents", json=_upload_body(), headers=headers)
        intent_id = first.json()["intent_id"]
        with tenant_session(OWNER) as session:
            record = get_upload_intent(session, intent_id)
            record.expires_at = utcnow() - timedelta(seconds=1)

        second = self.client.post("/api/v1/upload-intents", json=_upload_body(), headers=headers)
        self.assertEqual(second.status_code, 410)
        self.assertEqual(second.json()["code"], "upload_intent_expired")
        with tenant_session(OWNER) as session:
            from sqlalchemy import select

            record = get_upload_intent(session, intent_id)
            self.assertEqual(record.state, "expired")
            cleanup = session.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WORKSPACE_ID,
                    JobRecord.type == "cleanup_staging_upload",
                    JobRecord.resource_id == intent_id,
                )
            )
            self.assertIsNotNone(cleanup)

    # -- finalize_upload -------------------------------------------------

    def test_finalize_upload_returns_202_with_location_and_enqueues_job(self):
        intent_id = self._create_intent()
        response = self.client.post(
            f"/api/v1/upload-intents/{intent_id}/finalize", headers=self._idem()
        )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["state"], "queued")
        self.assertNotIn("location", response.headers)

        with tenant_session(OWNER) as session:
            from sqlalchemy import select

            job = session.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == WORKSPACE_ID,
                    JobRecord.type == "verify_upload_intent",
                    JobRecord.resource_id == intent_id,
                )
            )
            self.assertIsNotNone(job)
            self.assertEqual(job.payload_json["intent_id"], intent_id)
            self.assertEqual(job.payload_json["operation_id"], body["operation_id"])

    def test_finalize_upload_requires_idempotency_key(self):
        intent_id = self._create_intent()
        response = self.client.post(f"/api/v1/upload-intents/{intent_id}/finalize")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "idempotency_key_required")

    def test_finalize_upload_replay_returns_same_operation(self):
        intent_id = self._create_intent()
        headers = self._idem()
        first = self.client.post(f"/api/v1/upload-intents/{intent_id}/finalize", headers=headers)
        second = self.client.post(f"/api/v1/upload-intents/{intent_id}/finalize", headers=headers)
        self.assertEqual(first.json()["operation_id"], second.json()["operation_id"])

    def test_finalize_upload_unknown_intent_is_404(self):
        response = self.client.post(
            "/api/v1/upload-intents/ui_does_not_exist/finalize", headers=self._idem()
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "upload_intent_not_found")

    def test_finalize_upload_cross_workspace_is_404(self):
        intent_id = self._create_intent()
        self._override = OTHER_WORKSPACE_OWNER
        response = self.client.post(
            f"/api/v1/upload-intents/{intent_id}/finalize", headers=self._idem()
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "upload_intent_not_found")

    def test_finalize_upload_different_actor_is_403(self):
        intent_id = self._create_intent()
        self._override = OTHER_ACTOR
        response = self.client.post(
            f"/api/v1/upload-intents/{intent_id}/finalize", headers=self._idem()
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "finalize_forbidden")

    def test_finalize_upload_expired_intent_is_410(self):
        intent_id = self._create_intent()
        with tenant_session(OWNER) as session:
            record = get_upload_intent(session, intent_id)
            record.expires_at = utcnow() - timedelta(seconds=1)

        response = self.client.post(
            f"/api/v1/upload-intents/{intent_id}/finalize", headers=self._idem()
        )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["code"], "upload_intent_expired")

    def test_finalize_upload_rejected_intent_is_409(self):
        intent_id = self._create_intent()
        with tenant_session(OWNER) as session:
            from app.platform.upload_intents import acquire_verification_lease, _reject

            acquire_verification_lease(session, intent_id, "test-lease")
            _reject(session, intent_id, "test-lease", "checksum_mismatch")

        response = self.client.post(
            f"/api/v1/upload-intents/{intent_id}/finalize", headers=self._idem()
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "upload_intent_rejected")

    def test_finalize_upload_replay_succeeds_even_after_intent_is_later_rejected(self):
        """Spec §5.4: idempotent replay returns the original outcome -- it
        must not re-validate against state the intent moved to *after* the
        original finalize call already committed an Operation. This is the
        scenario a client hits when the original 202 response is lost
        (timeout) but the request itself landed: by the time the client
        retries with the same Idempotency-Key, the Worker may have already
        moved the intent past `pending` (here: rejected it)."""
        intent_id = self._create_intent()
        headers = self._idem()

        first = self.client.post(f"/api/v1/upload-intents/{intent_id}/finalize", headers=headers)
        self.assertEqual(first.status_code, 202)
        operation_id = first.json()["operation_id"]

        with tenant_session(OWNER) as session:
            from app.platform.upload_intents import acquire_verification_lease, _reject

            acquire_verification_lease(session, intent_id, "worker-lease")
            _reject(session, intent_id, "worker-lease", "checksum_mismatch")

        second = self.client.post(f"/api/v1/upload-intents/{intent_id}/finalize", headers=headers)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["operation_id"], operation_id)

    # -- create_download_intent -------------------------------------------

    def _committed_key(self, workspace_id: str = WORKSPACE_ID) -> str:
        return f"committed/{workspace_id}/generation_output/2026/08/30/{uuid.uuid4().hex}.png"

    def test_create_download_intent_returns_201_with_grant(self):
        response = self.client.post(
            "/api/v1/download-intents",
            json={
                "owner_kind": "generation",
                "owner_id": "gen_1",
                "committed_blob_ref_key": self._committed_key(),
                "committed_blob_ref_version": "rev-1",
                "filename": "output.png",
            },
            headers=self._idem(),
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("intent_id", body)
        self.assertTrue(body["download_grant"]["url"])
        self.assertIn("output.png", body["download_grant"]["content_disposition"])

    def test_create_download_intent_rejects_foreign_workspace_key(self):
        response = self.client.post(
            "/api/v1/download-intents",
            json={
                "owner_kind": "generation",
                "owner_id": "gen_1",
                "committed_blob_ref_key": self._committed_key(OTHER_WORKSPACE_ID),
                "committed_blob_ref_version": "rev-1",
                "filename": "output.png",
            },
            headers=self._idem(),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "committed_blob_ref_not_in_workspace")

    def test_create_download_intent_replay_returns_same_intent(self):
        headers = self._idem()
        payload = {
            "owner_kind": "generation",
            "owner_id": "gen_1",
            "committed_blob_ref_key": self._committed_key(),
            "committed_blob_ref_version": "rev-1",
            "filename": "output.png",
        }
        first = self.client.post("/api/v1/download-intents", json=payload, headers=headers)
        second = self.client.post("/api/v1/download-intents", json=payload, headers=headers)
        self.assertEqual(first.json()["intent_id"], second.json()["intent_id"])

    def test_create_download_intent_requires_idempotency_key(self):
        response = self.client.post(
            "/api/v1/download-intents",
            json={
                "owner_kind": "generation",
                "owner_id": "gen_1",
                "committed_blob_ref_key": self._committed_key(),
                "committed_blob_ref_version": "rev-1",
                "filename": "output.png",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "idempotency_key_required")


# ---------------------------------------------------------------------------
# Worker verification sequence (spec §7.8) against a real LocalFileBlobStore.
# ---------------------------------------------------------------------------

VERIFY_WORKSPACE_ID = "ws_verify_upload_intent"
VERIFY_OWNER = TenantContext(workspace_id=VERIFY_WORKSPACE_ID, role="owner", initiator_id="verify_actor")


class _FakeAdmissionReader:
    """Duck-types ``WorkspaceAdmissionReader`` -- ``verify_upload_intent``
    only ever calls ``.read()``, so a plain fake avoids standing up
    PostgreSQL's ``marketai_workspace_admission`` function for these tests."""

    def __init__(self, admission: WorkspaceAdmission | None = None, *, raises: bool = False):
        self._admission = admission
        self._raises = raises

    async def read(self, workspace_id: str) -> WorkspaceAdmission:
        if self._raises:
            raise WorkspaceNotFoundError(workspace_id)
        assert self._admission is not None
        return self._admission


def _default_admission(epoch: int = 0) -> WorkspaceAdmission:
    return WorkspaceAdmission(
        workspace_id=VERIFY_WORKSPACE_ID, authorization_epoch=epoch, entitlement_version=1, suspended=False
    )


class VerifyUploadIntentTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _migrate_to_head()
        _ensure_workspace(VERIFY_WORKSPACE_ID)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.blob_store = LocalFileBlobStore(
            root=self.root, public_base_url="http://localhost:9000", signing_secret=b"test-signing-secret"
        )

    def _create_intent(self, **overrides) -> str:
        from app.platform.upload_intents import create_upload_intent_record

        defaults = dict(
            intent_id=f"ui_{uuid.uuid4().hex}",
            staging_key=build_staging_key(VERIFY_WORKSPACE_ID, "generation_output"),
            owner_kind="generation",
            owner_id=None,
            purpose="generation_output",
            filename_original="output.png",
            content_type="image/png",
            content_length_min=1,
            content_length_max=10_000,
            checksum_sha256_b64=_checksum_b64(PNG_BYTES),
            ttl_seconds=300,
        )
        defaults.update(overrides)
        with tenant_session(VERIFY_OWNER) as session:
            record = create_upload_intent_record(session, **defaults)
            return record.id, record.staging_key

    def _write_staged(self, staging_key: str, data: bytes) -> None:
        path = resolve_key_path(self.root, staging_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def _verify(self, intent_id: str, *, admission_reader=None, lease_id: str = "lease-1"):
        return await verify_upload_intent(
            VERIFY_OWNER,
            intent_id,
            session_factory=lambda: tenant_session(VERIFY_OWNER),
            blob_store=self.blob_store,
            admission_reader=admission_reader,
            lease_id=lease_id,
        )

    async def test_golden_path_finalizes_and_commits(self):
        intent_id, staging_key = self._create_intent()
        self._write_staged(staging_key, PNG_BYTES)

        outcome = await self._verify(intent_id, admission_reader=_FakeAdmissionReader(_default_admission()))

        self.assertEqual(outcome.kind, "finalized")
        self.assertIsNotNone(outcome.committed)
        self.assertTrue(resolve_key_path(self.root, outcome.committed.committed_key).exists())
        self.assertFalse(resolve_key_path(self.root, staging_key).exists())

        with tenant_session(VERIFY_OWNER) as session:
            record = get_upload_intent(session, intent_id)
            self.assertEqual(record.state, "finalized")
            self.assertEqual(record.committed_blob_ref_key, outcome.committed.committed_key)
            self.assertIsNone(record.verification_lease_id)
            from sqlalchemy import select

            cleanup = session.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == VERIFY_WORKSPACE_ID,
                    JobRecord.type == "cleanup_staging_upload",
                    JobRecord.resource_id == intent_id,
                )
            )
            self.assertIsNotNone(cleanup)
            self.assertTrue(cleanup.safe_to_retry_after_crash)
            self.assertEqual(cleanup.payload_json["staging_key"], staging_key)

    async def test_checksum_mismatch_is_rejected(self):
        intent_id, staging_key = self._create_intent(checksum_sha256_b64="not-the-real-checksum")
        self._write_staged(staging_key, PNG_BYTES)

        outcome = await self._verify(intent_id)

        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.rejection_code, "checksum_mismatch")
        with tenant_session(VERIFY_OWNER) as session:
            record = get_upload_intent(session, intent_id)
            self.assertEqual(record.state, "rejected")
            self.assertEqual(record.rejection_code, "checksum_mismatch")
            from sqlalchemy import select

            cleanup = session.scalar(
                select(JobRecord).where(
                    JobRecord.workspace_id == VERIFY_WORKSPACE_ID,
                    JobRecord.type == "cleanup_staging_upload",
                    JobRecord.resource_id == intent_id,
                )
            )
            self.assertIsNotNone(cleanup)

    async def test_size_mismatch_is_rejected(self):
        intent_id, staging_key = self._create_intent(
            content_length_min=1, content_length_max=4, checksum_sha256_b64=_checksum_b64(PNG_BYTES)
        )
        self._write_staged(staging_key, PNG_BYTES)

        outcome = await self._verify(intent_id)

        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.rejection_code, "size_mismatch")

    async def test_mime_not_in_purpose_allowlist_is_rejected(self):
        zip_bytes = b"PK\x03\x04" + b"not actually a real zip but has the magic bytes"
        intent_id, staging_key = self._create_intent(
            purpose="generation_output",
            content_type="application/zip",
            content_length_max=10_000,
            checksum_sha256_b64=_checksum_b64(zip_bytes),
        )
        self._write_staged(staging_key, zip_bytes)

        outcome = await self._verify(intent_id)

        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.rejection_code, "mime_type_not_allowed")

    async def test_authorization_epoch_stale_is_rejected(self):
        intent_id, staging_key = self._create_intent()
        self._write_staged(staging_key, PNG_BYTES)

        outcome = await self._verify(
            intent_id, admission_reader=_FakeAdmissionReader(_default_admission(epoch=99))
        )

        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.rejection_code, "authorization_epoch_stale")

    async def test_workspace_suspended_is_rejected(self):
        intent_id, staging_key = self._create_intent()
        self._write_staged(staging_key, PNG_BYTES)
        suspended = WorkspaceAdmission(
            workspace_id=VERIFY_WORKSPACE_ID, authorization_epoch=0, entitlement_version=1, suspended=True
        )

        outcome = await self._verify(intent_id, admission_reader=_FakeAdmissionReader(suspended))

        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.rejection_code, "workspace_suspended")

    async def test_missing_staging_object_is_rejected(self):
        intent_id, _staging_key = self._create_intent()
        # Never written -- simulates a finalize call before/without an upload.

        outcome = await self._verify(intent_id)

        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.rejection_code, "staging_object_not_found")

    async def test_double_verification_second_caller_loses_the_cas_lease(self):
        intent_id, staging_key = self._create_intent()
        self._write_staged(staging_key, PNG_BYTES)

        with tenant_session(VERIFY_OWNER) as session:
            from app.platform.upload_intents import acquire_verification_lease

            acquire_verification_lease(session, intent_id, "someone-elses-lease")

        with self.assertRaises(UploadIntentLeaseLostError):
            await self._verify(intent_id, lease_id="a-different-lease")

    async def test_unknown_intent_raises_not_found(self):
        with self.assertRaises(UploadIntentNotFoundError):
            await self._verify("ui_does_not_exist")
