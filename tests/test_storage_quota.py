"""Storage admission: uploads and generations within ``storage.bytes.max`` (#158).

On SQLite, through the real routes, each test in its own Workspaces:

* the limit is the Entitlement Projection's ``storage.bytes.max`` and fails
  closed -- no projection, no key, or a value that is not a whole, non-negative
  number refuses as ``entitlement_unavailable`` and writes nothing;
* upload: stored bytes + the declared bytes of open intents + this upload's
  ``content_length_max`` may reach the limit exactly; one byte more is
  ``storage_limit_exceeded``. ``pending`` and ``verifying`` intents count
  until they expire; expired, finalized and rejected ones never do. A refusal
  leaves no intent and no idempotency record, so the same key admits once
  there is room; a replay of an admitted key is not checked again;
* another Workspace's stored bytes and open intents never count, even under
  the same object key, and its being full never refuses this one;
* a Workspace over its limit keeps every row; it only stops admitting.

Generation is in ``tests/test_storage_quota_generation.py``; two concurrent
uploads, and the sum under the API role's RLS, in
``tests/test_storage_quota_pg.py``.
"""
from __future__ import annotations

import logging
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import (
    IdempotencyRecord,
    ReferenceObjectRecord,
    UploadIntentRecord,
    WorkspaceRecord,
    utcnow,
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
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from tests.support.storage_limit import grant_storage_limit

LIMIT = 10_000


def _migrate_to_head() -> None:
    db_file = engine.url.database
    if db_file and db_file != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")


def _body(size: int, **overrides) -> dict:
    body = {
        "purpose": "reference_asset",
        "owner_kind": "reference",
        "owner_id": None,
        "filename": "acme-charge-hero.png",
        "content_type": "image/png",
        "content_length_min": 1,
        "content_length_max": size,
        "checksum_sha256_b64": "deadbeef==",
    }
    body.update(overrides)
    return body


class StorageQuotaFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _migrate_to_head()

    def setUp(self) -> None:
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _workspace(self, *, limit: int | None = LIMIT, project: bool = True) -> TenantContext:
        workspace_id = f"ws_storage_quota_{uuid.uuid4().hex[:12]}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}", created_at=utcnow()))
            session.flush()
            if project:
                grant_storage_limit(session, workspace_id, limit)
        return TenantContext(workspace_id=workspace_id, role="owner", initiator_id="usr_acme_owner")

    def _stored(self, ctx: TenantContext, size: int, key: str | None = None) -> None:
        """A released reference original of ``size`` bytes at ``key``."""
        with platform_session() as session:
            session.add(ReferenceObjectRecord(
                id=f"refobj_{uuid.uuid4().hex[:12]}",
                workspace_id=ctx.workspace_id,
                content_sha256=uuid.uuid4().hex * 2,
                original_object_key=key or f"committed/{ctx.workspace_id}/{uuid.uuid4().hex}.png",
                declared_mime="image/png",
                byte_size=size,
                state="released",
            ))

    def _intent(self, ctx: TenantContext, size: int, *, state: str = "pending", expires_in: timedelta = timedelta(hours=1)) -> str:
        intent_id = f"ui_{uuid.uuid4().hex}"
        now = utcnow()
        with platform_session() as session:
            session.add(UploadIntentRecord(
                id=intent_id,
                workspace_id=ctx.workspace_id,
                initiator_id=ctx.initiator_id,
                owner_kind="reference",
                staging_key=f"staging/{ctx.workspace_id}/reference_asset/{uuid.uuid4().hex}.dat",
                purpose="reference_asset",
                filename_original="acme-charge.png",
                content_type="image/png",
                content_length_min=1,
                content_length_max=size,
                checksum_sha256_b64="deadbeef==",
                state=state,
                created_at=now,
                expires_at=now + expires_in,
            ))
        return intent_id

    def _upload(self, ctx: TenantContext, size: int, *, key: str | None = None):
        self.current = ctx
        return self.client.post(
            "/api/v1/upload-intents",
            json=_body(size),
            headers={"Idempotency-Key": key or f"ci-only-{uuid.uuid4().hex}"},
        )

    def _counts(self, ctx: TenantContext) -> tuple[int, int, int]:
        with platform_session() as session:
            return tuple(
                session.scalar(select(func.count()).select_from(model).where(model.workspace_id == ctx.workspace_id))
                for model in (UploadIntentRecord, IdempotencyRecord, ReferenceObjectRecord)
            )


