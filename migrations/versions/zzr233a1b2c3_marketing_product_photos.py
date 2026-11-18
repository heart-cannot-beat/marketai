"""Photos the user adds to a product of ads-first ideas (#233, ADR 0018, design doc §5.2 and §5.7).

Revision ID: zzr233a1b2c3
Revises: zzq200a1b2c3

Until now an ads-first product's photos were only the ones its reading found (#199), kept in the product
intake -- a record of the reading, which nothing adds to afterwards. A product whose reading found no
usable photo had no way to get one.

- ``marketing_product_photos`` is new: an image the user uploaded through the existing reference upload
  gate (``reference_objects``, Worker job ``library.reference_gate``), added as a photo of one product with
  the role the user chose (``main``, ``angle``, ``detail``; ``logo`` for the brand's logo). One image is one
  photo of a product (unique per product and object). A row is never updated or deleted; whether the user
  checked or rejected the photo stays in the verification records (``marketing_ideas_export_records``).
- ``reference_objects`` gains the unique ``(workspace_id, id)`` so the new table can bind its object by a
  Workspace-composite foreign key, like every other tenant-owned reference. ``id`` is the primary key, so
  every existing row already satisfies it.

The new table is tenant owned: RLS enabled and forced, ``tenant_isolation`` policy, the product, the ideas
and the object bound by Workspace-composite foreign keys. The API reads and inserts it; the Worker and
readonly read it -- the Worker because a first batch drafts its images with the product's photos, added
ones included. Nobody updates or deletes a row. The grants are mirrored in
``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never reads the new table, and the new unique
constraint changes nothing it writes. Downgrade refuses while any added photo exists: dropping them would
lose which images the user added to which product (a checked one's decision would then name a photo no
product lists).
"""
from alembic import op
import sqlalchemy as sa

revision = "zzr233a1b2c3"
down_revision = "zzq200a1b2c3"
branch_labels = None
depends_on = None

PHOTOS = "marketing_product_photos"
NEW_WORKSPACE_OWNED = (PHOTOS,)


def upgrade():
    with op.batch_alter_table("reference_objects") as batch:
        batch.create_unique_constraint("uq_reference_objects_workspace_id", ["workspace_id", "id"])
    op.create_table(PHOTOS,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("reference_object_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_product_photos_workspace_id"),
        sa.UniqueConstraint("workspace_id", "product_id", "reference_object_id", name="uq_marketing_product_photo_object"),
        sa.ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
                                ondelete="RESTRICT", name="fk_marketing_product_photo_product"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                                ondelete="RESTRICT", name="fk_marketing_product_photo_project"),
        sa.ForeignKeyConstraint(["workspace_id", "reference_object_id"],
                                ["reference_objects.workspace_id", "reference_objects.id"],
                                ondelete="RESTRICT", name="fk_marketing_product_photo_object"),
        sa.CheckConstraint("role IN ('main', 'angle', 'detail', 'logo')", name="photo_role"),
    )
    op.create_index("ix_marketing_product_photos_workspace_id", PHOTOS, ["workspace_id"])
    op.create_index("ix_marketing_product_photos_product", PHOTOS, ["workspace_id", "product_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{PHOTOS}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{PHOTOS}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{PHOTOS}"
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{PHOTOS}" TO marketai_domain_api')
        op.execute(f'GRANT SELECT ON TABLE "{PHOTOS}" TO marketai_domain_worker, marketai_domain_readonly')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {PHOTOS}")).scalar():
        raise RuntimeError("photos added to products exist; dropping them would lose which images the user added to which product")
    op.drop_index("ix_marketing_product_photos_product", table_name=PHOTOS)
    op.drop_index("ix_marketing_product_photos_workspace_id", table_name=PHOTOS)
    op.drop_table(PHOTOS)
    with op.batch_alter_table("reference_objects") as batch:
        batch.drop_constraint("uq_reference_objects_workspace_id", type_="unique")
