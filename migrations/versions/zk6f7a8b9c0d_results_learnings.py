"""Results loop: ad-level result imports, observations and learning snapshots.

Revision ID: zk6f7a8b9c0d
Revises: zj5e6f7a8b9c

Issue #166 (front-flow contract §3.3, §5.4, §7-3).

* ``result_imports`` -- one CSV / manual / Meta-sync import: the parsed rows
  with their match preview, a fingerprint of the normalized rows (unique per
  product, so the same file is never imported twice), then ``committed``.
* ``result_observations`` -- the committed rows, attributed to an
  AssetVersion (and optionally a Meta ad) over a date range, in an evidence
  partition (``production`` / ``sandbox`` / ``mock``). Append-only.
* ``learning_snapshots`` -- one immutable learnings computation per product
  and partition, keyed by the hash of every input it read.

Tenancy: RLS ``tenant_isolation`` on all three. Grants: the API reads all
three, inserts all three and updates only ``result_imports`` (preview ->
committed); observations and snapshots are append-only (no UPDATE or
DELETE for any role). The Worker gets nothing (no Job reads them); readonly
reads.
"""
from alembic import op
import sqlalchemy as sa


revision = "zk6f7a8b9c0d"
down_revision = "zj5e6f7a8b9c"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = (
    "result_imports",
    "result_observations",
    "learning_snapshots",
)

#: (table, API privileges); readonly gets SELECT on all, the Worker nothing.
GRANTS = (
    ("result_imports", "SELECT, INSERT, UPDATE"),
    ("result_observations", "SELECT, INSERT"),
    ("learning_snapshots", "SELECT, INSERT"),
)

SOURCES = ("manual", "csv", "meta_sync")
MODES = ("production", "sandbox", "mock")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "result_imports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=300), nullable=False),
        sa.Column("upload_intent_id", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("rows_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("snapshot_ids_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("committed_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source IN " + _in(SOURCES), name="ck_result_imports_source"),
        sa.CheckConstraint("status IN " + _in(("previewed", "committed")), name="ck_result_imports_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_result_imports_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "product_id", "fingerprint", name="uq_result_imports_fingerprint"),
        sa.UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_result_imports_idempotency"),
    )
    op.create_index("ix_result_imports_workspace_id", "result_imports", ["workspace_id"], unique=False)
    op.create_index(
        "ix_result_imports_product", "result_imports", ["workspace_id", "product_id", "created_at"], unique=False,
    )

    op.create_table(
        "result_observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("import_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("evidence_mode", sa.String(length=16), nullable=False),
        sa.Column("asset_version_id", sa.String(length=64), nullable=False),
        sa.Column("ad_id", sa.String(length=100), nullable=False),
        sa.Column("date_start", sa.Date(), nullable=False),
        sa.Column("date_end", sa.Date(), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=False),
        sa.Column("clicks", sa.BigInteger(), nullable=False),
        sa.Column("spend", sa.Numeric(20, 6), nullable=True),
        sa.Column("conversions", sa.Numeric(20, 6), nullable=True),
        sa.Column("revenue", sa.Numeric(20, 6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("row_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN " + _in(SOURCES), name="ck_result_observations_source"),
        sa.CheckConstraint("evidence_mode IN " + _in(MODES), name="ck_result_observations_mode"),
        sa.CheckConstraint(
            "impressions >= 0 AND clicks >= 0 AND clicks <= impressions "
            "AND (spend IS NULL OR spend >= 0) AND (conversions IS NULL OR conversions >= 0) "
            "AND (revenue IS NULL OR revenue >= 0) AND date_end >= date_start",
            name="ck_result_observations_values",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "import_id"],
            ["result_imports.workspace_id", "result_imports.id"],
            name="fk_result_observations_workspace_import",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_result_observations_workspace_id", "result_observations", ["workspace_id"], unique=False,
    )
    op.create_index(
        "ix_result_observations_import_id", "result_observations", ["import_id"], unique=False,
    )
    op.create_index(
        "ix_result_observations_product", "result_observations",
        ["workspace_id", "product_id", "evidence_mode"], unique=False,
    )

    op.create_table(
        "learning_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_mode", sa.String(length=16), nullable=False),
        sa.Column("algorithm_version", sa.String(length=32), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("trigger_import_id", sa.String(length=64), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("evidence_mode IN " + _in(MODES), name="ck_learning_snapshots_mode"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_snapshots_workspace_id_id"),
    )
    op.create_index(
        "ix_learning_snapshots_workspace_id", "learning_snapshots", ["workspace_id"], unique=False,
    )
    op.create_index(
        "ix_learning_snapshots_product", "learning_snapshots",
        ["workspace_id", "product_id", "evidence_mode", "created_at"], unique=False,
    )

    if op.get_bind().dialect.name == "postgresql":
        for table, api_privileges in GRANTS:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                "USING (marketai_context_is_current(workspace_id)) "
                "WITH CHECK (marketai_context_is_current(workspace_id))"
            )
            op.execute(f"GRANT {api_privileges} ON TABLE {table} TO marketai_domain_api")
            op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table, _api in reversed(GRANTS):
            op.execute(
                f"REVOKE ALL ON TABLE {table} FROM "
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            )
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_learning_snapshots_product", table_name="learning_snapshots")
    op.drop_index("ix_learning_snapshots_workspace_id", table_name="learning_snapshots")
    op.drop_table("learning_snapshots")
    op.drop_index("ix_result_observations_product", table_name="result_observations")
    op.drop_index("ix_result_observations_import_id", table_name="result_observations")
    op.drop_index("ix_result_observations_workspace_id", table_name="result_observations")
    op.drop_table("result_observations")
    op.drop_index("ix_result_imports_product", table_name="result_imports")
    op.drop_index("ix_result_imports_workspace_id", table_name="result_imports")
    op.drop_table("result_imports")
