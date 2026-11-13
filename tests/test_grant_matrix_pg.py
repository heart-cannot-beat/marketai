"""The grant matrix the SQLite grant guard checks is the one PostgreSQL has (#59).

``tests/support/grant_matrix.py`` evaluates
``deploy/postgres/domain-tenant-roles.sql`` without a server, and the grant
guard (``tests/support/grant_guard.py``) checks every write the SQLite suite
makes as the API or the Worker against that result. This test proves the
evaluation is faithful: after ``make db-release``, the table and column ACLs
PostgreSQL holds for the runtime roles (``pg_class.relacl``,
``pg_attribute.attacl``) are exactly the ones the evaluator derives, for every
table the release created.

A mismatch means the evaluator misread the role file (fix the evaluator), or
something other than the role file changed a runtime role's grants after the
release (find it). Run it right after the release steps, before any test that
downgrades and upgrades a migration: a migration's own GRANTs are not the role
file's, and only the role file's survive a release.

Run with ``DOMAIN_OWNER_DATABASE_URL`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import os
import unittest

import pytest
from sqlalchemy import create_engine, text

from tests.support.grant_matrix import (
    API_ROLE,
    READONLY_ROLE,
    WORKER_ROLE,
    load_grant_matrix,
)

pytestmark = pytest.mark.postgres

#: The roles the file revokes everything from and grants back: their ACLs
#: are the file's alone. (``marketai_projection_writer`` also keeps grants
#: its migrations made, which the file never resets.)
ROLES = (API_ROLE, WORKER_ROLE, READONLY_ROLE)

_RELATIONS = text(
    """
    SELECT c.relname
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    """
)

_TABLE_GRANTS = text(
    """
    SELECT r.rolname, c.relname, a.privilege_type
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     CROSS JOIN LATERAL aclexplode(c.relacl) a
      JOIN pg_roles r ON r.oid = a.grantee
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND r.rolname = ANY(:roles)
    """
)

_COLUMN_GRANTS = text(
    """
    SELECT r.rolname, c.relname, a.privilege_type, att.attname
      FROM pg_attribute att
      JOIN pg_class c ON c.oid = att.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     CROSS JOIN LATERAL aclexplode(att.attacl) a
      JOIN pg_roles r ON r.oid = a.grantee
     WHERE n.nspname = 'public'
       AND att.attnum > 0
       AND NOT att.attisdropped
       AND r.rolname = ANY(:roles)
    """
)


@unittest.skipUnless(
    os.environ.get("DOMAIN_OWNER_DATABASE_URL"),
    "needs DOMAIN_OWNER_DATABASE_URL on a cluster released with make db-release",
)
class GrantMatrixMatchesPostgresTests(unittest.TestCase):
    def test_the_evaluated_matrix_is_the_released_one(self):
        engine = create_engine(os.environ["DOMAIN_OWNER_DATABASE_URL"], future=True)
        self.addCleanup(engine.dispose)
        roles = list(ROLES)
        with engine.connect() as connection:
            relations = set(connection.execute(_RELATIONS).scalars())
            actual = {
                (role, table, privilege, None)
                for role, table, privilege in connection.execute(_TABLE_GRANTS, {"roles": roles})
            } | {
                (role, table, privilege, column)
                for role, table, privilege, column in connection.execute(
                    _COLUMN_GRANTS, {"roles": roles}
                )
            }

        self.assertGreater(len(relations), 100, "the cluster has not been migrated")
        self.assertGreater(len(actual), 1000, "the release has not granted the runtime roles")
        expected = {
            row for row in load_grant_matrix().acl(set(ROLES)) if row[1] in relations
        }
        missing = sorted(expected - actual, key=str)
        extra = sorted(actual - expected, key=str)
        self.assertEqual(
            (missing, extra),
            ([], []),
            "evaluated grants PostgreSQL does not hold (first list) and grants it "
            "holds that the evaluation lacks (second list), as (role, table, "
            "privilege, column or None)",
        )


if __name__ == "__main__":
    unittest.main()
