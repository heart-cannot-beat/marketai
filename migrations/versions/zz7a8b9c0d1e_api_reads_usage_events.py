"""Let the API read the usage events it serves (Issue #347).

Revision ID: zz7a8b9c0d1e
Revises: zz6f7a8b9c0d

``GET /api/v1/usage`` (``get_usage``) sums ``usage_events`` as the API role,
but ``k1a2b3c4d5e6`` granted the API INSERT only, so every call on PostgreSQL
answered 500 ``permission denied for table usage_events``. The design spec's
matrix (§2.16, §4.14) already gives the API SELECT, and the ownership contract
lets a Member view Usage.

* The API gains SELECT on ``usage_events`` (API: SELECT, INSERT; Worker:
  INSERT; Readonly: SELECT; nobody UPDATE or DELETE). Forced RLS still scopes
  every read to the caller's Workspace. The grant is mirrored in
  ``deploy/postgres/domain-tenant-roles.sql``.
* The Worker gets no SELECT, although the spec's matrix lists one: it reads no
  usage event today.

No schema change, so the previous release keeps running while this migrates.
The Release applies the roles script before Alembic, so on the first deploy
the script grants SELECT and this repeats it. SQLite checks no grants.
"""

from alembic import op


revision = "zz7a8b9c0d1e"
down_revision = "zz6f7a8b9c0d"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute("GRANT SELECT ON TABLE usage_events TO marketai_domain_api")


def downgrade() -> None:
    if _is_postgres():
        op.execute("REVOKE SELECT ON TABLE usage_events FROM marketai_domain_api")
