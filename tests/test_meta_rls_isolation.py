"""PostgreSQL-only tests for Meta-domain RLS enforcement (issue #23, Task 7).

These prove that ``marketai_meta_context_is_current(workspace_id, meta_mode)``
-- installed in ``m23a_meta_foundation`` and applied as the ``tenant_isolation``
policy on every Meta/Launch-domain table in ``m23b_meta_domain_tables`` --
actually enforces both isolation dimensions:

1. A session scoped to the right (workspace_id, meta_mode) sees its own rows.
2. A session scoped to the wrong workspace_id sees nothing.
3. A session scoped to the wrong meta_mode (same workspace) sees nothing --
   ``customer_sandbox`` and ``controlled_mock`` rows in the same Workspace
   must stay mutually invisible, since Sandbox-first tenant safety depends on
   fake evidence never being readable under a mode that expects real evidence.

This file intentionally does not use a ``pg_session`` pytest fixture -- no
such fixture exists in this repository (see ``tests/conftest.py``, which only
bootstraps an isolated SQLite environment for the whole suite). It instead
follows the pattern already established by ``tests/test_postgres_rls.py``:
connect as the real least-privilege roles, skip the whole module when the
cluster connection strings are not configured, so ``make verify`` still runs
clean on SQLite and CI supplies the PostgreSQL environment.
"""
from __future__ import annotations

import os
import unittest
import uuid

OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")
CONTEXT_SECRET = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET")

CONFIGURED = all((OWNER_URL, API_URL, CONTEXT_SECRET))

SKIP_REASON = (
    "set DOMAIN_OWNER_DATABASE_URL, DOMAIN_API_DATABASE_URL and "
    "MARKETAI_TENANT_CONTEXT_SECRET against a cluster migrated to head with "
    "deploy/postgres/domain-tenant-roles.sql applied"
)

INSERT_WORKSPACE = """
INSERT INTO workspaces (id, name, default_currency, lifecycle, budget_currency, created_at)
VALUES (:id, :id, 'USD', 'active', 'USD', now())
ON CONFLICT (id) DO NOTHING
"""

