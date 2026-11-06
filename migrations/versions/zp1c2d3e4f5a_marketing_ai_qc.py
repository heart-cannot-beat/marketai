"""Exact-version multimodal QC suggestions with explicit human confirmation."""
from alembic import op
import sqlalchemy as sa

revision = "zp1c2d3e4f5a"
down_revision = "zo0b1c2d3e4f"
branch_labels = None
depends_on = None
NEW_WORKSPACE_OWNED = ("marketing_ai_qc_calls",)


def upgrade():
    op.create_table("marketing_ai_qc_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        *[sa.Column(name, sa.String(64), nullable=False) for name in
          ("round_id", "asset_version_id", "operation_id", "job_id", "input_hash", "request_hash")],
        sa.Column("retry_of_id", sa.String(64)), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False), sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False), sa.Column("model", sa.String(200), nullable=False),
        sa.Column("maximum_cost", sa.String(32), nullable=False), sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False), sa.Column("result_json", sa.JSON()),
        sa.Column("error_code", sa.String(64)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_ai_qc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "round_id", "asset_version_id", "request_hash", name="uq_marketing_ai_qc_request"),
        *[sa.ForeignKeyConstraint(["workspace_id", col], [table + ".workspace_id", table + ".id"], ondelete="RESTRICT")
          for col, table in (("round_id", "marketing_rounds"), ("asset_version_id", "asset_versions"),
            ("operation_id", "operation_records"), ("job_id", "jobs"), ("retry_of_id", "marketing_ai_qc_calls"))])
    op.create_index("ix_marketing_ai_qc_calls_workspace_id", "marketing_ai_qc_calls", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE marketing_ai_qc_calls ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE marketing_ai_qc_calls FORCE ROW LEVEL SECURITY')
        op.execute('''CREATE POLICY tenant_isolation ON marketing_ai_qc_calls
            USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute('GRANT SELECT, INSERT ON marketing_ai_qc_calls TO marketai_domain_api')
        op.execute('GRANT SELECT, UPDATE ON marketing_ai_qc_calls TO marketai_domain_worker')
        op.execute('GRANT SELECT ON marketing_ai_qc_calls TO marketai_domain_readonly')


def downgrade():
    op.drop_table("marketing_ai_qc_calls")
