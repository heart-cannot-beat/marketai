"""Database-role negative test (SQLAlchemy half).

Proves that SQLAlchemy cannot open a connection to the control-plane database
with the domain role, and that the control-plane role cannot open a connection
to the domain database. The Prisma half lives in
``web/__tests__/integration/database-role-isolation.spec.ts``.

Requires a PostgreSQL cluster provisioned with
``deploy/postgres/control-plane-domain-roles.sql``. The tests skip when the
connection strings are absent so ``make verify`` stays runnable on a clean
checkout without PostgreSQL; CI supplies them.
"""

from __future__ import annotations

import os
import unittest

DOMAIN_URL = os.environ.get("DOMAIN_DATABASE_URL")
DOMAIN_ROLE_ON_CONTROL_DB_URL = os.environ.get("DOMAIN_ROLE_ON_CONTROL_DB_URL")
CONTROL_ROLE_ON_DOMAIN_DB_URL = os.environ.get("CONTROL_ROLE_ON_DOMAIN_DB_URL")

CONFIGURED = all(
    (DOMAIN_URL, DOMAIN_ROLE_ON_CONTROL_DB_URL, CONTROL_ROLE_ON_DOMAIN_DB_URL)
)

SKIP_REASON = (
    "set DOMAIN_DATABASE_URL, DOMAIN_ROLE_ON_CONTROL_DB_URL and "
    "CONTROL_ROLE_ON_DOMAIN_DB_URL against a cluster provisioned with "
    "deploy/postgres/control-plane-domain-roles.sql"
)


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class DatabaseRoleIsolationTests(unittest.TestCase):
    """The tenant/ownership boundary is enforced at CONNECT, not at grants."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)

    def _connect(self, url: str):
        engine = self.create_engine(url, pool_pre_ping=False)
        try:
            with engine.connect() as connection:
                return connection.execute(self.text("SELECT 1")).scalar_one()
        finally:
            engine.dispose()

    def test_domain_role_connects_to_the_domain_database(self):
        self.assertEqual(self._connect(DOMAIN_URL), 1)

    def test_domain_role_cannot_connect_to_the_control_plane_database(self):
        from sqlalchemy.exc import SQLAlchemyError

        with self.assertRaises(SQLAlchemyError) as caught:
            self._connect(DOMAIN_ROLE_ON_CONTROL_DB_URL)

        self.assertIn("permission denied", str(caught.exception).lower())

    def test_control_plane_role_cannot_connect_to_the_domain_database(self):
        from sqlalchemy.exc import SQLAlchemyError

        with self.assertRaises(SQLAlchemyError) as caught:
            self._connect(CONTROL_ROLE_ON_DOMAIN_DB_URL)

        self.assertIn("permission denied", str(caught.exception).lower())


class DatabaseRoleIsolationConfigurationTests(unittest.TestCase):
    """Guards that stay meaningful even without a PostgreSQL cluster."""

    def test_role_provisioning_script_is_committed(self):
        import pathlib

        sql = (
            pathlib.Path(__file__).resolve().parents[1]
            / "deploy"
            / "postgres"
            / "control-plane-domain-roles.sql"
        )

        self.assertTrue(sql.exists(), "role provisioning SQL is missing")

        body = sql.read_text(encoding="utf-8")

        # The denials are the point of the file; assert they are still there.
        self.assertIn("REVOKE ALL ON DATABASE marketai_domain", body)
        self.assertIn("REVOKE ALL ON DATABASE marketai_control", body)
        self.assertIn("marketai_control_app", body)
        self.assertIn("marketai_domain_app", body)

    def test_no_password_literals_in_the_provisioning_script(self):
        import pathlib
        import re

        sql = (
            pathlib.Path(__file__).resolve().parents[1]
            / "deploy"
            / "postgres"
            / "control-plane-domain-roles.sql"
        )

        body = sql.read_text(encoding="utf-8")

        # Passwords must arrive as psql variables, never as literals.
        self.assertFalse(
            re.search(r"PASSWORD\s+'", body),
            "provisioning SQL must not contain literal passwords",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
