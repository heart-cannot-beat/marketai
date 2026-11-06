"""Issue #347 against an isolated migrated PostgreSQL with the real API, Worker and Readonly roles.

``GET /api/v1/usage`` (``get_usage``) answered 500 ``permission denied for
table usage_events``: ``marketai_domain_api`` held INSERT on the table but no
SELECT, and the only endpoint test mocked ``UsageQuery`` away. Here the
endpoint runs as the role that serves it in production:

* usage events are recorded through ``UsageRecorder`` as the Worker and as
  the API, the two roles holding INSERT, never inserted as rows;
* a Member and an Owner get 200 and the sum of one metric over
  ``[window_start, window_end)``: other metrics, events outside the window and
  another Workspace's events are left out;
* without parameters it sums ``provider_call`` over the last 24 hours;
* another Workspace sees its own events only, and one with none gets ``"0"``;
* the API role without a tenant context sees no row.

Then the privilege matrix of ``usage_events`` (the API reads and appends, the
Worker only appends, Readonly only reads, nobody updates or deletes) and the
migration's upgrade, downgrade and upgrade again.

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
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.usage import UsageRecorder
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
METRIC = "generation_image"


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

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    def _workspace(self, prefix: str, role: str, actor: str) -> TenantContext:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with self._as(0), platform_session() as session:  # the control plane opens Workspaces
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _as_role(self, ctx: TenantContext, role: str, actor: str) -> TenantContext:
        return TenantContext(workspace_id=ctx.workspace_id, role=role, initiator_id=actor,
                             request_id="test-req", trace_id="trace-test")

    def _record(self, index: int, ctx: TenantContext, metric_key: str, quantity: str, occurred_at: datetime) -> None:
        """Record one event through the domain, as the API (1) or the Worker (2)."""
        with self._as(index), tenant_session(ctx) as session:
            UsageRecorder().record(
                session, ctx,
                metric_key=metric_key, quantity=Decimal(quantity), unit="count",
                execution_mode="production", source_resource_type="test",
                source_resource_id=f"src_{uuid.uuid4().hex[:8]}",
                semantic_key_hash=uuid.uuid4().hex, occurred_at=occurred_at,
            )

    def _usage(self, caller: TenantContext, params: dict | None = None) -> dict:
        self.current = caller
        with self._as(1):
            response = self.client.get("/api/v1/usage", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_get_usage_sums_one_workspace_s_metric_in_the_window_under_the_api_role(self):
        member = self._workspace("ws_usage_pg", ROLE_MEMBER, "member_a")
        owner = self._as_role(member, ROLE_OWNER, "owner_a")
        stranger = self._workspace("ws_usage_pg_other", ROLE_MEMBER, "member_b")
        empty = self._workspace("ws_usage_pg_empty", ROLE_OWNER, "owner_c")

        now = datetime.now(timezone.utc).replace(microsecond=0)
        start, end = now - timedelta(hours=6), now - timedelta(hours=3)
        for index, metric_key, quantity, occurred_at in (
            (2, METRIC, "2", start),                               # the start is inside
            (2, METRIC, "3.5", start + timedelta(hours=1)),
            (1, METRIC, "0.25", end - timedelta(seconds=1)),       # the API records too
            (2, METRIC, "100", end),                               # the end is outside
            (2, METRIC, "1000", start - timedelta(seconds=1)),
            (2, "llm_output_tokens", "10000", start + timedelta(hours=1)),
            (2, "provider_call", "1", now - timedelta(hours=1)),   # inside the default window
            (2, "provider_call", "50", now - timedelta(hours=25)),
        ):
            self._record(index, member, metric_key, quantity, occurred_at)
        self._record(2, stranger, METRIC, "7", start + timedelta(hours=1))

        window = {"metric_key": METRIC, "window_start": start.isoformat(), "window_end": end.isoformat()}
        for caller in (member, owner):
            with self.subTest(role=caller.role):
                body = self._usage(caller, window)
                self.assertEqual(
                    (body["workspace_id"], body["metric_key"], body["window_start"], body["window_end"]),
                    (member.workspace_id, METRIC, start.isoformat(), end.isoformat()))
                self.assertEqual(Decimal(body["total"]), Decimal("5.75"))

                defaults = self._usage(caller)
                self.assertEqual(defaults["metric_key"], "provider_call")
                self.assertEqual(Decimal(defaults["total"]), Decimal("1"))
                self.assertEqual(datetime.fromisoformat(defaults["window_end"])
                                 - datetime.fromisoformat(defaults["window_start"]), timedelta(hours=24))

        other = self._usage(stranger, window)
        self.assertEqual((other["workspace_id"], Decimal(other["total"])), (stranger.workspace_id, Decimal("7")))
        self.assertEqual(self._usage(empty, window)["total"], "0")
        self.assertEqual(self._usage(empty)["total"], "0")

        with self.api.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM usage_events")).scalar(), 0)

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
