"""Let the API publish the AssetVersion of an adopted Localization output (Issue #46).

Revision ID: zz9c0d1e2f3a
Revises: zz8b9c0d1e2f

``POST /api/v1/localization/batches/{batch_id}/items/{item_id}/attempts/
{attempt_id}/adopt`` (``adopt_isolated_output``) publishes the localized child
AssetVersion in the API transaction, through the same publication path the
Worker's finalize uses (spec §2H, §3H: no new Operation). The API held SELECT
and UPDATE on ``asset_versions`` but no INSERT, so every adoption on
PostgreSQL answered 500 ``permission denied for table asset_versions``.

* The API gains INSERT on ``asset_versions`` (API: SELECT, INSERT, UPDATE;
  Worker: SELECT, INSERT, UPDATE; Readonly: SELECT; nobody DELETE). Forced RLS
  still scopes every write to the caller's Workspace. The grant is mirrored in
  ``deploy/postgres/domain-tenant-roles.sql``.

No schema change, so the previous release keeps running while this migrates.
GRANT is idempotent: the Release applies the roles script before and after
Alembic, so on an existing cluster this repeats a grant the script may already
have made. SQLite checks no grants.
"""

from alembic import op


revision = "zz9c0d1e2f3a"
down_revision = "zz8b9c0d1e2f"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute("GRANT INSERT ON TABLE asset_versions TO marketai_domain_api")


def downgrade() -> None:
    if _is_postgres():
        op.execute("REVOKE INSERT ON TABLE asset_versions FROM marketai_domain_api")
