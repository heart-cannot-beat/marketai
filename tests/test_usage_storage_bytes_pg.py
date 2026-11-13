"""Daily ``storage_bytes`` Usage under the real roles on PostgreSQL (#148, Usage S5).

``workspaces``, ``jobs`` and every table with a byte column are FORCE RLS, and
the Worker has no tenant to list Workspaces under before it dispatches. Here
the Worker runs as ``marketai_domain_worker`` through the deployed
``WorkerRuntime``, the API as ``marketai_domain_api`` through ``TestClient``:

* only the Worker may execute ``marketai_storage_usage_workspaces_v1``: it is
  SECURITY DEFINER, ``STABLE``, pins ``search_path`` to ``''``, returns ids
  and epochs only, and PUBLIC, the API and the read-only role are refused;
* the main path: the runtime's dispatch lists the Workspaces through the
  function and enqueues one Job per active Workspace in its signed context;
  each Job sums its own Workspace's byte columns (a key two rows name counts
  once) and appends one gauge row, which the CHECK constraints of #145
  accept; the suspended Workspace gets nothing; a second pass the same day
  enqueues nothing and adds no row, and a replayed measurement is a counted
  duplicate;
* negative: inside Workspace A's context the Worker's sum sees none of B's
  rows, even under the same key, and asking for B's bytes there finds 0;
* a Job the claim rejects for a stale authorization epoch does not cost the
  day: the next dispatch gives the Workspace a Job under its new epoch;
* ``GET /api/v1/usage`` returns the latest ``storage_bytes`` in the window,
  for a Member and an Owner, never a sum;
* the migration drops the function on downgrade and restores it, with its
  grant, on upgrade.

Workspaces are opened as the schema owner, as the control plane would. When a
test ends, the storage Jobs its dispatch opened in any Workspace are
cancelled, its own Workspaces are suspended (so no later dispatch lists them)
and their usage rows, export packages and Operations removed by the owner
with the append-only triggers disabled for that one transaction.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``; ``DOMAIN_READONLY_DATABASE_URL`` adds the read-only role
to the refusal checks.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, ReferenceObjectRecord, WorkspaceRecord, utcnow
from app.entitlements.storage_usage import (
    JOB_TYPE,
    record_storage_bytes,
    storage_usage_job_key,
    workspace_storage_bytes,
)
from app.entitlements.usage_models import UsageEventRecord
from app.generation.models import ExportPackageRecord
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.operations import create_operation
from app.platform.tenancy import TenantContext, system_context
from app.worker_runtime import WorkerRuntime

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
ROOT = Path(__file__).resolve().parents[1]
FUNCTION = "marketai_storage_usage_workspaces_v1(date, integer)"
REVISION, DOWN_REVISION = "zzl148a1b2c3", "zzk145a1b2c3"


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class StorageUsagePostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url) for url in URLS]
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
        with self.owner.connect() as connection:
            self.started = connection.scalar(text("SELECT clock_timestamp()"))
        self.addCleanup(self._tidy)
        self.runtime = WorkerRuntime(owner=f"ci-only-storage-pg-{uuid.uuid4().hex[:6]}")

    # -- roles and fixtures -------------------------------------------------

    @contextmanager
    def _as(self, index: int):
        """Every session the code under test opens, and the Job claim, as one role."""
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str, **columns) -> TenantContext:
        workspace_id = f"ws_storage_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}", **columns))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role=ROLE_OWNER, initiator_id="usr_acme_owner",
                             request_id="test-req", trace_id="trace-test")

    def _reference(self, workspace_id: str, size: int, key: str, *, derivative: tuple[str, int] = ("", 0)) -> None:
        with Session(self.owner) as session, session.begin():
            session.add(ReferenceObjectRecord(
                id=f"refobj_{uuid.uuid4().hex[:12]}", workspace_id=workspace_id, content_sha256=uuid.uuid4().hex * 2,
                original_object_key=key, declared_mime="image/png", byte_size=size, state="released",
                derivative_object_key=derivative[0], derivative_byte_size=derivative[1],
            ))

    def _package(self, ctx: TenantContext, size: int, key: str) -> None:
        with self._as(API), tenant_session(ctx) as session:  # the API admits the export
            operation_id = create_operation(session, command_type="generation.export_package", context=ctx).id
        with Session(self.owner) as session, session.begin():
            session.add(ExportPackageRecord(
                id=f"exp_{uuid.uuid4().hex[:12]}", workspace_id=ctx.workspace_id, operation_id=operation_id,
                actor_id="usr_acme_owner", selector_json={}, selected_versions_json={}, execution_mode="mock",
                state="ready", committed_object_key=key, byte_size=size,
            ))

    def _tidy(self) -> None:
        """Leave the shared cluster as later CI steps expect it."""
        with self.owner.begin() as connection:
            connection.execute(text(
                "UPDATE jobs SET state = 'cancelled', finished_at = clock_timestamp() "
                " WHERE state IN ('queued', 'retry_wait') "
                "   AND (workspace_id = ANY(:ids) OR (type = :type AND created_at >= :started))"
            ), {"ids": self.workspaces, "type": JOB_TYPE, "started": self.started})
            if not self.workspaces:
                return
            connection.execute(text("UPDATE workspaces SET suspended = true WHERE id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text("ALTER TABLE usage_events DISABLE TRIGGER trg_usage_events_immutable"))
            connection.execute(text("DELETE FROM usage_events WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text("ALTER TABLE usage_events ENABLE TRIGGER trg_usage_events_immutable"))
            # The export packages' Operations stay queued and would show as
            # stale in the health stats an hour later (tests.test_health_readiness).
            connection.execute(text("DELETE FROM export_packages WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text(
                "ALTER TABLE operation_state_events DISABLE TRIGGER operation_state_events_reject_mutation"))
            connection.execute(text("DELETE FROM operation_state_events WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text("DELETE FROM operation_records WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text(
                "ALTER TABLE operation_state_events ENABLE TRIGGER operation_state_events_reject_mutation"))

    # -- the Worker ---------------------------------------------------------

    def _today(self):
        return datetime.now(timezone.utc).date()

    def _dispatch(self, *workspace_ids: str) -> None:
        """The runtime's dispatch, until each named Workspace has today's Job.

        One pass lists at most 200 Workspaces in id order, and a shared CI
        cluster may hold more from earlier steps."""
        for _ in range(20):
            self.runtime._next_storage_usage_dispatch_at = 0.0  # due now
            with self._as(WORKER):
                asyncio.run(self.runtime._dispatch_storage_usage_if_due())
            if all(self._jobs(workspace_id) for workspace_id in workspace_ids):
                return
        self.fail("the dispatch never reached the Workspaces")

    def _drain(self, *workspace_ids: str) -> int:
        """Run the named Workspaces' Jobs as the Worker; every other Job waits."""
        handled = 0
        for _ in range(20):
            with Session(self.owner) as session, session.begin():
                open_jobs = JobRecord.state.in_(("queued", "retry_wait"))
                session.execute(update(JobRecord)
                                .where(JobRecord.workspace_id.not_in(workspace_ids), open_jobs)
                                .values(next_attempt_at=utcnow() + timedelta(days=1)))
                session.execute(update(JobRecord)  # an earlier drain may have deferred them
                                .where(JobRecord.workspace_id.in_(workspace_ids), JobRecord.state == "queued")
                                .values(next_attempt_at=None))
            with self._as(WORKER):
                if not asyncio.run(self.runtime.run_once()):
                    return handled
            handled += 1
        self.fail("the Worker did not go idle")

    # -- reading back -------------------------------------------------------

    def _jobs(self, workspace_id: str) -> list[JobRecord]:
        with Session(self.owner) as session:
            rows = list(session.scalars(select(JobRecord).where(
                JobRecord.workspace_id == workspace_id, JobRecord.type == JOB_TYPE).order_by(JobRecord.created_at)))
            for row in rows:
                session.expunge(row)
            return rows

    def _rows(self, ctx: TenantContext) -> list[UsageEventRecord]:
        """What the API sees of the Workspace's storage_bytes (the Worker cannot read)."""
        with self._as(API), tenant_session(ctx) as session:
            rows = list(session.scalars(select(UsageEventRecord)
                                        .where(UsageEventRecord.metric_key == "storage_bytes")
                                        .order_by(UsageEventRecord.occurred_at)))
            for row in rows:
                session.expunge(row)
            return rows

    def _listed(self) -> set[str]:
        with self.worker.connect() as connection:
            return set(connection.scalars(text(
                f"SELECT workspace_id FROM public.marketai_storage_usage_workspaces_v1("
                f"CAST(:day AS date), 500)"), {"day": self._today()}))

    # -- the narrow function ------------------------------------------------

    def test_only_the_worker_may_execute_the_function(self):
        with self.owner.connect() as connection:
            definer, config, volatility, result = connection.execute(text(
                "SELECT p.prosecdef, p.proconfig, p.provolatile, pg_get_function_result(p.oid) "
                "  FROM pg_proc p WHERE p.oid = CAST(:signature AS regprocedure)"
            ), {"signature": f"public.{FUNCTION}"}).one()
            self.assertEqual((definer, config, volatility, result), (
                True, ['search_path=""'], "s",
                "TABLE(workspace_id character varying, authorization_epoch integer)"))
            public = connection.scalar(text(
                "SELECT coalesce(bool_or(acl.privilege_type = 'EXECUTE'), false) "
                "  FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl "
                " WHERE p.oid = CAST(:signature AS regprocedure) AND acl.grantee = 0"
            ), {"signature": f"public.{FUNCTION}"})
            self.assertFalse(public, "PUBLIC may not execute it")
            for role, allowed in (("marketai_domain_api", False), ("marketai_domain_readonly", False),
                                  ("marketai_domain_worker", True)):
                with self.subTest(role=role):
                    self.assertEqual(connection.scalar(text(
                        "SELECT has_function_privilege(CAST(:role AS text), "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ), {"role": role, "signature": f"public.{FUNCTION}"}), allowed)
        engines = [self.api]
        if os.getenv("DOMAIN_READONLY_DATABASE_URL"):
            engines.append(create_engine(os.environ["DOMAIN_READONLY_DATABASE_URL"]))
            self.addCleanup(engines[-1].dispose)
        for engine in engines:
            with self.subTest(role=engine.url.username), engine.connect() as connection, \
                 self.assertRaisesRegex(Exception, "permission denied"):
                connection.execute(text(
                    "SELECT * FROM public.marketai_storage_usage_workspaces_v1(CURRENT_DATE, 1)"))

    # -- the main path ------------------------------------------------------

    def test_the_worker_records_each_workspace_s_bytes_once_a_day(self):
        a = self._workspace("a")
        b = self._workspace("b")
        held = self._workspace("held", suspended=True)
        shared = f"committed/{a.workspace_id}/derivatives/shared.jpg"
        self._reference(a.workspace_id, 1_000, f"committed/{a.workspace_id}/reference_asset/one.png",
                        derivative=(shared, 200))
        self._reference(a.workspace_id, 300, f"committed/{a.workspace_id}/reference_asset/two.png",
                        derivative=(shared, 200))  # one derivative object, named twice
        self._package(a, 5_000, f"committed/{a.workspace_id}/export/package.zip")
        self._reference(b.workspace_id, 777, f"committed/{b.workspace_id}/reference_asset/b.png")

        self._dispatch(a.workspace_id, b.workspace_id)
        today = self._today()
        for ctx in (a, b):
            with self.subTest(workspace=ctx.workspace_id):
                job, = self._jobs(ctx.workspace_id)
                self.assertEqual((job.state, job.idempotency_key, job.payload_json, job.authorization_epoch),
                                 ("queued", storage_usage_job_key(today, 0), {"usage_date": today.isoformat()}, 0))
        self.assertEqual(self._jobs(held.workspace_id), [], "a suspended Workspace is not listed")
        self.assertFalse({a.workspace_id, b.workspace_id, held.workspace_id} & self._listed(),
                         "today's Jobs exist, and the suspended Workspace is never listed")

        self.assertEqual(self._drain(a.workspace_id, b.workspace_id), 2)
        for ctx, expected in ((a, 1_000 + 200 + 300 + 5_000), (b, 777)):
            with self.subTest(workspace=ctx.workspace_id):
                self.assertEqual([job.state for job in self._jobs(ctx.workspace_id)], ["succeeded"])
                row, = self._rows(ctx)
                self.assertEqual(
                    (row.measurement_kind, row.direction, row.unit, row.execution_mode,
                     row.source_resource_type, row.source_resource_id, Decimal(row.quantity)),
                    ("gauge", "set", "byte", "not_applicable", "workspace", ctx.workspace_id, Decimal(expected)))
                self.assertEqual(row.occurred_at.date(), today)

        self._dispatch()  # the next interval, the same day
        self.assertEqual(self._drain(a.workspace_id, b.workspace_id), 0)
        self.assertEqual(len(self._jobs(a.workspace_id)), 1)

        # A replayed measurement, as the Worker, is a counted duplicate.
        context = system_context(a.workspace_id, initiator_id=None, authorization_epoch=0)
        self._reference(a.workspace_id, 50, f"committed/{a.workspace_id}/reference_asset/later.png")
        with self._as(WORKER), tenant_session(context) as session:
            replay = record_storage_bytes(session, context)
        self.assertEqual((replay.written, replay.duplicates), (0, 1))
        self.assertEqual([Decimal(row.quantity) for row in self._rows(a)], [Decimal("6500")])

    def test_workspace_a_s_job_never_reads_workspace_b_s_bytes(self):
        a = self._workspace("iso_a")
        b = self._workspace("iso_b")
        same_key = "committed/shared-name/reference_asset/same.png"
        self._reference(a.workspace_id, 100, same_key)
        self._reference(b.workspace_id, 9_000, same_key)
        self._package(b, 5_000, f"committed/{b.workspace_id}/export/b.zip")

        a_worker = system_context(a.workspace_id, initiator_id=None, authorization_epoch=0)
        b_worker = system_context(b.workspace_id, initiator_id=None, authorization_epoch=0)
        with self._as(WORKER), tenant_session(a_worker) as session:
            self.assertEqual(workspace_storage_bytes(session, a_worker), 100)
            # Inside A's context, even a sum asked for B's id finds none of B's rows.
            self.assertEqual(workspace_storage_bytes(session, b_worker), 0)
            self.assertEqual(session.scalars(select(ReferenceObjectRecord.workspace_id)).all(), [a.workspace_id])
            self.assertEqual(session.scalars(select(ExportPackageRecord.id)).all(), [])

        self._dispatch(a.workspace_id, b.workspace_id)
        self.assertEqual(self._drain(a.workspace_id), 1)  # A's Job alone
        self.assertEqual([Decimal(row.quantity) for row in self._rows(a)], [Decimal("100")])
        self.assertEqual(self._rows(b), [])
        self.assertEqual(self._drain(b.workspace_id), 1)
        self.assertEqual([Decimal(row.quantity) for row in self._rows(b)], [Decimal("14000")])

    def test_a_job_rejected_for_a_stale_epoch_is_dispatched_again_under_the_new_epoch(self):
        a = self._workspace("epoch")
        self._reference(a.workspace_id, 4_321, f"committed/{a.workspace_id}/reference_asset/r.png")
        self._dispatch(a.workspace_id)
        with Session(self.owner) as session, session.begin():  # a membership change, say
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == a.workspace_id)
                            .values(authorization_epoch=WorkspaceRecord.authorization_epoch + 1))
        self.assertEqual(self._drain(a.workspace_id), 0, "the claim rejects the stale Job")
        stale, = self._jobs(a.workspace_id)
        self.assertEqual((stale.state, stale.error_code), ("failed", "authorization_epoch_stale"))
        self.assertIn(a.workspace_id, self._listed(), "a rejected Job does not hide the Workspace")

        self._dispatch(a.workspace_id)
        self.assertEqual(len(self._jobs(a.workspace_id)), 2)
        self.assertEqual(self._drain(a.workspace_id), 1)
        fresh = self._jobs(a.workspace_id)[1]
        self.assertEqual((fresh.state, fresh.idempotency_key, fresh.authorization_epoch),
                         ("succeeded", storage_usage_job_key(self._today(), 1), 1))
        current = replace(a, authorization_epoch=1)  # RLS shows nothing to the stale epoch
        self.assertEqual([Decimal(row.quantity) for row in self._rows(current)], [Decimal("4321")])

    # -- reading ------------------------------------------------------------

    def test_get_usage_returns_the_latest_storage_bytes_under_the_api_role(self):
        owner = self._workspace("read")
        member = TenantContext(workspace_id=owner.workspace_id, role=ROLE_MEMBER, initiator_id="usr_acme_member",
                               request_id="test-req", trace_id="trace-test")
        empty = self._workspace("read_empty")
        worker = system_context(owner.workspace_id, initiator_id=None, authorization_epoch=0)
        day = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
        for days_ago, size in ((3, 100), (2, 50), (1, 70)):
            self._reference(owner.workspace_id, size, f"committed/{owner.workspace_id}/r-{days_ago}.png")
            with self._as(WORKER), tenant_session(worker) as session:
                record_storage_bytes(session, worker, measured_at=day - timedelta(days=days_ago))
        self.assertEqual([Decimal(r.quantity) for r in self._rows(owner)], [100, 150, 220])

        start = (day - timedelta(days=10)).isoformat()
        for caller in (owner, member):
            for end, expected in ((day.isoformat(), "220"), ((day - timedelta(days=1, hours=1)).isoformat(), "150")):
                with self.subTest(role=caller.role, end=end):
                    self.current = caller
                    with self._as(API):
                        response = self.client.get("/api/v1/usage", params={
                            "metric_key": "storage_bytes", "window_start": start, "window_end": end})
                    self.assertEqual(response.status_code, 200, response.text)
                    body = response.json()
                    self.assertEqual(Decimal(body["total"]), Decimal(expected))
                    self.assertEqual({k: Decimal(v) for k, v in body["by_execution_mode"].items()},
                                     {"byok": 0, "sandbox": 0, "mock": 0, "not_applicable": Decimal(expected)})
        self.current = empty
        with self._as(API):
            response = self.client.get("/api/v1/usage", params={"metric_key": "storage_bytes"})
        self.assertEqual((response.status_code, response.json()["total"]), (200, "0"))

    # -- the migration ------------------------------------------------------

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[OWNER]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _function_exists(self) -> bool:
        with self.owner.connect() as connection:
            return connection.scalar(text("SELECT to_regprocedure(:signature) IS NOT NULL"),
                                     {"signature": f"public.{FUNCTION}"})

    def test_the_migration_drops_the_function_and_restores_it_with_its_grant(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertFalse(self._function_exists())
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertTrue(self._function_exists())
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)
        self.test_only_the_worker_may_execute_the_function()


if __name__ == "__main__":
    unittest.main()
