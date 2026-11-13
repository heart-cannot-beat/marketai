"""Storage admission under the real roles on PostgreSQL (#158).

Every table the check reads is FORCE RLS. Here the API runs as
``marketai_domain_api`` through ``TestClient`` and directly:

* the API role reads the Workspace's ``storage.bytes.max``, sums its stored
  bytes (#148's rule) and its open upload intents under RLS, with no new
  grant: an upload of exactly the remaining room is admitted, one byte more is
  ``storage_limit_exceeded``, and both are decided as that role;
* negative: in Workspace A's context, Workspace B's stored bytes, open intents
  and limit are invisible, even under the same object key and even when the
  query names B; B being full refuses nothing in A;
* contention (contract §11, last paragraph): an upload admission that has not
  committed holds the Workspace's quota lock; a second admission waits, then
  counts the first one's intent (refused) -- or, when the first rolls back,
  finds the room free (admitted). Racing admissions never jointly exceed the
  limit. Another Workspace never waits on that lock;
* generation is refused at the limit and admitted below it, as the API role.

Workspaces, projections, stored bytes and intents are written as the schema
owner, as the control plane and the Worker would. When a test ends its
Workspaces are suspended and its intents expired.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    ReferenceObjectRecord,
    UploadIntentRecord,
    WorkspaceRecord,
)
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.storage_quota import (
    StorageLimitExceededError,
    StorageLimitReachedError,
    StorageLimitUnavailableError,
    admit_generation,
    admit_upload,
    open_upload_bytes,
    storage_limit_bytes,
)
from app.entitlements.storage_usage import workspace_storage_bytes
from app.library.contracts import ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.platform.upload_intents import build_staging_key, create_upload_intent_record

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
LIMIT = 10_000
#: How long a waiting admission must stay blocked to count as waiting.
BLOCKED_FOR = 1.0


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class StorageQuotaPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url, pool_size=10) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)
        self.owner, self.api, self.worker = self.engines
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False) for e in self.engines]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.workspaces: list[str] = []
        self.addCleanup(self._tidy)

    # -- roles and fixtures -------------------------------------------------

    @contextmanager
    def _as(self, index: int):
        """Every session the code under test opens, as one role."""
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]):
            yield

    def _workspace(self, *, limit: int | None = LIMIT) -> TenantContext:
        workspace_id = f"ws_storage_quota_pg_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
        self.workspaces.append(workspace_id)
        ctx = TenantContext(workspace_id=workspace_id, role=ROLE_OWNER, initiator_id="usr_acme_owner",
                            request_id="test-req", trace_id="trace-test")
        if limit is not None:
            now = datetime.now(timezone.utc)
            with self._as(OWNER), tenant_session(ctx) as session:  # the projection writer
                session.add(EntitlementProjectionRecord(
                    workspace_id=workspace_id, entitlement_version=1, lease_sequence=1,
                    projection_version=1, schema_version=2, semantic_hash="h" * 8,
                    valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                    period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                    resolved_entitlements_json={"storage.bytes.max": limit},
                ))
        return ctx

    def _stored(self, ctx: TenantContext, size: int, key: str | None = None) -> None:
        with Session(self.owner) as session, session.begin():
            session.add(ReferenceObjectRecord(
                id=f"refobj_{uuid.uuid4().hex[:12]}", workspace_id=ctx.workspace_id,
                content_sha256=uuid.uuid4().hex * 2,
                original_object_key=key or f"committed/{ctx.workspace_id}/{uuid.uuid4().hex}.png",
                declared_mime="image/png", byte_size=size, state="released",
            ))

    def _intent(self, ctx: TenantContext, size: int) -> None:
        now = datetime.now(timezone.utc)
        with Session(self.owner) as session, session.begin():
            session.add(UploadIntentRecord(
                id=f"ui_{uuid.uuid4().hex}", workspace_id=ctx.workspace_id, initiator_id=ctx.initiator_id,
                owner_kind="reference", staging_key=build_staging_key(ctx.workspace_id, "reference_asset"),
                purpose="reference_asset", filename_original="acme-charge.png", content_type="image/png",
                content_length_min=1, content_length_max=size, checksum_sha256_b64="deadbeef==",
                state="pending", created_at=now, expires_at=now + timedelta(hours=1),
            ))

    def _upload(self, ctx: TenantContext, size: int):
        self.current = ctx
        with self._as(API):
            return self.client.post(
                "/api/v1/upload-intents",
                json={
                    "purpose": "reference_asset", "owner_kind": "reference", "owner_id": None,
                    "filename": "acme-charge-hero.png", "content_type": "image/png",
                    "content_length_min": 1, "content_length_max": size,
                    "checksum_sha256_b64": "deadbeef==",
                },
                headers={"Idempotency-Key": f"ci-only-{uuid.uuid4().hex}"},
            )

    def _admit_and_write(self, session, ctx: TenantContext, size: int) -> None:
        """What the route's create step does: admit, then write the intent."""
        admit_upload(session, ctx, size)
        create_upload_intent_record(
            session, intent_id=f"ui_{uuid.uuid4().hex}",
            staging_key=build_staging_key(ctx.workspace_id, "reference_asset"),
            owner_kind="reference", owner_id=None, purpose="reference_asset",
            filename_original="acme-charge.png", content_type="image/png",
            content_length_min=1, content_length_max=size, checksum_sha256_b64="deadbeef==",
            ttl_seconds=3600,
        )

    def _open_intents(self, ctx: TenantContext) -> int:
        with Session(self.owner) as session:
            return session.scalar(select(func.count()).select_from(UploadIntentRecord).where(
                UploadIntentRecord.workspace_id == ctx.workspace_id, UploadIntentRecord.state == "pending"))

    def _tidy(self) -> None:
        """Leave the shared cluster as later CI steps expect it."""
        if not self.workspaces:
            return
        with self.owner.begin() as connection:
            connection.execute(text("UPDATE upload_intent_records SET state = 'expired' "
                                    " WHERE workspace_id = ANY(:ids) AND state IN ('pending', 'verifying')"),
                               {"ids": self.workspaces})
            connection.execute(text("UPDATE workspaces SET suspended = true WHERE id = ANY(:ids)"),
                               {"ids": self.workspaces})

    def _in_thread(self, target) -> tuple[threading.Thread, dict]:
        """Run ``target`` on its own connection. The caller holds ``_as(API)``
        until the thread ends: ``patch`` is not thread-safe."""
        outcome: dict = {}

        def run():
            try:
                outcome["value"] = target()
            except BaseException as exc:  # noqa: BLE001 -- reported to the test thread
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    # -- reading under RLS --------------------------------------------------

    def test_the_api_role_sums_under_rls_and_decides_through_the_route(self):
        ctx = self._workspace()
        self._stored(ctx, 4_000)
        self._stored(ctx, 2_000)
        self._intent(ctx, 1_000)

        with self._as(API), tenant_session(ctx) as session:
            self.assertEqual(session.scalar(text("SELECT current_user")), "marketai_domain_api")
            self.assertEqual(storage_limit_bytes(session, ctx), LIMIT)
            self.assertEqual(workspace_storage_bytes(session, ctx), 6_000)
            self.assertEqual(open_upload_bytes(session, ctx), 1_000)

        refused = self._upload(ctx, 3_001)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["code"], "storage_limit_exceeded")
        self.assertEqual(refused.json()["params"]["stored_bytes"], 6_000)
        admitted = self._upload(ctx, 3_000)
        self.assertEqual(admitted.status_code, 201, admitted.text)
        self.assertEqual(self._open_intents(ctx), 2)

    def test_a_workspace_without_the_limit_is_refused_as_the_api_role(self):
        ctx = self._workspace(limit=None)
        response = self._upload(ctx, 1)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "entitlement_unavailable")
        self.assertEqual(self._open_intents(ctx), 0)

    def test_another_workspace_is_invisible(self):
        mine, theirs = self._workspace(), self._workspace(limit=LIMIT * 2)
        shared_key = f"committed/shared/{uuid.uuid4().hex}.png"
        self._stored(theirs, LIMIT, key=shared_key)
        self._intent(theirs, LIMIT)
        self._stored(mine, 3_000, key=shared_key)

        with self._as(API), tenant_session(mine) as session:
            self.assertEqual(workspace_storage_bytes(session, mine), 3_000)
            self.assertEqual(open_upload_bytes(session, mine), 0)
            # Asking for B by name inside A's context finds nothing of B's.
            self.assertEqual(workspace_storage_bytes(session, theirs), 0)
            self.assertEqual(open_upload_bytes(session, theirs), 0)
            with self.assertRaises(StorageLimitUnavailableError):
                storage_limit_bytes(session, theirs)

        self.assertEqual(self._upload(mine, 7_000).status_code, 201)
        self.assertEqual(self._upload(theirs, 1).json()["code"], "storage_limit_exceeded")

    # -- contention ---------------------------------------------------------

    def test_a_second_upload_waits_for_the_first_and_counts_it(self):
        ctx = self._workspace()
        with self._as(API):
            with tenant_session(ctx) as first:
                self._admit_and_write(first, ctx, 6_000)
                thread, outcome = self._in_thread(lambda: self._second(ctx, 6_000))
                thread.join(BLOCKED_FOR)
                self.assertTrue(thread.is_alive(), "the second admission did not wait for the first")
            thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get("error"), StorageLimitExceededError, outcome)
        self.assertEqual(outcome["error"].admission.open_upload_bytes, 6_000)
        self.assertEqual(self._open_intents(ctx), 1)

    def test_a_rolled_back_admission_frees_its_room(self):
        ctx = self._workspace()
        with self._as(API):
            with self.assertRaisesRegex(RuntimeError, "abandon"), tenant_session(ctx) as first:
                self._admit_and_write(first, ctx, 6_000)
                thread, outcome = self._in_thread(lambda: self._second(ctx, 6_000))
                thread.join(BLOCKED_FOR)
                self.assertTrue(thread.is_alive(), "the second admission did not wait for the first")
                raise RuntimeError("abandon the first admission")
            thread.join(30)
        self.assertNotIn("error", outcome)
        self.assertEqual(self._open_intents(ctx), 1)

    def test_racing_uploads_never_jointly_exceed_the_limit(self):
        ctx = self._workspace()
        start = threading.Barrier(5)

        def race():
            start.wait(10)
            self._second(ctx, 3_000)

        with self._as(API):
            threads = [self._in_thread(race) for _ in range(5)]
            for thread, _ in threads:
                thread.join(60)
        errors = [outcome.get("error") for _, outcome in threads]
        self.assertEqual(sum(error is None for error in errors), 3, errors)
        self.assertTrue(all(error is None or isinstance(error, StorageLimitExceededError) for error in errors), errors)
        self.assertEqual(self._open_intents(ctx), 3)

    def test_another_workspace_does_not_wait(self):
        mine, theirs = self._workspace(), self._workspace()
        with self._as(API), tenant_session(mine) as first:
            self._admit_and_write(first, mine, 6_000)
            thread, outcome = self._in_thread(lambda: self._second(theirs, 6_000))
            thread.join(10)
            self.assertFalse(thread.is_alive(), "another Workspace waited on this one's lock")
        self.assertNotIn("error", outcome)
        self.assertEqual(self._open_intents(theirs), 1)

    def _second(self, ctx: TenantContext, size: int) -> None:
        with tenant_session(ctx) as session:
            self._admit_and_write(session, ctx, size)

    # -- generation ---------------------------------------------------------

    def test_generation_is_refused_at_the_limit_and_admitted_below_it(self):
        ctx = self._workspace()
        self._stored(ctx, LIMIT - 1)
        self._intent(ctx, LIMIT)  # open uploads do not count for generation
        with self._as(API), tenant_session(ctx) as session:
            self.assertEqual(admit_generation(session, ctx).stored_bytes, LIMIT - 1)

        self._stored(ctx, 1)
        with self._as(API), tenant_session(ctx) as session, self.assertRaises(StorageLimitReachedError):
            admit_generation(session, ctx)


if __name__ == "__main__":
    unittest.main()
