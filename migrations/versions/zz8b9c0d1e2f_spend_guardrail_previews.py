"""Stored spend guardrail previews (Issue #386).

Revision ID: zz8b9c0d1e2f
Revises: zz7a8b9c0d1e

One new table and its grants; no existing table, constraint or row changes,
so the API and Worker of the previous release keep running while this
migrates:

* ``spend_guardrail_previews`` -- an Owner's previewed guardrail change. The
  API inserts it on ``preview_spend_guardrail`` and consumes it once on
  ``confirm_spend_guardrail``, through ``consumed_at`` and
  ``result_revision`` only (API: SELECT, INSERT, UPDATE of those two
  columns; Worker: nothing, as for ``spend_guardrails``; Readonly: SELECT;
  nobody DELETE). Before this the preview lived in one API process's memory.

FORCE'd RLS with the canonical ``tenant_isolation`` policy. The grants are
mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

Downgrade drops the table with its rows. A preview is a 15-minute proposal;
the change it confirmed stays in ``spend_guardrail_events``, which this
migration does not touch, and the previous release has no use for a stored
preview.
"""

import sqlalchemy as sa
from alembic import op


revision = "zz8b9c0d1e2f"
down_revision = "zz7a8b9c0d1e"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("spend_guardrail_previews",)

#: The only columns any runtime role may ever UPDATE: confirm consumes the
#: preview, nothing else about it is rewritten.
CONSUME_COLUMNS = ("consumed_at", "result_revision")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "spend_guardrail_previews",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("creator_id", sa.String(length=100), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("per_operation_limit", sa.Numeric(24, 12), nullable=False),
        sa.Column("rolling_24h_limit", sa.Numeric(24, 12), nullable=False),
        sa.Column("period_limit", sa.Numeric(24, 12), nullable=False),
        sa.Column("outstanding_exposure_limit", sa.Numeric(24, 12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_revision", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "per_operation_limit >= 0 AND rolling_24h_limit >= 0 "
            "AND period_limit >= 0 "
            "AND outstanding_exposure_limit >= 0",
            name=op.f("ck_spend_guardrail_previews_amounts_nonneg"),
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (result_revision IS NULL)",
            name=op.f("ck_spend_guardrail_previews_consumed_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name=op.f("fk_spend_guardrail_previews_workspace_id_workspaces"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_guardrail_previews")),
    )
    op.create_index(
        op.f("ix_spend_guardrail_previews_workspace_id"), "spend_guardrail_previews", ["workspace_id"],
    )

    if _is_postgres():
        table = "spend_guardrail_previews"
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (marketai_context_is_current(workspace_id)) "
            "WITH CHECK (marketai_context_is_current(workspace_id))"
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO marketai_domain_api")
        op.execute(f"GRANT UPDATE ({', '.join(CONSUME_COLUMNS)}) ON TABLE {table} TO marketai_domain_api")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")


def downgrade() -> None:
    if _is_postgres():
        table = "spend_guardrail_previews"
        op.execute(
            f"REVOKE ALL ON TABLE {table} FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index(op.f("ix_spend_guardrail_previews_workspace_id"), table_name="spend_guardrail_previews")
    op.drop_table("spend_guardrail_previews")
