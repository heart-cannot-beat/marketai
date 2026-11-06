"""library_ui_gate0_contracts

Gate 0.2 persistence model for the Creative Library workspace UI (#47).

1. Extends ``library_idempotency`` with contract schema version, opaque
   command-identity hash, lifecycle state, response kind, and
   updated/reconciled timestamps.

2. Creates ``library_command_impacts`` for server-computed Publish /
   Withdraw / Archive impact projections that the browser must present
   before executing a destructive command.

3. Creates ``library_entry_contexts`` for contextual navigation
   associations that let internal resource entries resolve their
   parent/resource navigation path. Multiple legal contexts per entry
   are allowed.

4. Creates ``library_stepup_assertions`` for one-use MFA step-up JTI
   consumption (hashed, never raw).

5. Adds ``content_schema_version`` to ``library_drafts`` and
   ``library_versions``.

6. Adds ``lifecycle_revision`` to ``library_versions``.

7. Backfills existing rows with legacy contract version 1 and derives
   idempotency lifecycle state from ``response_json``.

Revision ID: r1a2b3c4d5e6
Revises: q1a2b3c4d5e6
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "r1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "q1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_WORKSPACE_OWNED = (
    "library_command_impacts",
    "library_entry_contexts",
    "library_stepup_assertions",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ---- 1. Extend library_idempotency ---------------------------------
    with op.batch_alter_table("library_idempotency", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("contract_schema_version", sa.Integer(),
                       nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("command_identity_hash", sa.String(64),
                       nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("lifecycle_state", sa.String(20),
                       nullable=False, server_default="pending")
        )
        batch_op.add_column(
            sa.Column("response_kind", sa.String(20),
                       nullable=False, server_default="pending")
        )
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_library_idempotency_lifecycle_state",
            "lifecycle_state IN ('pending', 'terminal')",
        )
        batch_op.create_check_constraint(
            "ck_library_idempotency_response_kind",
            "response_kind IN ('success', 'problem', 'not_applied', 'pending')",
        )

    # Backfill existing idempotency rows: set lifecycle_state and
    # response_kind based on response_json content, and updated_at = created_at.
    conn = op.get_bind()
    if _is_postgres():
        pending_evidence = (
            "COALESCE(response_json ->> '_state', '') = 'pending' "
            "OR response_json::jsonb = '{}'::jsonb"
        )
        problem_evidence = "COALESCE(response_json ->> '_terminal', '') <> ''"
    else:
        pending_evidence = (
            "COALESCE(json_extract(response_json, '$._state'), '') = 'pending' "
            "OR response_json = '{}'"
        )
        problem_evidence = (
            "COALESCE(json_extract(response_json, '$._terminal'), '') <> ''"
        )
    conn.execute(sa.text(
        "UPDATE library_idempotency "
        "SET updated_at = created_at, "
        f"    lifecycle_state = CASE WHEN {pending_evidence} "
        "        THEN 'pending' ELSE 'terminal' END, "
        f"    response_kind = CASE WHEN {pending_evidence} THEN 'pending' "
        f"        WHEN {problem_evidence} THEN 'problem' ELSE 'success' END"
    ))

    # Now create the unique constraint on command_identity_hash.
    # Existing rows have no browser identity to hash. Their already-unique,
    # bounded primary key is a safe contract-v1 placeholder that cannot exceed
    # the 64-character column; contract-v2 lookup never treats it as a hash.
    conn.execute(sa.text(
        "UPDATE library_idempotency "
        "SET command_identity_hash = id "
        "WHERE command_identity_hash = ''"
    ))

    with op.batch_alter_table("library_idempotency", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_library_idempotency_command_identity",
            ["workspace_id", "command_identity_hash"],
        )

    # ---- 2. library_command_impacts ------------------------------------
    op.create_table(
        "library_command_impacts",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("command_kind", sa.String(20), nullable=False),
        sa.Column("target_entry_id", sa.String(64), nullable=False),
        sa.Column("target_version_id", sa.String(64), nullable=True),
        sa.Column("target_revision", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("request_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("impact_hash", sa.String(64), nullable=False),
        sa.Column("projection_json", sa.JSON(), nullable=False),
        sa.Column("consumption_state", sa.String(20), nullable=False, server_default="available"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "command_kind IN ('publish', 'withdraw', 'archive')",
            name="ck_library_command_impact_kind",
        ),
        sa.CheckConstraint(
            "consumption_state IN ('available', 'consumed', 'expired')",
            name=op.f("ck_library_command_impacts_impact_consumption"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_command_impacts_workspace", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_library_command_impacts"),
        sa.UniqueConstraint(
            "workspace_id", "impact_hash",
            name="uq_library_command_impact_hash",
        ),
    )
    op.create_index(
        op.f("ix_library_command_impacts_workspace_id"),
        "library_command_impacts", ["workspace_id"],
    )
    op.create_index(
        op.f("ix_library_command_impacts_target_entry_id"),
        "library_command_impacts", ["target_entry_id"],
    )

    # ---- 3. library_entry_contexts -------------------------------------
    op.create_table(
        "library_entry_contexts",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("root_entry_id", sa.String(64), nullable=False),
        sa.Column("resource_entry_id", sa.String(64), nullable=False),
        sa.Column("parent_entry_id", sa.String(64), nullable=True),
        sa.Column("relation_kind", sa.String(40), nullable=False),
        sa.Column("creation_evidence", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "root_entry_id != resource_entry_id",
            name="ck_library_entry_context_no_self_ref",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_entry_contexts_workspace", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "root_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_entry_context_root", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "resource_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_entry_context_resource", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_entry_context_parent", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_library_entry_contexts"),
        sa.UniqueConstraint(
            "workspace_id", "root_entry_id", "resource_entry_id", "relation_kind",
            name="uq_library_entry_context",
        ),
    )
    op.create_index(
        op.f("ix_library_entry_contexts_workspace_id"),
        "library_entry_contexts", ["workspace_id"],
    )
    op.create_index(
        op.f("ix_library_entry_contexts_root_entry_id"),
        "library_entry_contexts", ["root_entry_id"],
    )
    op.create_index(
        op.f("ix_library_entry_contexts_resource_entry_id"),
        "library_entry_contexts", ["resource_entry_id"],
    )

    # ---- 4. library_stepup_assertions ----------------------------------
    op.create_table(
        "library_stepup_assertions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("jti_hash", sa.String(64), nullable=False),
        sa.Column("command_kind", sa.String(40), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_stepup_assertions_workspace", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_library_stepup_assertions"),
        sa.UniqueConstraint(
            "workspace_id", "jti_hash",
            name="uq_library_stepup_jti",
        ),
    )
    op.create_index(
        op.f("ix_library_stepup_assertions_workspace_id"),
        "library_stepup_assertions", ["workspace_id"],
    )

    # ---- 5. content_schema_version on drafts and versions --------------
    with op.batch_alter_table("library_drafts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("content_schema_version", sa.Integer(),
                       nullable=False, server_default="1")
        )

    with op.batch_alter_table("library_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("content_schema_version", sa.Integer(),
                       nullable=False, server_default="1")
        )

    # ---- 6. lifecycle_revision on versions -----------------------------
    with op.batch_alter_table("library_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("lifecycle_revision", sa.Integer(),
                       nullable=False, server_default="1")
        )
        batch_op.create_check_constraint(
            "lifecycle_revision_positive",
            "lifecycle_revision >= 1",
        )

    # ---- 7. Backfill content_schema_version on existing rows -----------
    conn.execute(sa.text(
        "UPDATE library_drafts SET content_schema_version = 1 "
        "WHERE content_schema_version = 1"
    ))
    conn.execute(sa.text(
        "UPDATE library_versions SET content_schema_version = 1, lifecycle_revision = 1 "
        "WHERE content_schema_version = 1"
    ))

    # ---- RLS & grants --------------------------------------------------
    _install_rls_and_grants()


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        return

    for table in NEW_WORKSPACE_OWNED:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
               USING (marketai_context_is_current(workspace_id))
               WITH CHECK (marketai_context_is_current(workspace_id))'''
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO marketai_domain_api")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_worker")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")


def _revoke_rls_and_grants() -> None:
    if not _is_postgres():
        return
    for table in NEW_WORKSPACE_OWNED:
        op.execute(
            f"REVOKE ALL ON TABLE {table} FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    _revoke_rls_and_grants()

    # ---- 6+5. Drop new columns from library_versions -------------------
    with op.batch_alter_table("library_versions", schema=None) as batch_op:
        batch_op.drop_constraint("lifecycle_revision_positive", type_="check")
        batch_op.drop_column("lifecycle_revision")
        batch_op.drop_column("content_schema_version")

    with op.batch_alter_table("library_drafts", schema=None) as batch_op:
        batch_op.drop_column("content_schema_version")

    # ---- 4. Drop library_stepup_assertions -----------------------------
    op.drop_table("library_stepup_assertions")

    # ---- 3. Drop library_entry_contexts --------------------------------
    op.drop_table("library_entry_contexts")

    # ---- 2. Drop library_command_impacts -------------------------------
    op.drop_table("library_command_impacts")

    # ---- 1. Revert library_idempotency extensions ----------------------
    with op.batch_alter_table("library_idempotency", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_library_idempotency_command_identity", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_library_idempotency_response_kind", type_="check"
        )
        batch_op.drop_constraint(
            "ck_library_idempotency_lifecycle_state", type_="check"
        )
        batch_op.drop_column("reconciled_at")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("response_kind")
        batch_op.drop_column("lifecycle_state")
        batch_op.drop_column("command_identity_hash")
        batch_op.drop_column("contract_schema_version")
