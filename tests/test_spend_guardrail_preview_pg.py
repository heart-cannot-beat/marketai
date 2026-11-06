"""Issue #386 against an isolated migrated PostgreSQL with the real API, Worker and Readonly roles.

A spend guardrail preview used to live in one API process's memory, keyed by
a token ``confirm_spend_guardrail`` never checked against the Workspace or the
revision. Here both routes run as the role that serves them in production:

* a preview survives a restart (a new engine, so no connection or process
  state carries over) and confirms once; a second confirm is refused;
* another Workspace, with a guardrail of its own at the same revision, can
  neither confirm, see, consume nor forge a preview: RLS hides the row and
  its ``WITH CHECK`` refuses the insert;
* the API can consume a preview but rewrite nothing else about it, and
  delete nothing;
* two confirms racing on one revision, or on one preview, let exactly one
  change through.

Then the privilege matrix of ``spend_guardrail_previews`` (the API reads,
inserts and updates ``consumed_at`` and ``result_revision`` only, the Worker
nothing, Readonly only reads, nobody deletes) and the migration's upgrade,
downgrade and upgrade again.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.guardrail_admin import GuardrailAdmin
from app.library.contracts import ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
ROOT = Path(__file__).resolve().parents[1]
TABLE = "spend_guardrail_previews"
#: The migration that adds the table (#386), and the revision it follows.
REVISION, DOWN_REVISION = "zz8b9c0d1e2f", "zz7a8b9c0d1e"
#: role -> privileges it must hold on the table; every other DML privilege must be absent.
GRANTED = {
    "marketai_domain_api": {"SELECT", "INSERT"},
    "marketai_domain_worker": set(),
    "marketai_domain_readonly": {"SELECT"},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
#: The only columns the API may update: confirm consumes a preview.
CONSUME_COLUMNS = {"consumed_at", "result_revision"}
COLUMNS = (
    "id", "workspace_id", "creator_id", "base_revision", "per_operation_limit",
    "rolling_24h_limit", "period_limit", "outstanding_exposure_limit",
    "created_at", "expires_at", "consumed_at", "result_revision",
)
LOWER = {
    "per_operation_limit": "0.50",
    "rolling_24h_limit": "2.00",
    "period_limit": "10.00",
    "outstanding_exposure_limit": "1.00",
}
HIGHER = {
    "per_operation_limit": "4.00",
    "rolling_24h_limit": "15.00",
    "period_limit": "80.00",
    "outstanding_exposure_limit": "8.00",
}
_BARRIER_TIMEOUT = 30


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class SpendGuardrailPreviewPostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api = create_engine(URLS[0]), create_engine(URLS[1])
        for engine in (self.owner, self.api):
            self.addCleanup(engine.dispose)
        self.owner_sessions = sessionmaker(bind=self.owner, expire_on_commit=False, autoflush=False)
        self.api_sessions = sessionmaker(bind=self.api, expire_on_commit=False, autoflush=False)
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _as_owner(self):
        return patch("app.adapters.persistence.database.SessionLocal", self.owner_sessions)

    def _as_api(self):
        return patch("app.adapters.persistence.database.SessionLocal", self.api_sessions)

    def _workspace(self, prefix: str, actor: str) -> TenantContext:
        """A Workspace with its guardrail at revision 1; the control plane and
        provisioning write these, so the owner does."""
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc)
        with self._as_owner(), platform_session() as session:
            session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))
            session.flush()
            session.add(SpendGuardrailRecord(
                workspace_id=workspace_id, revision=1, budget_currency="USD",
                per_operation_limit=Decimal("1.00"), rolling_24h_limit=Decimal("5.00"),
                period_limit=Decimal("20.00"), outstanding_exposure_limit=Decimal("3.00"),
                platform_policy_version=1, created_at=now, updated_at=now, updated_by="seed",
            ))
        return TenantContext(workspace_id=workspace_id, role=ROLE_OWNER, initiator_id=actor,
                             initiator_role=ROLE_OWNER, request_id="test-req", trace_id="trace-test")

    def _post(self, caller: TenantContext, path: str, body: dict):
        self.current = caller
        with self._as_api():
            return self.client.post(f"/api/v1/spend-guardrail/{path}", json=body)

    def _preview(self, caller: TenantContext, limits: dict = LOWER) -> str:
        response = self._post(caller, "preview", {**limits, "expected_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["preview_token"]

    def _confirm(self, caller: TenantContext, token: str, revision: int = 1):
        return self._post(caller, "confirm", {"preview_token": token, "expected_revision": revision})

    def _refused(self, response, code: str) -> None:
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body["code"], code)
        self.assertTrue({"type", "title", "status", "code", "trace_id", "retryable"} <= body.keys(), body)

    def _state(self, ctx: TenantContext) -> tuple[int, Decimal, int]:
        """(guardrail revision, per-operation limit, number of guardrail events), read as the owner."""
        with self.owner.connect() as connection:
            revision, per_operation = connection.execute(text(
                "SELECT revision, per_operation_limit FROM spend_guardrails WHERE workspace_id = :w"
            ), {"w": ctx.workspace_id}).one()
            events = connection.execute(text(
                "SELECT count(*) FROM spend_guardrail_events WHERE workspace_id = :w"
            ), {"w": ctx.workspace_id}).scalar()
        return revision, per_operation, events

    def _consumption(self, token: str) -> tuple[bool, int | None]:
        with self.owner.connect() as connection:
            consumed_at, result_revision = connection.execute(text(
                f"SELECT consumed_at, result_revision FROM {TABLE} WHERE id = :id"
            ), {"id": token}).one()
        return consumed_at is not None, result_revision

    def test_a_preview_confirms_once_after_a_restart_under_the_api_role(self):
        ctx = self._workspace("ws_sgp_pg", "owner_a")
        token = self._preview(ctx)
        self.assertEqual(self._consumption(token), (False, None))

        # A restart: a new engine and client, nothing of the previewing ones.
        self.api.dispose()
        self.client.close()
        self.api = create_engine(URLS[1])
        self.addCleanup(self.api.dispose)
        self.api_sessions = sessionmaker(bind=self.api, expire_on_commit=False, autoflush=False)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

        confirmed = self._confirm(ctx, token)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["revision"], 2)
        self.assertEqual(self._state(ctx), (2, Decimal("0.5"), 1))
        self.assertEqual(self._consumption(token), (True, 2))

        for revision in (1, 2):
            with self.subTest(expected_revision=revision):
                self._refused(self._confirm(ctx, token, revision), "preview_already_consumed")
        self.assertEqual(self._state(ctx), (2, Decimal("0.5"), 1))

    def test_another_workspace_neither_sees_consumes_nor_forges_a_preview(self):
        ctx = self._workspace("ws_sgp_pg_a", "owner_a")
        stranger = self._workspace("ws_sgp_pg_b", "owner_b")
        token = self._preview(ctx, HIGHER)

        self._refused(self._confirm(stranger, token), "spend_guardrail_preview_missing")

        with self._as_api(), tenant_session(stranger) as session:
            self.assertEqual(session.execute(text(f"SELECT count(*) FROM {TABLE} WHERE id = :id"),
                                             {"id": token}).scalar(), 0)
            consumed = session.execute(text(
                f"UPDATE {TABLE} SET consumed_at = now(), result_revision = 2 WHERE id = :id"
            ), {"id": token})
            self.assertEqual(consumed.rowcount, 0)
        with self.assertRaisesRegex(DBAPIError, "row-level security"):
            with self._as_api(), tenant_session(stranger) as session:
                session.execute(text(
                    f"INSERT INTO {TABLE} (id, workspace_id, creator_id, base_revision, per_operation_limit, "
                    "rolling_24h_limit, period_limit, outstanding_exposure_limit, created_at, expires_at) "
                    "VALUES (:id, :w, 'owner_b', 1, 4, 15, 80, 8, now(), now() + interval '15 minutes')"
                ), {"id": f"sgp_{uuid.uuid4().hex}", "w": ctx.workspace_id})
        with self.api.connect() as connection:  # no tenant context at all
            self.assertEqual(connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar(), 0)

        self.assertEqual(self._state(stranger), (1, Decimal("1"), 0))
        self.assertEqual(self._state(ctx), (1, Decimal("1"), 0))
        self.assertEqual(self._consumption(token), (False, None))
        self.assertEqual(self._confirm(ctx, token).status_code, 200)
        self.assertEqual(self._state(ctx), (2, Decimal("4"), 1))

    def test_the_api_consumes_a_preview_but_rewrites_or_deletes_nothing(self):
        ctx = self._workspace("ws_sgp_pg_rw", "owner_a")
        token = self._preview(ctx)

        for statement in (
            f"UPDATE {TABLE} SET expires_at = now() + interval '1 day' WHERE id = :id",
            f"UPDATE {TABLE} SET per_operation_limit = 5 WHERE id = :id",
            f"UPDATE {TABLE} SET base_revision = 7 WHERE id = :id",
            f"UPDATE {TABLE} SET creator_id = 'owner_b' WHERE id = :id",
            f"DELETE FROM {TABLE} WHERE id = :id",
        ):
            with self.subTest(statement=statement.split(" WHERE")[0]):
                with self.assertRaisesRegex(DBAPIError, "permission denied"):
                    with self._as_api(), tenant_session(ctx) as session:
                        session.execute(text(statement), {"id": token})

        self.assertEqual(self._consumption(token), (False, None))
        self.assertEqual(self._confirm(ctx, token).status_code, 200)

    def _race(self, ctx: TenantContext, tokens: list[str]) -> list[object]:
        """Confirm each token in its own thread and API transaction, released together."""
        barrier = threading.Barrier(len(tokens))
        results: list[object] = [None] * len(tokens)

        def confirm(index: int) -> None:
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT)
                with tenant_session(ctx) as session:
                    results[index] = GuardrailAdmin().confirm_update(
                        session, ctx, preview_token=tokens[index], expected_revision=1,
                    )
            except Exception as exc:  # noqa: BLE001 -- captured for assertion
                results[index] = exc

        threads = [threading.Thread(target=confirm, args=(i,)) for i in range(len(tokens))]
        with self._as_api():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=_BARRIER_TIMEOUT * 2)
        return results

    def _one_through(self, results: list[object], refusal: str) -> None:
        successes = [r for r in results if isinstance(r, dict)]
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(failures), 1, results)
        self.assertEqual(getattr(failures[0], "code", None), refusal, repr(failures[0]))
        self.assertEqual(successes[0]["revision"], 2)

    def test_two_confirms_on_one_revision_let_exactly_one_change_through(self):
        ctx = self._workspace("ws_sgp_pg_race", "owner_a")
        tokens = [self._preview(ctx, LOWER), self._preview(ctx, HIGHER)]

        self._one_through(self._race(ctx, tokens), "revision_conflict")

        revision, per_operation, events = self._state(ctx)
        self.assertEqual((revision, events), (2, 1))
        self.assertIn(per_operation, (Decimal("0.5"), Decimal("4")))
        self.assertEqual(sorted(self._consumption(token) for token in tokens), [(False, None), (True, 2)])

    def test_two_confirms_of_one_preview_apply_it_once(self):
        ctx = self._workspace("ws_sgp_pg_twice", "owner_a")
        token = self._preview(ctx)

        self._one_through(self._race(ctx, [token, token]), "preview_already_consumed")

        self.assertEqual(self._state(ctx), (2, Decimal("0.5"), 1))
        self.assertEqual(self._consumption(token), (True, 2))

    def test_the_privilege_matrix_of_spend_guardrail_previews_holds(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = :t"
            ), {"t": TABLE}).scalar()
            self.assertTrue(forced)
            policies = connection.execute(text(
                "SELECT policyname FROM pg_policies WHERE tablename = :t"
            ), {"t": TABLE}).scalars().all()
            self.assertEqual(policies, ["tenant_isolation"])
            for role, expected in GRANTED.items():
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                              {"r": role, "t": TABLE, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege} {TABLE}")
                for column in COLUMNS:
                    held = connection.execute(text("SELECT has_column_privilege(:r, :t, :c, 'UPDATE')"),
                                              {"r": role, "t": TABLE, "c": column}).scalar()
                    allowed = role == "marketai_domain_api" and column in CONSUME_COLUMNS
                    self.assertEqual(held, allowed, f"{role} UPDATE ({column}) {TABLE}")

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[0]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _table_exists(self) -> bool:
        with self.owner.connect() as connection:
            return connection.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{TABLE}"}).scalar()

    def test_the_migration_creates_the_table_and_its_downgrade_drops_it(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertFalse(self._table_exists())
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertTrue(self._table_exists())
        self.test_the_privilege_matrix_of_spend_guardrail_previews_holds()
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)


if __name__ == "__main__":
    unittest.main()
