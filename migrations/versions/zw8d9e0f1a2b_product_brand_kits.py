"""Append-only, versioned product brand baselines (#228, ADR 0013)."""
from alembic import op
import sqlalchemy as sa

revision = "zw8d9e0f1a2b"
down_revision = "zv7c8d9e0f1a"
branch_labels = None
depends_on = None
NEW_WORKSPACE_OWNED = ("product_brand_kits",)


def upgrade():
    op.create_table("product_brand_kits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_product_brand_kit_workspace_id"),
        sa.UniqueConstraint("workspace_id", "product_id", "version", name="uq_product_brand_kit_version"),
        sa.ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
                                ondelete="RESTRICT", name="fk_product_brand_kit_product"))
    op.create_index("ix_product_brand_kits_workspace_id", "product_brand_kits", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE product_brand_kits ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE product_brand_kits FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON product_brand_kits USING (marketai_context_is_current(workspace_id)) WITH CHECK (marketai_context_is_current(workspace_id))")
        # Versions are evidence that plans and recipes freeze: insert-only, never updated.
        op.execute("GRANT SELECT, INSERT ON product_brand_kits TO marketai_domain_api")
        op.execute("GRANT SELECT ON product_brand_kits TO marketai_domain_worker, marketai_domain_readonly")


def downgrade():
    # Plans and recipes reference exact versions. Refuse to discard them to downgrade.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM product_brand_kits")).scalar():
        raise RuntimeError("brand kit versions prevent downgrade")
    op.drop_table("product_brand_kits")