class UploadAdmissionTests(StorageQuotaFixtures):
    def test_an_upload_of_exactly_the_remaining_room_is_admitted(self):
        ctx = self._workspace()
        self._stored(ctx, 6_000)
        self._intent(ctx, 1_000)

        response = self._upload(ctx, 3_000)

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["state"], "pending")

    def test_one_byte_over_the_limit_is_refused_and_writes_nothing(self):
        ctx = self._workspace()
        self._stored(ctx, 6_000)
        self._intent(ctx, 1_000)
        before = self._counts(ctx)

        response = self._upload(ctx, 3_001)

        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual(body["code"], "storage_limit_exceeded")
        self.assertFalse(body["retryable"])
        self.assertEqual(body["params"], {
            "limit_gib": "0",
            "limit_bytes": LIMIT,
            "stored_bytes": 6_000,
            "open_upload_bytes": 1_000,
            "requested_bytes": 3_001,
        })
        self.assertEqual(self._counts(ctx), before)

    def test_the_admitted_intent_counts_against_the_next_upload(self):
        ctx = self._workspace()
        self.assertEqual(self._upload(ctx, 6_000).status_code, 201)

        self.assertEqual(self._upload(ctx, 4_001).json()["code"], "storage_limit_exceeded")
        self.assertEqual(self._upload(ctx, 4_000).status_code, 201)
        self.assertEqual(self._upload(ctx, 1).json()["code"], "storage_limit_exceeded")

    def test_open_intents_count_until_they_expire_or_end(self):
        ctx = self._workspace()
        self._intent(ctx, 1_000, state="pending")
        self._intent(ctx, 2_000, state="verifying")
        # Never counted: past their expiry (one not yet marked), or ended.
        self._intent(ctx, 4_000, state="pending", expires_in=timedelta(seconds=-1))
        self._intent(ctx, 4_000, state="verifying", expires_in=timedelta(seconds=-1))
        self._intent(ctx, 4_000, state="expired", expires_in=timedelta(seconds=-1))
        self._intent(ctx, 4_000, state="finalized")
        self._intent(ctx, 4_000, state="rejected")

        with tenant_session(ctx) as session:
            self.assertEqual(open_upload_bytes(session, ctx), 3_000)
        self.assertEqual(self._upload(ctx, 7_001).json()["code"], "storage_limit_exceeded")
        self.assertEqual(self._upload(ctx, 7_000).status_code, 201)

    def test_a_refused_key_admits_once_there_is_room(self):
        ctx = self._workspace()
        self._stored(ctx, LIMIT)
        key = f"ci-only-{uuid.uuid4().hex}"
        self.assertEqual(self._upload(ctx, 1, key=key).json()["code"], "storage_limit_exceeded")

        with platform_session() as session:
            grant_storage_limit(session, ctx.workspace_id, LIMIT + 1)

        self.assertEqual(self._upload(ctx, 1, key=key).status_code, 201)

    def test_a_replay_of_an_admitted_upload_is_not_checked_again(self):
        ctx = self._workspace()
        key = f"ci-only-{uuid.uuid4().hex}"
        first = self._upload(ctx, 5_000, key=key)
        self.assertEqual(first.status_code, 201)
        self._stored(ctx, LIMIT)  # now far over

        replay = self._upload(ctx, 5_000, key=key)

        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["intent_id"], first.json()["intent_id"])

    def test_a_workspace_over_its_limit_keeps_everything(self):
        ctx = self._workspace()
        self._stored(ctx, 8_000)
        self._stored(ctx, 7_000)
        self._intent(ctx, 500, state="finalized")
        before = self._counts(ctx)

        self.assertEqual(self._upload(ctx, 1).json()["code"], "storage_limit_exceeded")

        self.assertEqual(self._counts(ctx), before)
        with tenant_session(ctx) as session:
            self.assertEqual(
                session.scalar(select(func.sum(ReferenceObjectRecord.byte_size)).where(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id)),
                15_000,
            )

    def test_refusals_are_logged_with_the_numbers(self):
        ctx = self._workspace()
        self._stored(ctx, LIMIT)
        logger = logging.getLogger("app.entitlements.storage_quota")
        logger.disabled = False  # Alembic's fileConfig disables existing loggers
        with self.assertLogs(logger, level="INFO") as logs:
            self._upload(ctx, 10)
        record = logs.records[0]
        self.assertEqual(record.getMessage(), "storage_admission_refused")
        self.assertEqual(
            (record.workspace_id, record.admission, record.stored_bytes, record.requested_bytes),
            (ctx.workspace_id, "upload", LIMIT, 10),
        )


