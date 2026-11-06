"""Competitor scans, append-only observations and their origins (C-21, Issue #270).

Revision ID: zz2b3c4d5e6f
Revises: zz1a2b3c4d5e

New tables and grants only; no existing table, constraint or row changes,
so the API and Worker of the previous release keep running while this
migrates (the Release service migrates in parallel with them, #283):

* ``competitor_scans`` -- current state of one paid scan (API: SELECT,
  INSERT, UPDATE -- a resume or an Owner's reconciliation re-admits it;
  Worker: SELECT, UPDATE as the Job advances).
* ``competitor_ads`` -- append-only observations (API: SELECT; Worker:
  SELECT, INSERT).
* ``competitor_ad_origins`` -- append-only origins of each observation
  (API: SELECT; Worker: SELECT, INSERT).

PostgreSQL refuses UPDATE and DELETE on the two append-only tables with the
shared ``marketai_reject_update_or_delete`` trigger; SQLite gets an
equivalent trigger. All three carry composite foreign keys and FORCE'd RLS.
The grants are mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

Each page a scan pays for is a ``provider_call_attempts`` row with
``step_kind='market_data'``, which zz1a2b3c4d5e already admits.

Downgrade refuses while any scan exists.
"""

import sqlalchemy as sa
from alembic import op


revision = "zz2b3c4d5e6f"
down_revision = "zz1a2b3c4d5e"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = (
    "competitor_scans",
    "competitor_ads",
    "competitor_ad_origins",
)

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("competitor_scans", "SELECT, INSERT, UPDATE", "SELECT, UPDATE"),
    ("competitor_ads", "SELECT", "SELECT, INSERT"),
    ("competitor_ad_origins", "SELECT", "SELECT, INSERT"),
)
APPEND_ONLY = ("competitor_ads", "competitor_ad_origins")

_STATUSES = ("queued", "running", "completed", "partial", "failed", "uncertain")
_STAGES = ("fetch", "media", "filter", "tag", "cluster", "freeze")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "competitor_scans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=2), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=True),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("initial_credential_version_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("cost_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN {_in(_STATUSES)}", name=op.f("ck_competitor_scans_status")),
        sa.CheckConstraint(f"stage IS NULL OR stage IN {_in(_STAGES)}", name=op.f("ck_competitor_scans_stage")),
        sa.CheckConstraint(
            f"execution_mode IN {_in(('byok', 'mock'))}", name=op.f("ck_competitor_scans_execution_mode"),
        ),
        sa.CheckConstraint("platform = 'meta'", name=op.f("ck_competitor_scans_platform")),
        sa.CheckConstraint(
            "(execution_mode = 'byok') = (initial_credential_version_id IS NOT NULL)",
            name=op.f("ck_competitor_scans_byok_credential"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_scans_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
            name="fk_competitor_scans_product", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"],
            name="fk_competitor_scans_job", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"],
            name="fk_competitor_scans_operation", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "initial_credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_competitor_scans_credential_version", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_scans")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_scans_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_competitor_scans_idempotency"),
    )
    op.create_index("ix_competitor_scans_workspace_id", "competitor_scans", ["workspace_id"])
    op.create_index("ix_competitor_scans_product", "competitor_scans", ["workspace_id", "product_id", "created_at"])
    op.create_index("ix_competitor_scans_status", "competitor_scans", ["workspace_id", "status"])

    op.create_table(
        "competitor_ads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("ad_archive_id", sa.String(length=32), nullable=False),
        sa.Column("collation_id", sa.String(length=32), nullable=True),
        sa.Column("collation_count", sa.Integer(), nullable=True),
        sa.Column("advertiser_page_id", sa.String(length=32), nullable=True),
        sa.Column("advertiser_name", sa.String(length=300), nullable=True),
        sa.Column("page_profile_uri", sa.String(length=2048), nullable=True),
        sa.Column("page_categories_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("days_running", sa.Integer(), nullable=True),
        sa.Column("display_format", sa.String(length=32), nullable=True),
        sa.Column("publisher_platforms_json", sa.JSON(), nullable=False),
        sa.Column("cards_json", sa.JSON(), nullable=False),
        sa.Column("landing_domain", sa.String(length=253), nullable=True),
        sa.Column("library_url", sa.String(length=2048), nullable=True),
        sa.Column("media_json", sa.JSON(), nullable=False),
        sa.Column("raw_ref", sa.String(length=300), nullable=True),
        sa.Column("field_sources_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_ads_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scan_id"], ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_ads_scan", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_ads")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_ads_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "scan_id", "ad_archive_id", name="uq_competitor_ads_scan_ad"),
    )
    op.create_index("ix_competitor_ads_workspace_id", "competitor_ads", ["workspace_id"])

    op.create_table(
        "competitor_ad_origins",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("competitor_ad_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=200), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_ad_origins_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "competitor_ad_id"], ["competitor_ads.workspace_id", "competitor_ads.id"],
            name="fk_competitor_ad_origins_ad", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scan_id"], ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_ad_origins_scan", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"], ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_competitor_ad_origins_attempt", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_ad_origins")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_ad_origins_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "competitor_ad_id", "origin", name="uq_competitor_ad_origins_origin"),
    )
    op.create_index("ix_competitor_ad_origins_workspace_id", "competitor_ad_origins", ["workspace_id"])
    op.create_index(
        "ix_competitor_ad_origins_scan", "competitor_ad_origins", ["workspace_id", "scan_id", "origin"],
    )

    if _is_postgres():
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
        for table in APPEND_ONLY:
            op.execute(
                f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION marketai_reject_update_or_delete()"
            )
    else:
        for table in APPEND_ONLY:
            for event in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER trg_{table}_no_{event.lower()} BEFORE {event} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{event} denied on append-only table {table}'); END"
                )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM competitor_scans LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade zz2b3c4d5e6f: competitor scans exist")
    if _is_postgres():
        for table in APPEND_ONLY:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        for table, _api, _worker in reversed(GRANTS):
            op.execute(
                f"REVOKE ALL ON TABLE {table} FROM "
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            )
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    else:
        for table in APPEND_ONLY:
            for event in ("update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_{event}")
    op.drop_index("ix_competitor_ad_origins_scan", table_name="competitor_ad_origins")
    op.drop_index("ix_competitor_ad_origins_workspace_id", table_name="competitor_ad_origins")
    op.drop_table("competitor_ad_origins")
    op.drop_index("ix_competitor_ads_workspace_id", table_name="competitor_ads")
    op.drop_table("competitor_ads")
    op.drop_index("ix_competitor_scans_status", table_name="competitor_scans")
    op.drop_index("ix_competitor_scans_product", table_name="competitor_scans")
    op.drop_index("ix_competitor_scans_workspace_id", table_name="competitor_scans")
    op.drop_table("competitor_scans")
