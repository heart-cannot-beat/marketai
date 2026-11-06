"""Competitor list, resolves and append-only page confirmations (C-13, Issue #269).

Revision ID: zz1a2b3c4d5e
Revises: zy0f1a2b3c4d

New tables only, plus one widened CHECK:

* ``competitors`` -- current state (API: SELECT, INSERT, UPDATE for
  archiving; Worker: SELECT).
* ``competitor_resolutions`` -- current state of one paid resolve Job (API:
  SELECT, INSERT; Worker: SELECT, UPDATE as the Job advances).
* ``competitor_identity_confirmations`` -- append-only revisions (API and
  Worker: SELECT, INSERT; the Worker appends automatic matches). PostgreSQL
  refuses UPDATE and DELETE with the shared ``marketai_reject_update_or_delete``
  trigger; SQLite gets an equivalent trigger.

All three carry composite foreign keys and FORCE'd RLS. The grants are
mirrored in ``deploy/postgres/domain-tenant-roles.sql`` (a release revokes
and re-grants the whole matrix).

``provider_call_attempts.step_kind`` admits ``market_data``: each paid
market-data call (resolve now, scans in C-21) is a ProviderCallAttempt.
Widening only relaxes the CHECK, so code that predates it keeps working;
on PostgreSQL the new CHECK is added ``NOT VALID`` and then validated, which
holds the table's ACCESS EXCLUSIVE lock only for the catalog change, not
for the scan.

Downgrade refuses while any competitor row or market-data attempt exists.
"""

import sqlalchemy as sa
from alembic import op


revision = "zz1a2b3c4d5e"
down_revision = "zy0f1a2b3c4d"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = (
    "competitors",
    "competitor_resolutions",
    "competitor_identity_confirmations",
)

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("competitors", "SELECT, INSERT, UPDATE", "SELECT"),
    ("competitor_resolutions", "SELECT, INSERT", "SELECT, UPDATE"),
    ("competitor_identity_confirmations", "SELECT, INSERT", "SELECT, INSERT"),
)
APPEND_ONLY = "competitor_identity_confirmations"

