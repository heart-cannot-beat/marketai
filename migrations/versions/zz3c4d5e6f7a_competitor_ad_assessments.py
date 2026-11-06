"""Append-only judgments of competitor observations (C-22, Issue #271).

Revision ID: zz3c4d5e6f7a
Revises: zz2b3c4d5e6f

One new table and its grants; no existing table, constraint or row changes,
so the API and Worker of the previous release keep running while this
migrates (the Release service migrates in parallel with them, #283):

* ``competitor_ad_assessments`` -- revisions of an observation's judgment
  (``kind='relevance'`` here; ``tags`` is C-23's). The Worker appends the
  scan's AI and rule judgments; the API appends a member's flip. Both read
  (API: SELECT, INSERT; Worker: SELECT, INSERT).

PostgreSQL refuses UPDATE and DELETE with the shared
``marketai_reject_update_or_delete`` trigger; SQLite gets an equivalent
trigger. Composite foreign keys to the observation, its scan and (for an
``ai`` revision) the ``llm_chat`` attempt; FORCE'd RLS. The grants are
mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

Downgrade refuses while any assessment exists.
"""

import sqlalchemy as sa
from alembic import op


revision = "zz3c4d5e6f7a"
down_revision = "zz2b3c4d5e6f"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("competitor_ad_assessments",)

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("competitor_ad_assessments", "SELECT, INSERT", "SELECT, INSERT"),
)
APPEND_ONLY = ("competitor_ad_assessments",)

_KINDS = ("relevance", "tags")
_SOURCES = ("ai", "rule", "user")
_RELEVANCE = ("relevant", "adjacent", "unrelated")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "competitor_ad_assessments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("competitor_ad_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("relevance", sa.String(length=16), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("model_evidence_json", sa.JSON(), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_competitor_ad_assessments_revision")),
        sa.CheckConstraint(f"kind IN {_in(_KINDS)}", name=op.f("ck_competitor_ad_assessments_kind")),
        sa.CheckConstraint(f"source IN {_in(_SOURCES)}", name=op.f("ck_competitor_ad_assessments_source")),
        sa.CheckConstraint(
            f"relevance IS NULL OR relevance IN {_in(_RELEVANCE)}",
            name=op.f("ck_competitor_ad_assessments_relevance"),
        ),
        sa.CheckConstraint(
            "(kind = 'relevance') = (relevance IS NOT NULL)",
            name=op.f("ck_competitor_ad_assessments_relevance_kind"),
        ),
        sa.CheckConstraint(
            "(source = 'ai') = (attempt_id IS NOT NULL)", name=op.f("ck_competitor_ad_assessments_ai_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_ad_assessments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "competitor_ad_id"], ["competitor_ads.workspace_id", "competitor_ads.id"],
            name="fk_competitor_ad_assessments_ad", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scan_id"], ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_ad_assessments_scan", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"], ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_competitor_ad_assessments_attempt", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_ad_assessments")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_ad_assessments_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id", "competitor_ad_id", "kind", "revision", name="uq_competitor_ad_assessments_revision",
        ),
        sa.UniqueConstraint(
            "workspace_id", "competitor_ad_id", "kind", "idempotency_key_hash",
            name="uq_competitor_ad_assessments_idempotency",
        ),
    )
    op.create_index("ix_competitor_ad_assessments_workspace_id", "competitor_ad_assessments", ["workspace_id"])
    op.create_index(
        "ix_competitor_ad_assessments_scan", "competitor_ad_assessments", ["workspace_id", "scan_id", "kind"],
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
    if bind.execute(sa.text("SELECT 1 FROM competitor_ad_assessments LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade zz3c4d5e6f7a: competitor assessments exist")
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
    op.drop_index("ix_competitor_ad_assessments_scan", table_name="competitor_ad_assessments")
    op.drop_index("ix_competitor_ad_assessments_workspace_id", table_name="competitor_ad_assessments")
    op.drop_table("competitor_ad_assessments")
