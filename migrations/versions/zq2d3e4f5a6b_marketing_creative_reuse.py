"""Associate existing creative versions with a frozen test intent."""
from alembic import op
import sqlalchemy as sa

revision = "zq2d3e4f5a6b"
down_revision = "zp1c2d3e4f5a"
branch_labels = None
depends_on = None
NEW_WORKSPACE_OWNED = ("marketing_creative_reuses",)


def upgrade():
    op.create_table("marketing_creative_reuses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        *[sa.Column(name, sa.String(64), nullable=False) for name in
          ("round_id", "asset_version_id", "combination_id", "source_hash")],
        sa.Column("source_json", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "round_id", "asset_version_id", name="uq_marketing_creative_reuse"),
        *[sa.ForeignKeyConstraint(["workspace_id", col], [table + ".workspace_id", table + ".id"], ondelete="RESTRICT")
          for col, table in (("round_id", "marketing_rounds"), ("asset_version_id", "asset_versions"))])
    op.create_index("ix_marketing_creative_reuses_workspace_id", "marketing_creative_reuses", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE marketing_creative_reuses ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE marketing_creative_reuses FORCE ROW LEVEL SECURITY')
        op.execute('''CREATE POLICY tenant_isolation ON marketing_creative_reuses
            USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute('GRANT SELECT, INSERT ON marketing_creative_reuses TO marketai_domain_api')
        op.execute('GRANT SELECT ON marketing_creative_reuses TO marketai_domain_worker, marketai_domain_readonly')


def downgrade():
    op.drop_table("marketing_creative_reuses")
