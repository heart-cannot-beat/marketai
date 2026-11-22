"""A test round's recorded deviations from its plan (#254, ADR 0019 decision 6, step 2 design §5.7).

Revision ID: zzv254a1b2c3
Revises: zzu253a1b2c3

``marketing_round_deviations`` is the append-only record of what ran differently from a test project's
round plan, written whenever the user notices it -- before or after the round's Meta ads were matched, while
the round runs or after it closed. Each row names the round's ads it concerns (entries of its ad package), what
was different, who recorded it and when; a later note is a new row. The evaluation reads each row as a
deviation noted when those ads' Meta ads were matched, so under the existing rule the compared groups it
names draw no conclusion this round. The frozen snapshot and the bindings are never rewritten. Only rounds of
test projects (``flow = "ads_test"``) get rows; rounds of earlier tests keep their binding-time deviations only.

The table is tenant owned: RLS enabled and forced, ``tenant_isolation`` policy, the round bound by a
Workspace-composite foreign key. The API reads and inserts it; the Worker reads it, because the checks that a
round's evaluation is still current -- run by the Worker before a paid explanation call and by Scale's
evidence checks -- read a test round's recorded deviations; readonly reads it. Nobody updates or deletes a
row. The grants are mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never reads the new table, and no row exists until
this release's command writes one. Downgrade refuses while any row exists: dropping them would let the groups
they name conclude as if their delivery had matched the plan, so evaluations made with them would stop
matching their round; with none, it drops the table.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzv254a1b2c3"
down_revision = "zzu253a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_round_deviations"
NEW_WORKSPACE_OWNED = (TABLE,)


def upgrade():
    op.create_table(TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("round_id", sa.String(64), nullable=False),
        sa.Column("asset_version_ids", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"],
                                ondelete="RESTRICT", name="fk_marketing_round_deviation_round"),
    )
    op.create_index("ix_marketing_round_deviations_workspace_id", TABLE, ["workspace_id"])
    op.create_index("ix_marketing_round_deviations_round", TABLE, ["workspace_id", "round_id"])
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
        raise RuntimeError("round deviations exist; dropping them would let the groups they name conclude this round")
    op.drop_index("ix_marketing_round_deviations_round", table_name=TABLE)
    op.drop_index("ix_marketing_round_deviations_workspace_id", table_name=TABLE)
    op.drop_table(TABLE)