class OtherWorkspaceTests(StorageQuotaFixtures):
    def test_another_workspace_never_counts(self):
        mine, theirs = self._workspace(), self._workspace()
        shared_key = f"committed/shared/{uuid.uuid4().hex}.png"
        self._stored(theirs, LIMIT, key=shared_key)
        self._intent(theirs, LIMIT, state="pending")
        self._stored(mine, 4_000, key=shared_key)

        with tenant_session(mine) as session:
            self.assertEqual(open_upload_bytes(session, mine), 0)
        self.assertEqual(self._upload(mine, 6_000).status_code, 201)
        self.assertEqual(self._upload(theirs, 1).json()["code"], "storage_limit_exceeded")

    def test_another_workspaces_limit_is_not_read(self):
        mine = self._workspace(project=False)
        self._workspace(limit=LIMIT)

        response = self._upload(mine, 1)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "entitlement_unavailable")


class FailClosedTests(StorageQuotaFixtures):
    def _refused(self, ctx: TenantContext) -> None:
        before = self._counts(ctx)
        response = self._upload(ctx, 1)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "entitlement_unavailable")
        self.assertEqual(self._counts(ctx), before)
        with tenant_session(ctx) as session, self.assertRaises(StorageLimitUnavailableError):
            admit_generation(session, ctx)

    def test_no_projection(self):
        self._refused(self._workspace(project=False))

    def test_no_storage_key(self):
        self._refused(self._workspace(limit=None))

    def test_values_that_are_not_a_whole_number_of_bytes(self):
        for value in (True, False, -1, 1.5, "ten", "NaN", "Infinity", None, [LIMIT], {"bytes": LIMIT}):
            with self.subTest(value=value):
                ctx = self._workspace(limit=LIMIT)
                with platform_session() as session:
                    projection = session.get(EntitlementProjectionRecord, ctx.workspace_id)
                    projection.resolved_entitlements_json = {"storage.bytes.max": value}
                self._refused(ctx)

    def test_whole_numbers_in_any_spelling_are_read(self):
        for value, expected in ((LIMIT, LIMIT), (str(LIMIT), LIMIT), (1e4, LIMIT), ("1E+4", LIMIT), (0, 0)):
            with self.subTest(value=value):
                ctx = self._workspace()
                with platform_session() as session:
                    projection = session.get(EntitlementProjectionRecord, ctx.workspace_id)
                    projection.resolved_entitlements_json = {"storage.bytes.max": value}
                with tenant_session(ctx) as session:
                    self.assertEqual(storage_limit_bytes(session, ctx), expected)

    def test_a_zero_limit_admits_nothing(self):
        ctx = self._workspace(limit=0)
        self.assertEqual(self._upload(ctx, 1).json()["code"], "storage_limit_exceeded")
        with tenant_session(ctx) as session, self.assertRaises(StorageLimitReachedError):
            admit_generation(session, ctx)


class AdmissionFunctionTests(StorageQuotaFixtures):
    def test_generation_is_refused_at_the_limit_and_admitted_below_it(self):
        ctx = self._workspace()
        self._stored(ctx, LIMIT - 1)
        self._intent(ctx, LIMIT)  # open uploads do not count for generation
        with tenant_session(ctx) as session:
            admission = admit_generation(session, ctx)
        self.assertEqual((admission.stored_bytes, admission.limit_bytes), (LIMIT - 1, LIMIT))

        self._stored(ctx, 1)
        with tenant_session(ctx) as session, self.assertRaises(StorageLimitReachedError) as refused:
            admit_generation(session, ctx)
        self.assertEqual(refused.exception.code, "storage_limit_reached")
        self.assertEqual(refused.exception.params["stored_bytes"], LIMIT)

    def test_upload_admission_reports_what_it_read(self):
        ctx = self._workspace()
        self._stored(ctx, 2_000)
        self._intent(ctx, 3_000)
        with tenant_session(ctx) as session:
            admission = admit_upload(session, ctx, 5_000)
        self.assertEqual(
            (admission.limit_bytes, admission.stored_bytes, admission.open_upload_bytes),
            (LIMIT, 2_000, 3_000),
        )
        with tenant_session(ctx) as session, self.assertRaises(StorageLimitExceededError):
            admit_upload(session, ctx, 5_001)

    def test_the_limit_is_shown_in_gib(self):
        for limit, shown in ((10 * 1024 ** 3, "10"), (1536 * 1024 ** 2, "1.5"), (LIMIT, "0")):
            with self.subTest(limit=limit):
                ctx = self._workspace(limit=limit)
                with tenant_session(ctx) as session:
                    admission = admit_generation(session, ctx)
                self.assertEqual(admission.params()["limit_gib"], shown)


if __name__ == "__main__":
    unittest.main()
