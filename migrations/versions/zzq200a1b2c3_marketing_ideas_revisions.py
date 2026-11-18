"""Ads-first revisions and uploads: changing and adding ad drafts without a round (#200, ADR 0018).

Revision ID: zzq200a1b2c3
Revises: zzp198a1b2c3

An ads-first ad has no round, so its corrections and crops hang on the ideas (``marketing_projects``):

- ``marketing_feedback_drafts`` (a correction or a supplier image tool, prepared as a generation draft) and
  ``marketing_image_edits`` (a local crop or rotation) keep both kinds of row: ``round_id`` becomes
  nullable, the new ``project_id`` names the ideas, and a check says exactly one of the two is set. A
  project row is unique per (project, input hash), like a round row per (round, input hash).
- ``marketing_ideas_uploads`` is new: an image the user uploaded (an external creative, #168) placed under
  one ad of the ideas, with the intent of that ad frozen at that moment. A row is never updated or deleted.

The two existing tables already enforce RLS with the ``tenant_isolation`` policy and their grants are
table-wide or name other columns (API SELECT and INSERT; Worker SELECT, and UPDATE of an edit's state and
result), so the new column needs no grant. ``project_id`` is bound to its project by a composite
``(workspace_id, project_id)`` foreign key, so a row can never name another Workspace's ideas; round
queries filter on ``round_id`` and never see a project row.

The new table is tenant owned: RLS enabled and forced, ``tenant_isolation`` policy, the project, the asset
version and the external creative bound by Workspace-composite foreign keys. The API reads and inserts it;
the Worker and readonly read it -- the Worker because a first batch's check step and every check's last
look list the ads of the ideas, uploads included. Nobody updates or deletes a row. The grants are mirrored
in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never writes ``project_id`` and never reads
the new table. Downgrade refuses while any project row or any upload exists: dropping them would lose the
user's corrections and uploads (and the record of which ad an image was made for), and ``round_id`` could
not become NOT NULL again.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzq200a1b2c3"
down_revision = "zzp198a1b2c3"
branch_labels = None
depends_on = None

UPLOADS = "marketing_ideas_uploads"
NEW_WORKSPACE_OWNED = (UPLOADS,)
#: Existing round tables that gain a project: (table, short name, unique columns' last name).
ROUND_TABLES = (("marketing_feedback_drafts", "feedback", "feedback_hash"),
                ("marketing_image_edits", "image_edit", "input_hash"))


def upgrade():
    for table, short, hashed in ROUND_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("round_id", existing_type=sa.String(64), nullable=True)
            batch.add_column(sa.Column("project_id", sa.String(64)))
            batch.create_foreign_key(f"fk_{short}_project", "marketing_projects",
                                     ["workspace_id", "project_id"], ["workspace_id", "id"], ondelete="RESTRICT")
            batch.create_unique_constraint(f"uq_marketing_{short}_project_input", ["workspace_id", "project_id", hashed])
            batch.create_check_constraint(f"{short}_subject", "(round_id IS NULL) <> (project_id IS NULL)")
    op.create_table(UPLOADS,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("combination_id", sa.String(64), nullable=False),
        sa.Column("external_creative_id", sa.String(64), nullable=False),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("intent_json", sa.JSON(), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_marketing_ideas_uploads_workspace_id"),
        sa.UniqueConstraint("workspace_id", "project_id", "asset_version_id", name="uq_marketing_ideas_upload_ad"),
        sa.ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                                ondelete="RESTRICT", name="fk_marketing_ideas_upload_project"),
        sa.ForeignKeyConstraint(["workspace_id", "asset_version_id"], ["asset_versions.workspace_id", "asset_versions.id"],
                                ondelete="RESTRICT", name="fk_marketing_ideas_upload_asset_version"),
        sa.ForeignKeyConstraint(["workspace_id", "external_creative_id"],
                                ["external_creatives.workspace_id", "external_creatives.id"],
                                ondelete="RESTRICT", name="fk_marketing_ideas_upload_external_creative"),
    )
    op.create_index("ix_marketing_ideas_uploads_workspace_id", UPLOADS, ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{UPLOADS}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{UPLOADS}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{UPLOADS}"
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))''')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{UPLOADS}" TO marketai_domain_api')
        op.execute(f'GRANT SELECT ON TABLE "{UPLOADS}" TO marketai_domain_worker, marketai_domain_readonly')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The tables FORCE RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {UPLOADS}")).scalar():
        raise RuntimeError("ads-first uploads exist; dropping them would lose which ad each uploaded image was placed under")
    for table, _, _ in ROUND_TABLES:
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE project_id IS NOT NULL")).scalar():
            raise RuntimeError(f"ads-first rows exist in {table}; dropping them would lose the user's corrections")
    op.drop_index("ix_marketing_ideas_uploads_workspace_id", table_name=UPLOADS)
    op.drop_table(UPLOADS)
    for table, short, _ in reversed(ROUND_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(op.f(f"ck_{table}_{short}_subject"), type_="check")
            batch.drop_constraint(f"uq_marketing_{short}_project_input", type_="unique")
            batch.drop_constraint(f"fk_{short}_project", type_="foreignkey")
            batch.drop_column("project_id")
            batch.alter_column("round_id", existing_type=sa.String(64), nullable=False)
