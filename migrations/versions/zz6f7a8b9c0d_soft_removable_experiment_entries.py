"""Edit and remove draft experiment entries under the API role (Issue #284).

Revision ID: zz6f7a8b9c0d
Revises: zz5e6f7a8b9c

The API edits a draft entry in place and removes one by marking it, because
the domain database grants no DELETE to any role (方案 A on #284):

* ``experiment_entries.removed_at`` -- nullable, no backfill. A removed entry
  keeps its row; every read leaves it out.
* ``uq_experiment_entry_position`` and ``uq_experiment_entry_asset_version``
  become unique indexes over live entries only (``WHERE removed_at IS NULL``),
  so a removed entry's asset version and position can be taken again.
* The API gains UPDATE on ``experiment_entries`` (API: SELECT, INSERT, UPDATE;
  Worker: SELECT; Readonly: SELECT; nobody DELETE). The grant is mirrored in
  ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates (#283): it reads no
``removed_at`` and never removes an entry on PostgreSQL (its DELETE was
refused), so no removed row exists until this release serves requests.

Downgrade refuses while any removed entry exists: dropping the column would
bring it back as a live entry.
"""

import sqlalchemy as sa
from alembic import op


revision = "zz6f7a8b9c0d"
down_revision = "zz5e6f7a8b9c"
branch_labels = None
depends_on = None


LIVE = "removed_at IS NULL"
#: The unique keys of a live entry, constraint name -> columns.
LIVE_UNIQUE = (
    ("uq_experiment_entry_position", ["experiment_id", "position"]),
    ("uq_experiment_entry_asset_version", ["experiment_id", "asset_version_id"]),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
        for name, _columns in LIVE_UNIQUE:
            batch_op.drop_constraint(name, type_="unique")
        for name, columns in LIVE_UNIQUE:
            batch_op.create_index(
                name, columns, unique=True,
                sqlite_where=sa.text(LIVE), postgresql_where=sa.text(LIVE),
            )
    if _is_postgres():
        op.execute("GRANT UPDATE ON TABLE experiment_entries TO marketai_domain_api")


def _removed_entries() -> int:
    if not _is_postgres():
        return op.get_bind().execute(
            sa.text("SELECT count(*) FROM experiment_entries WHERE removed_at IS NOT NULL")
        ).scalar_one()
    # The owner sees no Workspace's rows while RLS is forced; lift it for this
    # count only, inside the migration transaction.
    op.execute('ALTER TABLE "experiment_entries" NO FORCE ROW LEVEL SECURITY')
    removed = op.get_bind().execute(
        sa.text("SELECT count(*) FROM experiment_entries WHERE removed_at IS NOT NULL")
    ).scalar_one()
    op.execute('ALTER TABLE "experiment_entries" FORCE ROW LEVEL SECURITY')
    return removed


def downgrade() -> None:
    removed = _removed_entries()
    if removed:
        raise RuntimeError(
            f"cannot downgrade zz6f7a8b9c0d: {removed} removed experiment entries would come back as live entries"
        )
    if _is_postgres():
        op.execute("REVOKE UPDATE ON TABLE experiment_entries FROM marketai_domain_api")
    with op.batch_alter_table("experiment_entries", schema=None) as batch_op:
        for name, _columns in LIVE_UNIQUE:
            batch_op.drop_index(name, sqlite_where=sa.text(LIVE), postgresql_where=sa.text(LIVE))
        for name, columns in LIVE_UNIQUE:
            batch_op.create_unique_constraint(name, columns)
        batch_op.drop_column("removed_at")
