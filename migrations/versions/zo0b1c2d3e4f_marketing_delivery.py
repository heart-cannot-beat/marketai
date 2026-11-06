"""Frozen marketing rounds, exact QC, handoffs and append-only evidence."""
from alembic import op
import sqlalchemy as sa

revision = "zo0b1c2d3e4f"
down_revision = "zn9a0b1c2d3e"
branch_labels = None
depends_on = None
NEW_WORKSPACE_OWNED = ("marketing_rounds", "marketing_qc_reviews", "marketing_handoffs", "marketing_ad_bindings", "marketing_metric_batches", "marketing_evaluations")


def _fk(column, parent):
    return sa.ForeignKeyConstraint(["workspace_id", column], [parent + ".workspace_id", parent + ".id"], ondelete="RESTRICT")


def _table(name, *columns):
    op.create_table(name, sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        *columns, sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_" + name + "_workspace_id", name, ["workspace_id"])


def _c(name, kind=sa.String(64), nullable=False):
    return sa.Column(name, kind, nullable=nullable)


def upgrade():
    _table("marketing_rounds", _c("project_id"), _c("plan_revision", sa.Integer()), _c("ordinal", sa.Integer()),
        _c("revision", sa.Integer()), _c("state", sa.String(24)), _c("execution_mode", sa.String(16)),
        _c("snapshot_json", sa.JSON()), _c("start_date", sa.Date()), _c("end_date", sa.Date()),
        _c("timezone"), _c("close_json", sa.JSON(), True), _fk("project_id", "marketing_projects"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_rounds_workspace_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "plan_revision", name="uq_marketing_round_plan"))
    _table("marketing_qc_reviews", _c("round_id"), _c("asset_version_id"), _c("findings_json", sa.JSON()),
        _c("quality_review_id"), _c("actor_id", sa.String(128)),
        _fk("round_id", "marketing_rounds"), _fk("asset_version_id", "asset_versions"), _fk("quality_review_id", "quality_reviews"))
    _table("marketing_handoffs", _c("round_id"), _c("operation_id"), _c("state", sa.String(24)),
        _c("manifest_json", sa.JSON()), _c("blob_json", sa.JSON(), True), _c("error_code", nullable=True),
        _fk("round_id", "marketing_rounds"), _fk("operation_id", "operation_records"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_handoffs_workspace_id"),
        sa.UniqueConstraint("workspace_id", "round_id", name="uq_marketing_handoff_round"))
    _table("marketing_ad_bindings", _c("round_id"), _c("handoff_id"), _c("entry_id"), _c("ad_account_id"), _c("ad_id"),
        _c("start_date", sa.Date()), _c("end_date", sa.Date()), _c("conditions_json", sa.JSON()),
        _fk("round_id", "marketing_rounds"), _fk("handoff_id", "marketing_handoffs"),
        sa.UniqueConstraint("workspace_id", "round_id", "ad_account_id", "ad_id", name="uq_marketing_ad_round"))
    _table("marketing_metric_batches", _c("round_id"), _c("replaces_id", nullable=True), _c("request_hash"), _c("raw_hash"),
        _c("raw_csv", sa.Text()), _c("metadata_json", sa.JSON()), _c("rows_json", sa.JSON()), _c("issues_json", sa.JSON()),
        _c("state", sa.String(24)), _fk("round_id", "marketing_rounds"), _fk("replaces_id", "marketing_metric_batches"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_metric_batches_workspace_id"),
        sa.UniqueConstraint("workspace_id", "round_id", "request_hash", name="uq_marketing_metric_request"))
    _table("marketing_evaluations", _c("round_id"), _c("input_hash"), _c("input_json", sa.JSON()), _c("result_json", sa.JSON()),
        _fk("round_id", "marketing_rounds"),
        sa.UniqueConstraint("workspace_id", "round_id", "input_hash", name="uq_marketing_evaluation_input"))
    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_WORKSPACE_OWNED:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))''')
            op.execute(f'GRANT SELECT, INSERT ON "{table}" TO marketai_domain_api')
            op.execute(f'GRANT SELECT ON "{table}" TO marketai_domain_worker, marketai_domain_readonly')
        for table in ("marketing_rounds", "marketing_metric_batches", "marketing_handoffs"):
            op.execute(f'GRANT UPDATE ON "{table}" TO marketai_domain_api')
        op.execute('GRANT UPDATE ON marketing_handoffs TO marketai_domain_worker')


def downgrade():
    for table in reversed(NEW_WORKSPACE_OWNED):
        op.drop_table(table)
