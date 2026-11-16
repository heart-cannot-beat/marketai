"""Ads-first ideas container on marketing projects, and the keep mark (#189, ADR 0018).

Revision ID: zzm189a1b2c3
Revises: zzl148a1b2c3

``marketing_projects`` gains four columns. ``flow`` is ``plan_first`` for
every existing row (server default) and ``ads_first`` for projects created
through ``/marketing/ideas``; the old endpoints keep reading only
``plan_first`` rows. ``market``, ``ad_language`` and ``ideas_json`` are null
for ``plan_first`` rows. ``marketing_projects`` already grants UPDATE to the
API and Worker roles, so the new columns need no new grant.

``marketing_ad_keeps`` holds one row per (project, ad) with the user's current
"keep" mark; each change is also appended to the audit log. It is tenant
owned: RLS enabled and forced, ``tenant_isolation`` policy, the project bound
by a composite foreign key. The API reads, inserts and updates it; the Worker
and readonly roles only read it. The grants are mirrored in
``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never writes the
new columns (the default keeps its rows ``plan_first``) and never reads the
new table. Downgrade refuses while any ``ads_first`` project or keep mark
exists, because dropping ``flow`` would hand those projects to the old
endpoints as if they were plan-first tests.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzm189a1b2c3"
down_revision = "zzl148a1b2c3"
branch_labels = None
depends_on = None

NEW_WORKSPACE_OWNED = ("marketing_ad_keeps",)


def upgrade():
    with op.batch_alter_table("marketing_projects") as batch:
        batch.add_column(sa.Column("flow", sa.String(20), nullable=False, server_default="plan_first"))
        batch.add_column(sa.Column("market", sa.String(80), nullable=True))
        batch.add_column(sa.Column("ad_language", sa.String(35), nullable=True))
        batch.add_column(sa.Column("ideas_json", sa.JSON(), nullable=True))
    op.create_table("marketing_ad_keeps",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("kept", sa.Boolean(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "project_id", "asset_version_id", name="uq_marketing_ad_keep"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                                ondelete="RESTRICT"),
    )
    op.create_index("ix_marketing_ad_keeps_workspace_id", "marketing_ad_keeps", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_WORKSPACE_OWNED:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (marketai_context_is_current(workspace_id))
                WITH CHECK (marketai_context_is_current(workspace_id))''')
            op.execute(f'GRANT SELECT, INSERT, UPDATE ON TABLE "{table}" TO marketai_domain_api')
            op.execute(f'GRANT SELECT ON TABLE "{table}" TO marketai_domain_worker, marketai_domain_readonly')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Both tables FORCE RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if (bind.execute(sa.text("SELECT COUNT(*) FROM marketing_projects WHERE flow = 'ads_first'")).scalar()
            or bind.execute(sa.text("SELECT COUNT(*) FROM marketing_ad_keeps")).scalar()):
        raise RuntimeError("ads_first projects or keep marks exist; downgrading would show them to the plan-first endpoints")
    op.drop_index("ix_marketing_ad_keeps_workspace_id", table_name="marketing_ad_keeps")
    op.drop_table("marketing_ad_keeps")
    with op.batch_alter_table("marketing_projects") as batch:
        batch.drop_column("ideas_json")
        batch.drop_column("ad_language")
        batch.drop_column("market")
        batch.drop_column("flow")
