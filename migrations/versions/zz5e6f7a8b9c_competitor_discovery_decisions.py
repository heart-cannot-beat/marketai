"""Append-only competitor discovery decisions (C-33, Issue #276).

Revision ID: zz5e6f7a8b9c
Revises: zz4d5e6f7a8b

One new table and its grants; no existing table, constraint or row changes,
so the API and Worker of the previous release keep running while this
migrates (the Release service migrates in parallel with them, #283):

* ``competitor_discovery_decisions`` -- a member's accept or reject of an
  advertiser a scan's discover step recommended, per product (spec §8,
  §11). The API appends it (an accept also creates the competitor and its
  link confirmation); the Worker reads it to leave rejected advertisers
  out of later scans (API: SELECT, INSERT; Worker: SELECT).

PostgreSQL refuses UPDATE and DELETE with the shared
``marketai_reject_update_or_delete`` trigger; SQLite gets an equivalent
trigger. Composite foreign keys to the product, the scan and (for an
accept) the competitor; FORCE'd RLS. The grants are mirrored in
``deploy/postgres/domain-tenant-roles.sql``.

Downgrade refuses while any decision exists.
"""

import sqlalchemy as sa
from alembic import op


revision = "zz5e6f7a8b9c"
down_revision = "zz4d5e6f7a8b"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("competitor_discovery_decisions",)

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("competitor_discovery_decisions", "SELECT, INSERT", "SELECT"),
)
APPEND_ONLY = ("competitor_discovery_decisions",)

_DECISIONS = ("accept", "reject")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "competitor_discovery_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("advertiser_page_id", sa.String(length=64), nullable=False),
        sa.Column("advertiser_name", sa.String(length=300), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("competitor_id", sa.String(length=64), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"decision IN {_in(_DECISIONS)}", name=op.f("ck_competitor_discovery_decisions_decision"),
        ),
        sa.CheckConstraint(
            "(decision = 'accept') = (competitor_id IS NOT NULL)",
            name=op.f("ck_competitor_discovery_decisions_accept_competitor"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name=op.f("fk_competitor_discovery_decisions_workspace_id_workspaces"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
            name="fk_competitor_discovery_decisions_product", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scan_id"], ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_discovery_decisions_scan", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "competitor_id"], ["competitors.workspace_id", "competitors.id"],
            name="fk_competitor_discovery_decisions_competitor", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_discovery_decisions")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_discovery_decisions_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id", "product_id", "idempotency_key_hash",
            name="uq_competitor_discovery_decisions_idempotency",
        ),
    )
    op.create_index(
        "ix_competitor_discovery_decisions_workspace_id", "competitor_discovery_decisions", ["workspace_id"],
    )
    op.create_index(
        "ix_competitor_discovery_decisions_page", "competitor_discovery_decisions",
        ["workspace_id", "product_id", "advertiser_page_id", "decided_at"],
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
    if bind.execute(sa.text("SELECT 1 FROM competitor_discovery_decisions LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade zz5e6f7a8b9c: competitor discovery decisions exist")
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
    op.drop_index("ix_competitor_discovery_decisions_page", table_name="competitor_discovery_decisions")
    op.drop_index("ix_competitor_discovery_decisions_workspace_id", table_name="competitor_discovery_decisions")
    op.drop_table("competitor_discovery_decisions")
