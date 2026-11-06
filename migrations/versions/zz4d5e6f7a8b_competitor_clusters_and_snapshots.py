"""Competitor buying-claim clusters, snapshots and the drafting switch (C-23, Issue #272).

Revision ID: zz4d5e6f7a8b
Revises: zz3c4d5e6f7a

Two new tables, one new column with a default, and their grants; no
existing table's constraints or rows change, so the API and Worker of the
previous release keep running while this migrates (the Release service
migrates in parallel with them, #283):

* ``competitor_scan_assessments`` -- revisions of a scan's buying-claim
  clusters (``kind='clusters'``, ``source='ai'``). The Worker appends them
  (the scan's cluster step and a paid recluster's Job); the API reads them
  (API: SELECT; Worker: SELECT, INSERT).
* ``competitor_scan_snapshots`` -- pure-function freezes, unique per
  (Workspace, scan, hash). The Worker freezes at the end of a run or a
  recluster, the API on a member's refreeze (API and Worker: SELECT,
  INSERT).
* ``products.competitor_snapshot_in_drafts`` -- boolean, ``NOT NULL``,
  server default ``true``: rows of the previous release's inserts get the
  default. The API already holds UPDATE on ``products``.

Tag revisions go into ``competitor_ad_assessments`` (``kind='tags'``,
already admitted by its CHECK since ``zz3c4d5e6f7a``); nothing changes
there.

PostgreSQL refuses UPDATE and DELETE on both new tables with the shared
``marketai_reject_update_or_delete`` trigger; SQLite gets an equivalent
trigger. Composite foreign keys to the scan, the Job, the ``llm_chat``
attempt, the product and the cited clusters revision; FORCE'd RLS. The
grants are mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

Downgrade refuses while any cluster revision or snapshot exists; it drops
the column (its values are re-derivable defaults until C-32 lets members
change them).
"""

import sqlalchemy as sa
from alembic import op


revision = "zz4d5e6f7a8b"
down_revision = "zz3c4d5e6f7a"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("competitor_scan_assessments", "competitor_scan_snapshots")

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("competitor_scan_assessments", "SELECT", "SELECT, INSERT"),
    ("competitor_scan_snapshots", "SELECT, INSERT", "SELECT, INSERT"),
)
APPEND_ONLY = ("competitor_scan_assessments", "competitor_scan_snapshots")

_KINDS = ("clusters",)
_SOURCES = ("ai",)
_MODES = ("byok", "mock")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "competitor_scan_assessments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("model_evidence_json", sa.JSON(), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_competitor_scan_assessments_revision")),
        sa.CheckConstraint(f"kind IN {_in(_KINDS)}", name=op.f("ck_competitor_scan_assessments_kind")),
        sa.CheckConstraint(f"source IN {_in(_SOURCES)}", name=op.f("ck_competitor_scan_assessments_source")),
        sa.CheckConstraint(
            "(source = 'ai') = (attempt_id IS NOT NULL)", name=op.f("ck_competitor_scan_assessments_ai_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_scan_assessments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scan_id"], ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_scan_assessments_scan", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"],
            name="fk_competitor_scan_assessments_job", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"], ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_competitor_scan_assessments_attempt", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_scan_assessments")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_scan_assessments_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id", "scan_id", "kind", "revision", name="uq_competitor_scan_assessments_revision",
        ),
        sa.UniqueConstraint(
            "workspace_id", "scan_id", "kind", "idempotency_key_hash",
            name="uq_competitor_scan_assessments_idempotency",
        ),
    )
    op.create_index("ix_competitor_scan_assessments_workspace_id", "competitor_scan_assessments", ["workspace_id"])
    op.create_index(
        "ix_competitor_scan_assessments_scan", "competitor_scan_assessments", ["workspace_id", "scan_id", "kind"],
    )

    op.create_table(
        "competitor_scan_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=2), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("cluster_assessment_id", sa.String(length=64), nullable=True),
        sa.Column("cluster_revision", sa.Integer(), nullable=True),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"execution_mode IN {_in(_MODES)}", name=op.f("ck_competitor_scan_snapshots_execution_mode"),
        ),
        sa.CheckConstraint(
            "(cluster_revision IS NULL) = (cluster_assessment_id IS NULL)",
            name=op.f("ck_competitor_scan_snapshots_cluster_reference"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_scan_snapshots_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scan_id"], ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_scan_snapshots_scan", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
            name="fk_competitor_scan_snapshots_product", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cluster_assessment_id"],
            ["competitor_scan_assessments.workspace_id", "competitor_scan_assessments.id"],
            name="fk_competitor_scan_snapshots_clusters", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_scan_snapshots")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_scan_snapshots_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "scan_id", "hash", name="uq_competitor_scan_snapshots_hash"),
    )
    op.create_index("ix_competitor_scan_snapshots_workspace_id", "competitor_scan_snapshots", ["workspace_id"])
    op.create_index(
        "ix_competitor_scan_snapshots_market", "competitor_scan_snapshots",
        ["workspace_id", "product_id", "market", "created_at"],
    )

    # A plain ADD COLUMN with a constant default: no table rewrite on
    # PostgreSQL 11+, and SQLite needs no batch copy of ``products``.
    op.add_column("products", sa.Column(
        "competitor_snapshot_in_drafts", sa.Boolean(), nullable=False, server_default=sa.true(),
    ))

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
    for table in APPEND_ONLY:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(f"cannot downgrade zz4d5e6f7a8b: {table} has rows")
    op.drop_column("products", "competitor_snapshot_in_drafts")
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
    op.drop_index("ix_competitor_scan_snapshots_market", table_name="competitor_scan_snapshots")
    op.drop_index("ix_competitor_scan_snapshots_workspace_id", table_name="competitor_scan_snapshots")
    op.drop_table("competitor_scan_snapshots")
    op.drop_index("ix_competitor_scan_assessments_scan", table_name="competitor_scan_assessments")
    op.drop_index("ix_competitor_scan_assessments_workspace_id", table_name="competitor_scan_assessments")
    op.drop_table("competitor_scan_assessments")
