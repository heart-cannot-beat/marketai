"""External creatives: upload images made elsewhere, tag them, let them join tests.

Revision ID: zm8b9c0d1e2f
Revises: zl7a8b9c0d1e

Issue #168 (front-flow contract §5.2). An image made in Lovart, by a
designer or as real UGC becomes an AssetVersion of origin ``upload`` that can
enter the same experiments as generated images and be attributed by
dimension value.

* ``assets`` gains ``source`` (``generation`` | ``external``) and ``run_id``
  becomes nullable: an external creative has no GenerationRun. A check ties
  the two (an external asset never has a Run, a generated one always does);
  existing rows are ``generation`` through the server default, nothing is
  rewritten.
* ``external_creatives`` -- one uploaded image: the committed upload, the
  uploader's rights declaration, the reference-gate object, the AssetVersion
  it became, the dimension vocabulary frozen at admission and the governed
  ``llm_chat`` tagging suggestion, with the Operation/Job of its Worker job.
* ``external_creative_tag_confirmations`` -- the user's confirmed tags, the
  concept identity written for them (same ``dck_`` keys a generated
  combination carries) and the learning placement. Append-only: editing
  appends a new revision.

Tenancy: RLS ``tenant_isolation`` on both new tables. Grants: the API
creates and reads both and advances ``external_creatives`` (confirm); the
Worker reads both and advances ``external_creatives`` (gate -> asset -> tag)
-- it already inserts ``assets`` / ``asset_versions`` and gates
``reference_objects``; confirmations get no UPDATE for any role. Readonly
reads both. ``deploy/postgres/domain-tenant-roles.sql`` repeats the matrix.
"""
from alembic import op
import sqlalchemy as sa


revision = "zm8b9c0d1e2f"
down_revision = "zl7a8b9c0d1e"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = (
    "external_creatives",
    "external_creative_tag_confirmations",
)

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("external_creatives", "SELECT, INSERT, UPDATE", "SELECT, UPDATE"),
    ("external_creative_tag_confirmations", "SELECT, INSERT", "SELECT"),
)

STATUSES = (
    "queued", "gating", "preparing_asset", "tagging",
    "ready", "tagging_failed", "confirmed", "rejected", "failed",
)
MADE_BY = ("self", "designer", "agency", "creator", "ai_tool", "other")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source", sa.String(length=16), nullable=False, server_default="generation")
        )
        batch_op.alter_column("run_id", existing_type=sa.String(length=64), nullable=True)
        batch_op.create_check_constraint(
            "ck_assets_source", "source IN ('generation', 'external')",
        )
        batch_op.create_check_constraint(
            "ck_assets_source_run",
            "(source = 'external' AND run_id IS NULL) "
            "OR (source = 'generation' AND run_id IS NOT NULL)",
        )

    op.create_table(
        "external_creatives",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_step", sa.String(length=32), nullable=True),
        sa.Column("upload_intent_id", sa.String(length=64), nullable=False),
        sa.Column("upload_key", sa.String(length=512), nullable=False),
        sa.Column("upload_storage_version_id", sa.String(length=256), nullable=False),
        sa.Column("upload_sha256", sa.String(length=64), nullable=False),
        sa.Column("upload_mime_type", sa.String(length=100), nullable=False),
        sa.Column("upload_byte_size", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("made_by", sa.String(length=16), nullable=False),
        sa.Column("rights_json", sa.JSON(), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("vocabulary_json", sa.JSON(), nullable=False),
        sa.Column("vocabulary_hash", sa.String(length=64), nullable=False),
        sa.Column("suggestion_json", sa.JSON(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("reference_object_id", sa.String(length=64), nullable=True),
        sa.Column("asset_id", sa.String(length=100), nullable=True),
        sa.Column("asset_version_id", sa.String(length=64), nullable=True),
        sa.Column("confirmed_revision", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("batch_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("initiator_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN " + _in(STATUSES), name="ck_external_creatives_status"),
        sa.CheckConstraint("execution_mode IN ('mock', 'byok')", name="ck_external_creatives_execution_mode"),
        sa.CheckConstraint("made_by IN " + _in(MADE_BY), name="ck_external_creatives_made_by"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_external_creatives_workspace_product",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_external_creatives_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id", "product_id", "upload_sha256", name="uq_external_creatives_content",
        ),
    )
    op.create_index(
        "ix_external_creatives_workspace_id", "external_creatives", ["workspace_id"], unique=False,
    )
    op.create_index(
        "ix_external_creatives_product", "external_creatives",
        ["workspace_id", "product_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_external_creatives_batch", "external_creatives", ["workspace_id", "batch_key_hash"], unique=False,
    )

    op.create_table(
        "external_creative_tag_confirmations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("external_creative_id", sa.String(length=64), nullable=False),
        sa.Column("asset_version_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=64), nullable=True),
        sa.Column("vocabulary_hash", sa.String(length=64), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("concepts_json", sa.JSON(), nullable=False),
        sa.Column("stable_dimension_signature", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("direction_key", sa.String(length=32), nullable=True),
        sa.Column("cells_json", sa.JSON(), nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_ectc_revision"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "external_creative_id"],
            ["external_creatives.workspace_id", "external_creatives.id"],
            name="fk_ectc_workspace_creative",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_ectc_workspace_asset_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_ectc_workspace_id_id"),
        sa.UniqueConstraint("external_creative_id", "revision", name="uq_ectc_creative_revision"),
    )
    op.create_index(
        "ix_external_creative_tag_confirmations_workspace_id", "external_creative_tag_confirmations",
        ["workspace_id"], unique=False,
    )
    op.create_index(
        "ix_ectc_asset_version", "external_creative_tag_confirmations",
        ["workspace_id", "asset_version_id", "revision"], unique=False,
    )

    if op.get_bind().dialect.name == "postgresql":
        for table, api_privileges, worker_privileges in GRANTS:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                "USING (marketai_context_is_current(workspace_id)) "
                "WITH CHECK (marketai_context_is_current(workspace_id))"
            )
            op.execute(f"GRANT {api_privileges} ON TABLE {table} TO marketai_domain_api")
            op.execute(f"GRANT {worker_privileges} ON TABLE {table} TO marketai_domain_worker")
            op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table, _api, _worker in reversed(GRANTS):
            op.execute(
                f"REVOKE ALL ON TABLE {table} FROM "
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            )
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_ectc_asset_version", table_name="external_creative_tag_confirmations")
    op.drop_index(
        "ix_external_creative_tag_confirmations_workspace_id", table_name="external_creative_tag_confirmations",
    )
    op.drop_table("external_creative_tag_confirmations")
    op.drop_index("ix_external_creatives_batch", table_name="external_creatives")
    op.drop_index("ix_external_creatives_product", table_name="external_creatives")
    op.drop_index("ix_external_creatives_workspace_id", table_name="external_creatives")
    op.drop_table("external_creatives")
    # External assets have no Run; they cannot survive a NOT NULL run_id.
    op.execute("DELETE FROM asset_versions WHERE asset_id IN (SELECT id FROM assets WHERE source = 'external')")
    op.execute("DELETE FROM assets WHERE source = 'external'")
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.drop_constraint("ck_assets_source_run", type_="check")
        batch_op.drop_constraint("ck_assets_source", type_="check")
        batch_op.alter_column("run_id", existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_column("source")
