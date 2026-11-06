"""Product intake: paste a URL, images or text and get a confirmable product card.

Revision ID: zi4d5e6f7a8b
Revises: zh3c4d5e6f7a

Issue #161 (front-flow contract §3.1). One ``product_intakes`` row per
intake: the normalized input and its fingerprint (24h replay), the durable
Operation/Job ids, the per-step evidence (fetch digests, extraction outcome,
image candidates and their reference-gate results), the AI-drafted product
card and, once the user confirms, the published Library entry/version and
the bridged ``prod_`` id. Raw pages are never stored -- only digests and the
bounded summary the extraction step used.

Tenancy: RLS ``tenant_isolation`` like every Workspace-owned table. Grants:
the API creates intakes, reads them and records the confirmation; the Worker
reads and advances them (no INSERT); readonly reads. No role gets DELETE.

The Worker also creates and gates the intake's images as
``reference_objects`` (it already updates them in the reference gate, #158),
so this revision grants it INSERT/UPDATE there too; the role file repeats the
grants (``deploy/postgres/domain-tenant-roles.sql``) because it re-converges
every runtime grant on each release.
"""
from alembic import op
import sqlalchemy as sa


revision = "zi4d5e6f7a8b"
down_revision = "zh3c4d5e6f7a"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("product_intakes",)

STATUSES = (
    "queued", "fetching", "extracting", "preparing_images",
    "ready", "fetch_failed", "extraction_failed", "failed", "confirmed",
)


def upgrade() -> None:
    op.create_table(
        "product_intakes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_step", sa.String(length=32), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("upload_intent_ids", sa.JSON(), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("extraction_json", sa.JSON(), nullable=True),
        sa.Column("images_json", sa.JSON(), nullable=False),
        sa.Column("card_json", sa.JSON(), nullable=True),
        sa.Column("confirmation_json", sa.JSON(), nullable=True),
        sa.Column("product_entry_id", sa.String(length=64), nullable=True),
        sa.Column("product_version_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("initiator_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in STATUSES) + ")",
            name="product_intake_status",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('mock', 'byok')",
            name="product_intake_execution_mode",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key_hash", name="uq_product_intake_idempotency",
        ),
    )
    op.create_index(
        "ix_product_intakes_workspace_id", "product_intakes", ["workspace_id"], unique=False,
    )
    op.create_index(
        "ix_product_intakes_fingerprint",
        "product_intakes",
        ["workspace_id", "input_fingerprint", "created_at"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "product_intakes" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "product_intakes" FORCE ROW LEVEL SECURITY')
        op.execute(
            "CREATE POLICY tenant_isolation ON product_intakes "
            "USING (marketai_context_is_current(workspace_id)) "
            "WITH CHECK (marketai_context_is_current(workspace_id))"
        )
        op.execute(
            "GRANT SELECT, INSERT, UPDATE ON TABLE product_intakes TO marketai_domain_api"
        )
        op.execute(
            "GRANT SELECT, UPDATE ON TABLE product_intakes TO marketai_domain_worker"
        )
        op.execute("GRANT SELECT ON TABLE product_intakes TO marketai_domain_readonly")
        op.execute(
            "GRANT INSERT, UPDATE ON TABLE reference_objects TO marketai_domain_worker"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "REVOKE INSERT, UPDATE ON TABLE reference_objects FROM marketai_domain_worker"
        )
        op.execute(
            "REVOKE ALL ON TABLE product_intakes FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON product_intakes")
        op.execute('ALTER TABLE "product_intakes" NO FORCE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "product_intakes" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_product_intakes_fingerprint", table_name="product_intakes")
    op.drop_index("ix_product_intakes_workspace_id", table_name="product_intakes")
    op.drop_table("product_intakes")
