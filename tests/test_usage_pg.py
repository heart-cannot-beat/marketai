"""Usage against an isolated migrated PostgreSQL with the real API, Worker and Readonly roles.

Issue #347: ``GET /api/v1/usage`` (``get_usage``) answered 500 ``permission
denied for table usage_events`` because ``marketai_domain_api`` held INSERT
on the table but no SELECT. Issue #8 (slice S1) adds the writer: provider
calls are recorded with ``INSERT ... ON CONFLICT DO NOTHING`` in the
settlement's own transaction, as the Worker or the API.

Here, as the roles that run in production:

* ``record_many`` writes a fact as the Worker, which holds INSERT but no
  SELECT, and as the API; writing the same key again is a counted duplicate,
  never an error, so the settlement around it still commits. Its
  ``ON CONFLICT DO NOTHING`` names no target: with one, PostgreSQL wants
  SELECT on the target's columns and refuses the Worker; without one, any
  unique key would be skipped, so the table keeps exactly two;
* inside Workspace A's context, a row for Workspace B is refused by RLS,
  with or without ``ON CONFLICT``;
* UPDATE and DELETE are refused by the append-only trigger, even for the
  owner that may bypass RLS;
* a Member and an Owner get 200 and one Workspace's ``provider_calls`` over
  ``[window_start, window_end)``, bucketed by execution mode, with Mock out
  of ``total``; without parameters it is ``provider_calls`` over the last
  24 hours; another Workspace's events stay out; an empty Workspace gets
  ``"0"``; a request with no verified identity gets 401;
* the API role without a tenant context sees no row.

Then the privilege matrix of ``usage_events`` (the API reads and appends, the
Worker only appends, Readonly only reads, nobody updates or deletes) and the
migration's upgrade, downgrade and upgrade again.

Every Workspace a test opens, and its usage rows, are removed afterwards by
the owner with the append-only trigger disabled for that one transaction.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app import config
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.usage import UsageFact, UsageRecorder, record_provider_call, usage_semantic_key
from app.entitlements.usage_models import UsageEventRecord
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
ROOT = Path(__file__).resolve().parents[1]
#: The migration that grants the API SELECT (#347), and the head it follows.
REVISION, DOWN_REVISION = "zz7a8b9c0d1e", "zz6f7a8b9c0d"
#: role -> privileges it must hold on usage_events; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {"SELECT", "INSERT"},
    "marketai_domain_worker": {"INSERT"},
    "marketai_domain_readonly": {"SELECT"},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
OWNER, API, WORKER = 0, 1, 2
ROLE_NAMES = {API: "api", WORKER: "worker"}
METRIC = "provider_calls"


def fact(source_id: str, mode: str = "byok", occurred_at: datetime | None = None) -> UsageFact:
    return UsageFact(
        metric_key=METRIC, quantity=Decimal("1"), unit="call", execution_mode=mode,
        semantic_key_hash=usage_semantic_key(METRIC, "provider_call_attempt", source_id),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        source_resource_type="provider_call_attempt", source_resource_id=source_id,
    )


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class UsagePostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False)
                         for e in (self.owner, self.api, self.worker)]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.workspaces: list[str] = []
        self.addCleanup(self._remove_workspaces)

    def _remove_workspaces(self) -> None:
        """Leave the shared cluster as it was: the later CI steps count rows."""
        if not self.workspaces:
            return
        with self.owner.begin() as connection:
            connection.execute(text("ALTER TABLE usage_events DISABLE TRIGGER trg_usage_events_immutable"))
            connection.execute(text("DELETE FROM usage_events WHERE workspace_id = ANY(:ids)"),
                               {"ids": self.workspaces})
            connection.execute(text("ALTER TABLE usage_events ENABLE TRIGGER trg_usage_events_immutable"))
            connection.execute(text("DELETE FROM workspaces WHERE id = ANY(:ids)"), {"ids": self.workspaces})

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    def _workspace(self, prefix: str, role: str, actor: str) -> TenantContext:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with self._as(OWNER), platform_session() as session:  # the control plane opens Workspaces
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _as_role(self, ctx: TenantContext, role: str, actor: str) -> TenantContext:
        return TenantContext(workspace_id=ctx.workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _record(self, index: int, ctx: TenantContext, *facts: UsageFact):
        """Record through the domain, as the API or the Worker, in a transaction of its own."""
        with self._as(index), tenant_session(ctx) as session:
            return UsageRecorder().record_many(session, ctx, facts)

    def _rows(self, ctx: TenantContext) -> list[tuple]:
        """What the API role sees of the Workspace (the Worker cannot read)."""
        with self._as(API), tenant_session(ctx) as session:
            return [(r.source_resource_id, r.execution_mode, r.measurement_kind, r.unit)
                    for r in session.query(UsageEventRecord).order_by(UsageEventRecord.source_resource_id)]

    def _usage(self, caller: TenantContext, params: dict | None = None) -> dict:
        self.current = caller
        with self._as(API):
            response = self.client.get("/api/v1/usage", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_first_and_duplicate_writes_succeed_as_the_worker_and_the_api(self):
        ctx = self._workspace("ws_usage_pg_write", ROLE_OWNER, "owner_w")
        for index, name in ROLE_NAMES.items():
            with self.subTest(role=name):
                source = f"pca_{name}_{uuid.uuid4().hex[:8]}"
                first = self._record(index, ctx, fact(source))
                again = self._record(index, ctx, fact(source))
                batch = self._record(index, ctx, fact(source), fact(f"{source}_b"), fact(f"{source}_b"))
                self.assertEqual((first.written, first.duplicates), (1, 0))
                self.assertEqual((again.written, again.duplicates), (0, 1))
                self.assertEqual((batch.written, batch.duplicates), (1, 2))

                attempt = SimpleNamespace(id=f"{source}_c", workspace_id=ctx.workspace_id,
                                          execution_mode="controlled_mock")
                with self._as(index), tenant_session(ctx) as session:
                    self.assertEqual(record_provider_call(session, ctx, attempt, occurred_at=datetime.now(timezone.utc)).written, 1)
                    self.assertEqual(record_provider_call(session, ctx, attempt, occurred_at=datetime.now(timezone.utc)).duplicates, 1)
        rows = self._rows(ctx)
        self.assertEqual(len(rows), 6)  # three keys per role, each once
        self.assertEqual({(kind, unit) for _, _, kind, unit in rows}, {("delta", "call")})
        self.assertEqual(sorted(mode for _, mode, _, _ in rows), ["byok"] * 4 + ["mock"] * 2)

    def test_a_named_conflict_target_would_need_select_the_worker_does_not_hold(self):
        """Why ``record_many`` names no conflict target: PostgreSQL wants SELECT on its columns."""
        ctx = self._workspace("ws_usage_pg_target", ROLE_OWNER, "owner_t")
        now = datetime.now(timezone.utc)
        statement = postgresql.insert(UsageEventRecord.__table__).values(
            id=f"ue_{uuid.uuid4().hex}", workspace_id=ctx.workspace_id, metric_key=METRIC,
            measurement_kind="delta", direction="increase", quantity=Decimal("1"), unit="call",
            execution_mode="byok", semantic_key_hash=uuid.uuid4().hex * 2, occurred_at=now, created_at=now,
        ).on_conflict_do_nothing(index_elements=["workspace_id", "semantic_key_hash"])
        with self.assertRaises(DBAPIError) as caught:
            with self._as(WORKER), tenant_session(ctx) as session:
                session.execute(statement)
        self.assertIn("permission denied for table usage_events", str(caught.exception))

    def test_the_only_unique_keys_are_the_id_and_the_semantic_key(self):
        """``ON CONFLICT DO NOTHING`` without a target skips any unique violation: keep it to these two."""
        with self.owner.connect() as connection:
            keys = connection.execute(text(
                "SELECT array_agg(a.attname::text ORDER BY k.ordinality) FROM pg_index i "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality) "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = 'usage_events'::regclass AND i.indisunique GROUP BY i.indexrelid"
            )).scalars().all()
        self.assertEqual(sorted(tuple(key) for key in keys), [("id",), ("workspace_id", "semantic_key_hash")])

    def test_rls_refuses_a_row_for_another_workspace(self):
        alpha = self._workspace("ws_usage_pg_alpha", ROLE_OWNER, "owner_a")
        beta = self._workspace("ws_usage_pg_beta", ROLE_OWNER, "owner_b")
        now = datetime.now(timezone.utc)
        for index, name in ROLE_NAMES.items():
            for on_conflict in (False, True):
                with self.subTest(role=name, on_conflict=on_conflict):
                    statement = postgresql.insert(UsageEventRecord.__table__).values(
                        id=f"ue_{uuid.uuid4().hex}", workspace_id=beta.workspace_id, metric_key=METRIC,
                        measurement_kind="delta", direction="increase", quantity=Decimal("1"), unit="call",
                        execution_mode="byok", semantic_key_hash=uuid.uuid4().hex * 2,
                        occurred_at=now, created_at=now,
                    )
                    if on_conflict:  # the shape record_many sends
                        statement = statement.on_conflict_do_nothing()
                    with self.assertRaises(DBAPIError) as caught:
                        with self._as(index), tenant_session(alpha) as session:
                            session.execute(statement)
                    self.assertIn("row-level security", str(caught.exception))
        self.assertEqual(self._rows(alpha), [])
        self.assertEqual(self._rows(beta), [])

    def test_update_and_delete_are_refused_by_the_append_only_trigger(self):
        ctx = self._workspace("ws_usage_pg_append", ROLE_OWNER, "owner_t")
        self._record(WORKER, ctx, fact("pca_append"))
        for statement in ("UPDATE usage_events SET quantity = 2 WHERE workspace_id = :ws",
                          "DELETE FROM usage_events WHERE workspace_id = :ws"):
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(DBAPIError) as caught:
                    with self.owner.begin() as connection:  # the owner may bypass RLS, not the trigger
                        connection.execute(text(statement), {"ws": ctx.workspace_id})
                self.assertIn("usage_events is append-only", str(caught.exception))
        self.assertEqual([source for source, *_ in self._rows(ctx)], ["pca_append"])

    def test_get_usage_buckets_one_workspace_s_provider_calls_under_the_api_role(self):
        member = self._workspace("ws_usage_pg", ROLE_MEMBER, "member_a")
        owner = self._as_role(member, ROLE_OWNER, "owner_a")
        stranger = self._workspace("ws_usage_pg_other", ROLE_MEMBER, "member_b")
        empty = self._workspace("ws_usage_pg_empty", ROLE_OWNER, "owner_c")

        now = datetime.now(timezone.utc).replace(microsecond=0)
        start, end = now - timedelta(hours=6), now - timedelta(hours=3)
        for index, source, mode, occurred_at in (
            (WORKER, "start", "byok", start),                          # the start is inside
            (WORKER, "sandbox", "sandbox", start + timedelta(hours=1)),
            (API, "late", "byok", end - timedelta(seconds=1)),         # the API records too
            (WORKER, "mock", "mock", start + timedelta(hours=1)),      # bucketed, never in total
            (WORKER, "end", "byok", end),                              # the end is outside
            (WORKER, "before", "byok", start - timedelta(seconds=1)),
            (WORKER, "recent", "byok", now - timedelta(hours=1)),
            (WORKER, "recent_mock", "mock", now - timedelta(hours=2)),
            (WORKER, "old", "byok", now - timedelta(hours=25)),        # before the default 24 hours
        ):
            self._record(index, member, fact(source, mode, occurred_at))
        self._record(WORKER, stranger, fact("stranger", "byok", start + timedelta(hours=1)))

        window = {"metric_key": METRIC, "window_start": start.isoformat(), "window_end": end.isoformat()}
        for caller in (member, owner):
            with self.subTest(role=caller.role):
                body = self._usage(caller, window)
                self.assertEqual(
                    (body["workspace_id"], body["metric_key"], body["window_start"], body["window_end"]),
                    (member.workspace_id, METRIC, start.isoformat(), end.isoformat()))
                self.assertEqual({k: Decimal(v) for k, v in body["by_execution_mode"].items()},
                                 {"byok": 2, "sandbox": 1, "mock": 1, "not_applicable": 0})
                self.assertEqual(Decimal(body["total"]), Decimal("3"))

                defaults = self._usage(caller)  # every row but "old" is in the last 24 hours
                self.assertEqual(defaults["metric_key"], METRIC)
                self.assertEqual({k: Decimal(v) for k, v in defaults["by_execution_mode"].items()},
                                 {"byok": 5, "sandbox": 1, "mock": 2, "not_applicable": 0})
                self.assertEqual(Decimal(defaults["total"]), Decimal("6"))
                self.assertEqual(datetime.fromisoformat(defaults["window_end"])
                                 - datetime.fromisoformat(defaults["window_start"]), timedelta(hours=24))

        other = self._usage(stranger, window)
        self.assertEqual((other["workspace_id"], Decimal(other["total"])), (stranger.workspace_id, Decimal("1")))
        self.assertEqual(self._usage(empty, window)["total"], "0")
        self.assertEqual(set(self._usage(empty)["by_execution_mode"].values()), {"0"})

        with self.api.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM usage_events")).scalar(), 0)

        app.dependency_overrides.pop(request_tenant_context, None)  # no verified identity at all
        with self._as(API), patch.object(config, "LOCAL_IDENTITY_ENABLED", False):
            self.assertEqual(self.client.get("/api/v1/usage").status_code, 401)

    def test_the_privilege_matrix_of_usage_events_holds(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'usage_events'"
            )).scalar()
            self.assertTrue(forced)
            for role, expected in GRANTED.items():
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, 'usage_events', :p)"),
                                              {"r": role, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege} usage_events")

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[0]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _api_selects(self) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text(
                "SELECT has_table_privilege('marketai_domain_api', 'usage_events', 'SELECT')")).scalar()

    def test_the_migration_grants_select_and_its_downgrade_revokes_it(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertFalse(self._api_selects())
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertTrue(self._api_selects())
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)
        self.test_the_privilege_matrix_of_usage_events_holds()


if __name__ == "__main__":
    unittest.main()
