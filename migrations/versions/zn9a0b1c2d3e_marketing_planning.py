"""Question-led marketing planning, separate from legacy experiments."""
from alembic import op
import sqlalchemy as sa

revision = "zn9a0b1c2d3e"
down_revision = "zm8b9c0d1e2f"
branch_labels = None
depends_on = None

NEW_WORKSPACE_OWNED = (
    "marketing_projects", "marketing_revisions", "marketing_planning_calls", "marketing_generation_links",
)


def upgrade():
    op.create_table("marketing_projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("product_id", sa.String(64)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("understanding_json", sa.JSON()), sa.Column("plan_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_projects_workspace_id"),
        sa.ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"], ondelete="RESTRICT"),
    )
    op.create_table("marketing_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "project_id", "revision", name="uq_marketing_revision"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
    )
    op.create_table("marketing_planning_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False), sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("state", sa.String(20), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False), sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(200), nullable=False), sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("maximum_cost", sa.String(32), nullable=False), sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"], ondelete="RESTRICT"),
    )
    op.create_table("marketing_generation_links",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False), sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("combination_id", sa.String(64), nullable=False), sa.Column("draft_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("workspace_id", "project_id", "plan_revision", "combination_id", name="uq_marketing_generation_link"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "draft_id"], ["generation_drafts.workspace_id", "generation_drafts.id"], ondelete="RESTRICT"),
    )
    for table in NEW_WORKSPACE_OWNED:
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute('GRANT SELECT ON workspaces TO marketai_domain_worker')
        op.execute('''CREATE VIEW workspace_provider_bindings WITH (security_barrier = true) AS
            SELECT b.workspace_id, b.provider, b.id AS binding_id, b.revision AS binding_revision
            FROM credential_bindings b JOIN credential_versions c
              ON c.workspace_id = b.workspace_id AND c.id = b.credential_version_id AND c.provider = b.provider
            WHERE b.binding_scope = 'generation' AND c.state = 'active'
              AND marketai_context_is_current(b.workspace_id)''')
        op.execute('REVOKE ALL ON workspace_provider_bindings FROM PUBLIC')
        op.execute('GRANT SELECT ON workspace_provider_bindings TO marketai_domain_api')
        for table in NEW_WORKSPACE_OWNED:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (marketai_context_is_current(workspace_id))
                WITH CHECK (marketai_context_is_current(workspace_id))''')
            op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO marketai_domain_api')
            op.execute(f'GRANT SELECT ON TABLE "{table}" TO marketai_domain_worker')
            if table in ("marketing_projects", "marketing_planning_calls"):
                op.execute(f'GRANT UPDATE ON TABLE "{table}" TO marketai_domain_api, marketai_domain_worker')
            if table == "marketing_revisions":
                op.execute(f'GRANT INSERT ON TABLE "{table}" TO marketai_domain_worker')
            op.execute(f'GRANT SELECT ON TABLE "{table}" TO marketai_domain_readonly')


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute('DROP VIEW workspace_provider_bindings')
    for table in reversed(NEW_WORKSPACE_OWNED):
        op.drop_table(table)
