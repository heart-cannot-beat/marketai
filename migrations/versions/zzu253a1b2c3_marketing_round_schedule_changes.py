"""A test round's schedule changes: a moved start and an early end (#253, ADR 0019, step 2 design §5.7).

Revision ID: zzu253a1b2c3
Revises: zzt247a1b2c3

``marketing_round_schedule_changes`` is the append-only record of the two changes a user can make to the
dates of a test project's round after it was frozen:

- ``start_moved``: the ads went live on another day than planned; the end moves with it, the number of days
  stays. Only before the round's first report is confirmed.
- ``ended_early``: the ads were stopped before the planned end; the evaluation takes that day as the last.

The frozen snapshot of the round is never rewritten: the dates an evaluation uses (the "effective" start and
end) are the frozen ones plus these rows, the latest of each kind winning. Each row names who made the change
and when; a correction is a new row. Only rounds of test projects (``flow = "ads_test"``) get rows; rounds of
earlier tests keep their frozen dates.

The table is tenant owned: RLS enabled and forced, ``tenant_isolation`` policy, the round bound by a
Workspace-composite foreign key. The API reads and inserts it; the Worker reads it, because the checks that a
round's evaluation is still current -- run by the Worker before a paid explanation call and by Scale's
evidence checks -- read a test round's effective dates; readonly reads it. Nobody updates or deletes a row.
The grants are mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never reads the new table, and no row exists until
this release's commands write one. Downgrade refuses while any row exists: dropping them would put the rounds
they moved back on their frozen dates, so evaluations made on the moved dates would stop matching their
round; with none, it drops the table.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzu253a1b2c3"
down_revision = "zzt247a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_round_schedule_changes"
NEW_WORKSPACE_OWNED = (TABLE,)


def upgrade():
    op.create_table(TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("round_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"],
                                ondelete="RESTRICT", name="fk_marketing_round_schedule_change_round"),
        sa.CheckConstraint("kind IN ('start_moved', 'ended_early')", name="schedule_change_kind"),
    )
    op.create_index("ix_marketing_round_schedule_changes_workspace_id", TABLE, ["workspace_id"])
    op.create_index("ix_marketing_round_schedule_changes_round", TABLE, ["workspace_id", "round_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{TABLE}"
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{TABLE}" TO marketai_domain_api')
        op.execute(f'GRANT SELECT ON TABLE "{TABLE}" TO marketai_domain_worker, marketai_domain_readonly')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar():
        raise RuntimeError("round schedule changes exist; dropping them would put moved rounds back on their frozen dates")
    op.drop_index("ix_marketing_round_schedule_changes_round", table_name=TABLE)
    op.drop_index("ix_marketing_round_schedule_changes_workspace_id", table_name=TABLE)
    op.drop_table(TABLE)
