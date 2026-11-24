"""A test round's Meta ad matches can be changed or taken away by a new record (#252, ADR 0019, design §5.7).

Revision ID: zzv252a1b2c3
Revises: zzv254a1b2c3

``marketing_ad_bindings`` records which package ad a Meta ad of a round shows, by its Ad ID. Until now a round
had at most one record per Meta ad and nobody changes a record (the API may only read and insert it). A test
project's round lets the user change or take away a match before its first report is confirmed, each change a
new record that keeps the old one as history. So the table gains:

- ``replaces_id``: the record this one replaces (a composite ``(workspace_id, replaces_id)`` foreign key to the
  same table, so it never names another Workspace's record); a unique ``(workspace_id, replaces_id)`` keeps
  each record replaced at most once, so the records of one Meta ad in a round form one line;
- ``removed``: the record takes the Meta ad's match away (it repeats the entry it takes away);
- ``actor_id``: who confirmed the record (empty on records written before this release).

The unique ``(workspace_id, round_id, ad_account_id, ad_id)`` becomes a partial unique index on the same
columns for first records only (``replaces_id IS NULL``): still one line per Meta ad and round. A round's
current match of a Meta ad is the record of its line no other record replaces, unless it is ``removed``.
Rounds of earlier tests never get a replacing record, so for them every record stays current, as before.

RLS, the ``tenant_isolation`` policy and the grants are unchanged: the grants are table-wide (API SELECT and
INSERT; Worker and readonly SELECT), so the new columns need none, and nobody updates or deletes a record.

The previous release keeps running while this migrates: it never writes the new columns (``removed`` defaults
to false) and finds no replacing record until this release writes one. Downgrade refuses while a replacing
record exists: dropping it would put a changed or removed match back. With none, it drops the columns --
``actor_id`` too: who confirmed a match stays in the round's ``marketing.test_bindings_confirmed`` audit
events -- and restores the unique constraint.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzv252a1b2c3"
down_revision = "zzv254a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_ad_bindings"
KEY = ["workspace_id", "round_id", "ad_account_id", "ad_id"]
FIRST = sa.text("replaces_id IS NULL")


def upgrade():
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("replaces_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("removed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("actor_id", sa.String(128), nullable=True))
        batch.drop_constraint("uq_marketing_ad_round", type_="unique")
        batch.create_unique_constraint("uq_marketing_ad_bindings_workspace_id", ["workspace_id", "id"])
        batch.create_unique_constraint("uq_marketing_ad_binding_replaces", ["workspace_id", "replaces_id"])
        batch.create_foreign_key("fk_marketing_ad_binding_replaces", TABLE, ["workspace_id", "replaces_id"],
                                 ["workspace_id", "id"], ondelete="RESTRICT")
    op.create_index("uq_marketing_ad_round_first", TABLE, KEY, unique=True, sqlite_where=FIRST, postgresql_where=FIRST)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE replaces_id IS NOT NULL OR removed")).scalar():
        raise RuntimeError("ad binding changes exist; dropping them would put changed or removed matches back")
    op.drop_index("uq_marketing_ad_round_first", table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint("fk_marketing_ad_binding_replaces", type_="foreignkey")
        batch.drop_constraint("uq_marketing_ad_binding_replaces", type_="unique")
        batch.drop_constraint("uq_marketing_ad_bindings_workspace_id", type_="unique")
        batch.create_unique_constraint("uq_marketing_ad_round", KEY)
        batch.drop_column("actor_id")
        batch.drop_column("removed")
        batch.drop_column("replaces_id")
