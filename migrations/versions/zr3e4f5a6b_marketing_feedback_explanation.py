"""Persist QC correction lineage and evidence-bound AI explanations."""
from alembic import op
import sqlalchemy as sa

revision = "zr3e4f5a6b7c"
down_revision = "zq2d3e4f5a6b"
branch_labels = None
depends_on = None
NEW_WORKSPACE_OWNED = ("marketing_feedback_drafts",)


def upgrade():
    op.add_column("marketing_planning_calls", sa.Column("output_json", sa.JSON()))
    op.create_table("marketing_feedback_drafts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        *[sa.Column(name, sa.String(64), nullable=False) for name in
          ("round_id", "source_asset_version_id", "draft_id", "feedback_hash")],
        sa.Column("feedback_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "draft_id", name="uq_marketing_feedback_draft"),
        sa.UniqueConstraint("workspace_id", "round_id", "feedback_hash", name="uq_marketing_feedback_intent"),
        *[sa.ForeignKeyConstraint(["workspace_id", col], [table + ".workspace_id", table + ".id"], ondelete="RESTRICT")
          for col, table in (("round_id", "marketing_rounds"), ("source_asset_version_id", "asset_versions"), ("draft_id", "generation_drafts"))])
    op.create_index("ix_marketing_feedback_drafts_workspace_id", "marketing_feedback_drafts", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE marketing_feedback_drafts ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE marketing_feedback_drafts FORCE ROW LEVEL SECURITY')
        op.execute('''CREATE POLICY tenant_isolation ON marketing_feedback_drafts
            USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute('GRANT SELECT, INSERT ON marketing_feedback_drafts TO marketai_domain_api')
        op.execute('GRANT SELECT ON marketing_feedback_drafts TO marketai_domain_worker, marketai_domain_readonly')


def downgrade():
    op.drop_table("marketing_feedback_drafts")
    op.drop_column("marketing_planning_calls", "output_json")
