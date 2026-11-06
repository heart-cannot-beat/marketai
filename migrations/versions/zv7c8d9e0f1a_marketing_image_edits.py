"""Durable local crop/rotation and honest derived asset provenance."""
from alembic import op
import sqlalchemy as sa

revision = "zv7c8d9e0f1a"
down_revision = "zu6b7c8d9e0f"
branch_labels = None
depends_on = None
NEW_WORKSPACE_OWNED = ("marketing_image_edits",)


def upgrade():
    with op.batch_alter_table("asset_versions") as batch:
        batch.drop_constraint(op.f("ck_asset_versions_ck_asset_version_origin_kind"), type_="check")
        batch.create_check_constraint("ck_asset_version_origin_kind",
            "origin_kind IN ('generation', 'upload', 'reuse', 'legacy', 'localized', 'edit')")
    op.create_table("marketing_image_edits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        *[sa.Column(name, sa.String(64), nullable=False) for name in
          ("round_id", "source_asset_version_id", "operation_id", "input_hash")],
        sa.Column("result_asset_version_id", sa.String(64)),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_image_edit_workspace_id"),
        sa.UniqueConstraint("workspace_id", "round_id", "input_hash", name="uq_marketing_image_edit_input"),
        sa.CheckConstraint("state IN ('queued', 'succeeded', 'unchanged', 'failed')", name="image_edit_state"),
        *[sa.ForeignKeyConstraint(["workspace_id", col], [table + ".workspace_id", table + ".id"], ondelete="RESTRICT", name="fk_image_edit_" + col)
          for col, table in (("round_id", "marketing_rounds"), ("source_asset_version_id", "asset_versions"),
                             ("result_asset_version_id", "asset_versions"), ("operation_id", "operation_records"))])
    op.create_index("ix_marketing_image_edits_workspace_id", "marketing_image_edits", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE marketing_image_edits ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE marketing_image_edits FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON marketing_image_edits USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))")
        op.execute("GRANT SELECT, INSERT ON marketing_image_edits TO marketai_domain_api")
        op.execute("GRANT SELECT ON marketing_image_edits TO marketai_domain_worker, marketai_domain_readonly")
        op.execute("GRANT UPDATE (state, error_code, result_asset_version_id) ON marketing_image_edits TO marketai_domain_worker")


def downgrade():
    # Derived versions are evidence. Refuse to relabel or delete them to downgrade.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM asset_versions WHERE origin_kind = 'edit'")).scalar():
        raise RuntimeError("derived asset versions prevent downgrade")
    op.drop_table("marketing_image_edits")
    with op.batch_alter_table("asset_versions") as batch:
        batch.drop_constraint(op.f("ck_asset_versions_ck_asset_version_origin_kind"), type_="check")
        batch.create_check_constraint("ck_asset_version_origin_kind", "origin_kind IN ('generation', 'upload', 'reuse', 'legacy', 'localized')")
