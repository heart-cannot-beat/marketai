"""WorkspaceAdmissionReader: the SECURITY DEFINER read wrapper (spec §3.3).

The PostgreSQL-only tests at the bottom of this file (`AdmissionFunctionPostgresTests`)
exercise the real `marketai_workspace_admission()` SECURITY DEFINER function
against a real cluster -- they do not use a `pg_session` pytest fixture. No
such fixture exists anywhere in this repository: `tests/test_scale_migration.py`
and `tests/test_meta_rls_isolation.py` both document that `pg_session`, as
referenced by `tests/test_meta_tenant_isolation.py`, "does not exist anywhere
in this repository ... and would error at fixture resolution if its tests
were ever un-skipped." Task 4's brief carried the same phantom fixture. These
tests instead follow the proven, already-working pattern in
`tests/test_postgres_rls.py`:
a `unittest.TestCase` gated by `unittest.skipUnless` on `DOMAIN_OWNER_DATABASE_URL`,
connecting with a plain SQLAlchemy engine. `tests/conftest.py` always skips
any `@pytest.mark.postgres`-marked test under `pytest` regardless of that env
var (SQLite-only local/CI-lite runs), so this suite -- like
`test_postgres_rls.py` -- is exercised in CI via `python -m unittest
tests.test_workspace_admission -v` with `DOMAIN_OWNER_DATABASE_URL` set, not
via a bare `pytest` invocation.
"""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import MagicMock

from app.platform.workspace_admission import (
    WorkspaceAdmission,
    WorkspaceAdmissionReader,
    WorkspaceNotFoundError,
)


def make_session(row: dict | None):
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    session.execute.return_value = result
    return session


class WorkspaceAdmissionReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_returns_admission_for_an_existing_workspace(self) -> None:
        session = make_session(
            {
                "workspace_id": "ws_1",
                "authorization_epoch": 3,
                "entitlement_version": 2,
                "suspended": False,
            }
        )
        reader = WorkspaceAdmissionReader(lambda: session)

        admission = await reader.read("ws_1")

        self.assertEqual(
            admission,
            WorkspaceAdmission(
                workspace_id="ws_1", authorization_epoch=3, entitlement_version=2, suspended=False
            ),
        )
        session.close.assert_called_once()

    async def test_read_includes_is_platform_workspace_when_the_row_carries_it(self) -> None:
        session = make_session(
            {
                "workspace_id": "ws_plat",
                "authorization_epoch": 0,
                "entitlement_version": 0,
                "suspended": False,
                "is_platform_workspace": True,
            }
        )
        reader = WorkspaceAdmissionReader(lambda: session)

        admission = await reader.read("ws_plat")

        self.assertTrue(admission.is_platform_workspace)

    async def test_read_defaults_is_platform_workspace_false_when_the_row_omits_it(self) -> None:
        # Backward compatibility: a row from a not-yet re-migrated
        # `marketai_workspace_admission()` (pre-`aa1_identity_kind`) has no
        # `is_platform_workspace` key at all.
        session = make_session(
            {
                "workspace_id": "ws_1",
                "authorization_epoch": 3,
                "entitlement_version": 2,
                "suspended": False,
            }
        )
        reader = WorkspaceAdmissionReader(lambda: session)

        admission = await reader.read("ws_1")

        self.assertFalse(admission.is_platform_workspace)

    async def test_read_raises_not_found_when_row_is_none(self) -> None:
        session = make_session(None)
        reader = WorkspaceAdmissionReader(lambda: session)

        with self.assertRaises(WorkspaceNotFoundError):
            await reader.read("ws_missing")
        session.close.assert_called_once()

    async def test_read_raises_not_found_when_function_returns_a_null_row(self) -> None:
        # marketai_workspace_admission always returns exactly one row (via
        # the VALUES(1) seed LEFT JOIN); a workspace that does not exist
        # comes back as a row of NULLs, not no row at all.
        session = make_session(
            {
                "workspace_id": None,
                "authorization_epoch": None,
                "entitlement_version": None,
                "suspended": None,
            }
        )
        reader = WorkspaceAdmissionReader(lambda: session)

        with self.assertRaises(WorkspaceNotFoundError):
            await reader.read("ws_missing")

    async def test_session_is_closed_even_when_the_query_raises(self) -> None:
        session = MagicMock()
        session.execute.side_effect = RuntimeError("connection lost")
        reader = WorkspaceAdmissionReader(lambda: session)

        with self.assertRaises(RuntimeError):
            await reader.read("ws_1")
        session.close.assert_called_once()


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")


@unittest.skipUnless(
    OWNER_URL,
    "set DOMAIN_OWNER_DATABASE_URL against a PostgreSQL cluster migrated to "
    "head (see tests/test_postgres_rls.py)",
)
class AdmissionFunctionPostgresTests(unittest.TestCase):
    """PO19: `marketai_workspace_admission()` returns `is_platform_workspace`.

    Connects as the schema owner (the function is `SECURITY DEFINER`, so a
    lower-privilege runtime role can call it too, but the owner connection is
    simplest here).
    Requires `alembic upgrade head` to already have run against `OWNER_URL`
    (`make db-release` in CI, per `.github/workflows/ci.yml`'s `domain-rls`
    job) -- this test does not migrate the database itself.
    """

    def setUp(self) -> None:
        from sqlalchemy import create_engine, text

        self._text = text
        self.engine = create_engine(OWNER_URL, future=True)
        self.addCleanup(self.engine.dispose)

    def _insert_workspace(self, workspace_id: str, *, is_platform_workspace: bool) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self._text(
                    "INSERT INTO workspaces "
                    "(id, name, default_currency, created_at, lifecycle, "
                    " budget_currency, is_platform_workspace) "
                    "VALUES (:id, :name, 'USD', now(), 'active', 'USD', :platform)"
                ),
                {"id": workspace_id, "name": workspace_id, "platform": is_platform_workspace},
            )

    def test_workspace_admission_includes_is_platform_workspace_true(self) -> None:
        workspace_id = "ws_plat_" + uuid.uuid4().hex[:12]
        self._insert_workspace(workspace_id, is_platform_workspace=True)

        with self.engine.connect() as conn:
            row = conn.execute(
                self._text("SELECT * FROM marketai_workspace_admission(:ws_id)"),
                {"ws_id": workspace_id},
            ).mappings().first()

        self.assertIsNotNone(row)
        self.assertEqual(row["workspace_id"], workspace_id)
        self.assertIs(row["is_platform_workspace"], True)

    def test_workspace_admission_ordinary_workspace_is_not_platform(self) -> None:
        workspace_id = "ws_ord_" + uuid.uuid4().hex[:12]
        self._insert_workspace(workspace_id, is_platform_workspace=False)

        with self.engine.connect() as conn:
            row = conn.execute(
                self._text("SELECT * FROM marketai_workspace_admission(:ws_id)"),
                {"ws_id": workspace_id},
            ).mappings().first()

        self.assertIsNotNone(row)
        self.assertEqual(row["workspace_id"], workspace_id)
        self.assertIs(row["is_platform_workspace"], False)

    def test_workspace_admission_unknown_workspace_returns_null_row(self) -> None:
        with self.engine.connect() as conn:
            row = conn.execute(
                self._text("SELECT * FROM marketai_workspace_admission(:ws_id)"),
                {"ws_id": "ws_does_not_exist_" + uuid.uuid4().hex[:12]},
            ).mappings().first()

        self.assertIsNotNone(row)
        self.assertIsNone(row["workspace_id"])
        self.assertIsNone(row["is_platform_workspace"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