INSERT_CONNECTION = """
INSERT INTO meta_connection_records
    (id, workspace_id, meta_mode, ad_account_id, state, created_at, updated_at)
VALUES (:id, :workspace_id, :meta_mode, :ad_account_id, 'active', now(), now())
"""


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class MetaRlsIsolationTests(unittest.TestCase):
    """Row Level Security is the Meta-domain tenant *and* Meta-mode boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)

        cls.ws_a = f"ws_meta_a_{uuid.uuid4().hex[:8]}"
        cls.ws_b = f"ws_meta_b_{uuid.uuid4().hex[:8]}"

        cls.owner = create_engine(OWNER_URL, future=True)
        with cls.owner.begin() as conn:
            for workspace in (cls.ws_a, cls.ws_b):
                conn.execute(text(INSERT_WORKSPACE), {"id": workspace})

    @classmethod
    def tearDownClass(cls) -> None:
        from sqlalchemy import text

        with cls.owner.begin() as conn:
            conn.execute(
                text("DELETE FROM meta_connection_records WHERE workspace_id IN (:a, :b)"),
                {"a": cls.ws_a, "b": cls.ws_b},
            )
            conn.execute(
                text("DELETE FROM workspaces WHERE id IN (:a, :b)"),
                {"a": cls.ws_a, "b": cls.ws_b},
            )
        cls.owner.dispose()

    def engine(self, url: str):
        engine = self.create_engine(url, future=True)
        self.addCleanup(engine.dispose)
        return engine

    def scoped_connection(self, workspace: str, meta_mode: str | None):
        """An API-role connection inside a transaction carrying the scope."""
        conn = self.engine(API_URL).connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)
        self.set_scope(conn, workspace, meta_mode)
        return conn

    def set_scope(self, connection, workspace: str, meta_mode: str | None) -> None:
        from app.platform.tenancy import TenantContext, tenant_context_signature

        context = TenantContext(
            workspace_id=workspace,
            role="member",
            initiator_id="rls-meta-test",
            meta_mode=meta_mode,
        )
        connection.execute(
            self.text("SELECT set_config('marketai.workspace_id', :w, true)"),
            {"w": workspace},
        )
        connection.execute(
            self.text("SELECT set_config('marketai.authorization_epoch', '0', true)"),
        )
        connection.execute(
            self.text("SELECT set_config('marketai.meta_mode', :m, true)"),
            {"m": meta_mode or ""},
        )
        connection.execute(
            self.text(
                "SELECT set_config('marketai.context_signature', :signature, true)"
            ),
            {"signature": tenant_context_signature(context, CONTEXT_SECRET)},
        )

    def insert_connection(self, workspace: str, meta_mode: str) -> str:
        conn_id = f"mcon_{uuid.uuid4().hex[:8]}"
        with self.owner.begin() as conn:
            conn.execute(
                self.text(INSERT_CONNECTION),
                {
                    "id": conn_id,
                    "workspace_id": workspace,
                    "meta_mode": meta_mode,
                    "ad_account_id": "act_1",
                },
            )
        self.addCleanup(self._delete_connection, conn_id)
        return conn_id

    def _delete_connection(self, conn_id: str) -> None:
        with self.owner.begin() as conn:
            conn.execute(
                self.text("DELETE FROM meta_connection_records WHERE id = :id"),
                {"id": conn_id},
            )

    # ------------------------------------------------------------------ --

    def test_rls_allows_correct_workspace_and_mode(self):
        conn_id = self.insert_connection(self.ws_a, "customer_sandbox")

        scoped = self.scoped_connection(self.ws_a, "customer_sandbox")
        rows = [
            r[0]
            for r in scoped.execute(
                self.text("SELECT id FROM meta_connection_records WHERE id = :id"),
                {"id": conn_id},
            )
        ]

        self.assertEqual(rows, [conn_id])

    def test_rls_blocks_wrong_workspace(self):
        conn_id = self.insert_connection(self.ws_a, "customer_sandbox")

        scoped = self.scoped_connection(self.ws_b, "customer_sandbox")
        rows = [
            r[0]
            for r in scoped.execute(
                self.text("SELECT id FROM meta_connection_records WHERE id = :id"),
                {"id": conn_id},
            )
        ]

        self.assertEqual(rows, [])

    def test_rls_blocks_wrong_mode(self):
        """A session for customer_sandbox cannot see controlled_mock rows.

        Same Workspace, both modes real -- only the ``meta_mode`` differs.
        Sandbox-first tenant safety depends on this: fabricated
        ``controlled_mock`` fault-injection evidence must never leak into a
        ``customer_sandbox`` read, and vice versa.
        """
        conn_id = self.insert_connection(self.ws_a, "controlled_mock")

        scoped = self.scoped_connection(self.ws_a, "customer_sandbox")
        rows = [
            r[0]
            for r in scoped.execute(
                self.text("SELECT id FROM meta_connection_records WHERE id = :id"),
                {"id": conn_id},
            )
        ]

        self.assertEqual(rows, [])

    def test_rls_blocks_unscoped_mode(self):
        """An unset meta_mode fails closed, matching the unset-workspace case."""
        conn_id = self.insert_connection(self.ws_a, "customer_sandbox")

        scoped = self.scoped_connection(self.ws_a, None)
        rows = [
            r[0]
            for r in scoped.execute(
                self.text("SELECT id FROM meta_connection_records WHERE id = :id"),
                {"id": conn_id},
            )
        ]

        self.assertEqual(rows, [])

    def test_a_row_cannot_be_written_into_another_workspace(self):
        from sqlalchemy.exc import ProgrammingError

        scoped = self.scoped_connection(self.ws_a, "customer_sandbox")

        with self.assertRaises(ProgrammingError):
            scoped.execute(
                self.text(INSERT_CONNECTION),
                {
                    "id": f"mcon_{uuid.uuid4().hex[:8]}",
                    "workspace_id": self.ws_b,
                    "meta_mode": "customer_sandbox",
                    "ad_account_id": "act_1",
                },
            )

    def test_a_row_cannot_be_written_under_another_mode(self):
        """WITH CHECK also compares meta_mode, not just workspace_id."""
        from sqlalchemy.exc import ProgrammingError

        scoped = self.scoped_connection(self.ws_a, "customer_sandbox")

        with self.assertRaises(ProgrammingError):
            scoped.execute(
                self.text(INSERT_CONNECTION),
                {
                    "id": f"mcon_{uuid.uuid4().hex[:8]}",
                    "workspace_id": self.ws_a,
                    "meta_mode": "controlled_mock",
                    "ad_account_id": "act_1",
                },
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
