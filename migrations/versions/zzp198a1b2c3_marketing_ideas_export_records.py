"""Ads-first verification and download records (#198, ADR 0018).

Revision ID: zzp198a1b2c3
Revises: zzo197a1b2c3

``marketing_ideas_export_records`` is the append-only evidence of the two decisions that take an ads-first
ad from draft to a downloaded file:

- ``verification``: what one "check before download" confirmed -- the facts (with the hash of the text the
  user saw), the ads made without a product photo whose appearance the user confirmed, the check findings
  the user accepted -- and the product version that verification published, if any;
- ``download``: which ads one download handed out, the download intents it created and what each ad stood
  on at that moment.

A row is never updated or deleted: the ads' current status is derived from these rows, the project's
facts and the images' checks. The table is tenant owned: RLS enabled and forced, ``tenant_isolation``
policy, the project bound by a composite foreign key. The API reads and inserts it; readonly reads it; the
Worker gets nothing, because no Job reads or writes a verification or a download. The grants are mirrored
in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never reads the new table. Downgrade drops the
table and refuses while any row exists: dropping them would lose who confirmed which fact and which ads
were downloaded, and would turn every verified ad back into a draft that the facts still call checked.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzp198a1b2c3"
down_revision = "zzo197a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_ideas_export_records"
NEW_WORKSPACE_OWNED = (TABLE,)


def upgrade():
    op.create_table(TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("library_version_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_ideas_export_records_workspace_id"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                                ondelete="RESTRICT", name="fk_marketing_ideas_export_record_project"),
        sa.CheckConstraint("kind IN ('verification', 'download')", name="record_kind"),
    )
    op.create_index("ix_marketing_ideas_export_records_workspace_id", TABLE, ["workspace_id"])
    op.create_index("ix_marketing_ideas_export_records_project", TABLE, ["workspace_id", "project_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{TABLE}"
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{TABLE}" TO marketai_domain_api')
        op.execute(f'GRANT SELECT ON TABLE "{TABLE}" TO marketai_domain_readonly')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar():
        raise RuntimeError("verifications or downloads exist; dropping them would lose who checked which fact")
    op.drop_index("ix_marketing_ideas_export_records_project", table_name=TABLE)
    op.drop_index("ix_marketing_ideas_export_records_workspace_id", table_name=TABLE)
    op.drop_table(TABLE)