_ATTEMPTS = "provider_call_attempts"
_STEP_KIND_CHECK = "ck_provider_call_attempts_ck_provider_call_attempt_step_kind"
_KINDS = (
    "'generation', 'visual_qc', 'source_analysis', 'translation', "
    "'localization_edit', 'localization_ocr', 'localization_visual_qc', 'llm_chat'"
)
_WIDENED = f"step_kind IN ({_KINDS}, 'market_data')"
_PREVIOUS = f"step_kind IN ({_KINDS})"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def _replace_step_kind_check(predicate: str) -> None:
    if _is_postgres():
        op.execute(f'ALTER TABLE {_ATTEMPTS} DROP CONSTRAINT IF EXISTS "{_STEP_KIND_CHECK}"')
        op.execute(f'ALTER TABLE {_ATTEMPTS} ADD CONSTRAINT "{_STEP_KIND_CHECK}" CHECK ({predicate}) NOT VALID')
        op.execute(f'ALTER TABLE {_ATTEMPTS} VALIDATE CONSTRAINT "{_STEP_KIND_CHECK}"')
        return
    with op.batch_alter_table(_ATTEMPTS, schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint(op.f(_STEP_KIND_CHECK), type_="check")
        batch_op.create_check_constraint(op.f(_STEP_KIND_CHECK), predicate)


def upgrade() -> None:
    _replace_step_kind_check(_WIDENED)

    op.create_table(
        "competitors",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("website_domain", sa.String(length=253), nullable=True),
        sa.Column("input_kind", sa.String(length=16), nullable=False),
        sa.Column("input_text", sa.String(length=2048), nullable=False),
        sa.Column("input_page_alias", sa.String(length=100), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_by", sa.String(length=100), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"role IN {_in(('competitor', 'self'))}", name=op.f("ck_competitors_role")),
        sa.CheckConstraint(f"status IN {_in(('active', 'archived'))}", name=op.f("ck_competitors_status")),
        sa.CheckConstraint(
            f"input_kind IN {_in(('name', 'facebook_page', 'ad_library'))}", name=op.f("ck_competitors_input_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitors_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
            name="fk_competitors_workspace_product", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitors")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitors_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_competitors_idempotency"),
    )
    op.create_index("ix_competitors_workspace_id", "competitors", ["workspace_id"], unique=False)
    op.create_index("ix_competitors_product", "competitors", ["workspace_id", "product_id", "created_at"])

    op.create_table(
        "competitor_resolutions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("competitor_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("credential_version_id", sa.String(length=64), nullable=True),
        sa.Column("query", sa.String(length=200), nullable=False),
        sa.Column("website_domain", sa.String(length=253), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("quote_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("cost_json", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN {_in(('queued', 'running', 'completed', 'failed', 'uncertain'))}",
            name=op.f("ck_competitor_resolutions_status"),
        ),
        sa.CheckConstraint(
            f"execution_mode IN {_in(('byok', 'mock'))}", name=op.f("ck_competitor_resolutions_execution_mode"),
        ),
        sa.CheckConstraint(
            "(execution_mode = 'byok') = (credential_version_id IS NOT NULL)",
            name=op.f("ck_competitor_resolutions_byok_credential"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_competitor_resolutions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "competitor_id"], ["competitors.workspace_id", "competitors.id"],
            name="fk_competitor_resolutions_competitor", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"],
            name="fk_competitor_resolutions_job", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"],
            name="fk_competitor_resolutions_operation", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "credential_version_id"], ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_competitor_resolutions_credential_version", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_resolutions")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_resolutions_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "job_id", name="uq_competitor_resolutions_job"),
        sa.UniqueConstraint(
            "workspace_id", "competitor_id", "idempotency_key_hash", name="uq_competitor_resolutions_idempotency",
        ),
    )
    op.create_index("ix_competitor_resolutions_workspace_id", "competitor_resolutions", ["workspace_id"])
    op.create_index(
        "ix_competitor_resolutions_competitor", "competitor_resolutions",
        ["workspace_id", "competitor_id", "created_at"],
    )
    op.create_index(
        "ix_competitor_resolutions_credential", "competitor_resolutions",
        ["workspace_id", "credential_version_id", "status"],
    )

    op.create_table(
        "competitor_identity_confirmations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("competitor_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("page_id", sa.String(length=32), nullable=False),
        sa.Column("page_alias", sa.String(length=100), nullable=True),
        sa.Column("page_name", sa.String(length=300), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("resolve_job_id", sa.String(length=64), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by", sa.String(length=100), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_competitor_identity_confirmations_revision")),
        sa.CheckConstraint("platform = 'meta'", name=op.f("ck_competitor_identity_confirmations_platform")),
        sa.CheckConstraint(
            f"source IN {_in(('link', 'auto', 'user'))}", name=op.f("ck_competitor_identity_confirmations_source"),
        ),
        sa.CheckConstraint(
            "(source = 'link') = (resolve_job_id IS NULL)",
            name=op.f("ck_competitor_identity_confirmations_source_resolution"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name=op.f("fk_competitor_identity_confirmations_workspace_id_workspaces"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "competitor_id"], ["competitors.workspace_id", "competitors.id"],
            name="fk_competitor_identity_confirmations_competitor", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "resolve_job_id"],
            ["competitor_resolutions.workspace_id", "competitor_resolutions.job_id"],
            name="fk_competitor_identity_confirmations_resolution", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_identity_confirmations")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_competitor_identity_confirmations_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id", "competitor_id", "revision", name="uq_competitor_identity_confirmations_revision",
        ),
        sa.UniqueConstraint(
            "workspace_id", "competitor_id", "idempotency_key_hash",
            name="uq_competitor_identity_confirmations_idempotency",
        ),
    )
    op.create_index(
        "ix_competitor_identity_confirmations_workspace_id", "competitor_identity_confirmations", ["workspace_id"],
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
        op.execute(
            f"CREATE TRIGGER trg_{APPEND_ONLY}_append_only BEFORE UPDATE OR DELETE ON {APPEND_ONLY} "
            "FOR EACH ROW EXECUTE FUNCTION marketai_reject_update_or_delete()"
        )
    else:
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{APPEND_ONLY}_no_{event.lower()} BEFORE {event} ON {APPEND_ONLY} "
                f"BEGIN SELECT RAISE(ABORT, '{event} denied on append-only table {APPEND_ONLY}'); END"
            )


def downgrade() -> None:
    bind = op.get_bind()
    in_use = bind.execute(sa.text("SELECT 1 FROM competitors LIMIT 1")).first() or bind.execute(
        sa.text(f"SELECT 1 FROM {_ATTEMPTS} WHERE step_kind = 'market_data' LIMIT 1")
    ).first()
    if in_use is not None:
        raise RuntimeError(
            "cannot downgrade zz1a2b3c4d5e: competitors or market-data provider attempts exist"
        )
    if _is_postgres():
        op.execute(f"DROP TRIGGER IF EXISTS trg_{APPEND_ONLY}_append_only ON {APPEND_ONLY}")
        for table, _api, _worker in reversed(GRANTS):
            op.execute(
                f"REVOKE ALL ON TABLE {table} FROM "
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            )
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    else:
        for event in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{APPEND_ONLY}_no_{event}")
    op.drop_index("ix_competitor_identity_confirmations_workspace_id", table_name="competitor_identity_confirmations")
    op.drop_table("competitor_identity_confirmations")
    op.drop_index("ix_competitor_resolutions_credential", table_name="competitor_resolutions")
    op.drop_index("ix_competitor_resolutions_competitor", table_name="competitor_resolutions")
    op.drop_index("ix_competitor_resolutions_workspace_id", table_name="competitor_resolutions")
    op.drop_table("competitor_resolutions")
    op.drop_index("ix_competitors_product", table_name="competitors")
    op.drop_index("ix_competitors_workspace_id", table_name="competitors")
    op.drop_table("competitors")
    _replace_step_kind_check(_PREVIOUS)
