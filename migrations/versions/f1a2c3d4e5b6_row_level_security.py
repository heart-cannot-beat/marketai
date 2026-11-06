"""Enable Row Level Security on every Workspace-owned table.

Revision ID: f1a2c3d4e5b6
Revises: d884bc1b4a90

Isolation stops being something every query has to remember and becomes
something the database enforces.

Two policy shapes, and only two:

* a table that carries ``workspace_id`` compares it to the transaction-local
  ``marketai.workspace_id`` setting;
* a child table is visible exactly when its parent row is visible.

The child form is deliberate. Denormalising ``workspace_id`` onto every child
would give a uniform one-line policy, but it adds a column that can drift out of
step with the foreign key it duplicates, and a drifted copy is a cross-tenant
read that looks correct. Pointing the policy at the foreign key instead makes
the relationship itself the boundary. PostgreSQL applies RLS to tables named
inside a policy expression too, so a two-level chain such as
``dimension_values -> dimensions -> dimension_schema_versions`` composes without
either policy knowing about the Workspace.

``marketai_current_workspace()`` reads the setting with ``missing_ok``, so an
unset scope is NULL rather than an error, and ``workspace_id = NULL`` matches
nothing. An untenanted connection therefore sees an empty database instead of
everyone's.

SQLite has no RLS. This migration is a no-op there, which is what keeps the
local and import path working while leaving the hosted path enforced.
"""

from __future__ import annotations

from alembic import op

revision = "f1a2c3d4e5b6"
down_revision = "d884bc1b4a90"
branch_labels = None
depends_on = None


#: Tables that carry the Workspace directly.
WORKSPACE_OWNED = (
    "products",
    "dimension_schema_versions",
    "plans",
    "generation_runs",
    "assets",
    "asset_versions",
    "quality_reviews",
    "verdict_decisions",
    "jobs",
    "cost_ledger",
    "import_batches",
    "meta_accounts",
    "meta_sync_runs",
    "launch_previews",
    "launch_operations",
    "launch_audit_log",
    "meta_sync_schedules",
    "experiments",
)

#: Tables owned transitively: (table, foreign key column, parent table).
#: A row is visible exactly when the parent row it belongs to is visible.
CHILD_TABLES = (
    ("dimensions", "schema_id", "dimension_schema_versions"),
    ("dimension_values", "dimension_id", "dimensions"),
    ("plan_entries", "plan_id", "plans"),
    ("meta_ingestions", "sync_run_id", "meta_sync_runs"),
    ("meta_ingestion_pages", "ingestion_id", "meta_ingestions"),
    ("ad_daily_metrics", "account_id", "meta_accounts"),
    ("meta_entity_status_events", "account_id", "meta_accounts"),
    ("meta_ad_bindings", "account_id", "meta_accounts"),
    ("launch_confirmations", "preview_id", "launch_previews"),
    ("launch_steps", "operation_id", "launch_operations"),
    ("experiment_entries", "experiment_id", "experiments"),
    ("experiment_ad_bindings", "experiment_entry_id", "experiment_entries"),
    ("experiment_state_events", "experiment_id", "experiments"),
    ("judgements", "experiment_id", "experiments"),
    ("experiment_decisions", "experiment_id", "experiments"),
)

#: The Workspace registry itself. Platform-global in the sense that it is not
#: owned by any Workspace, but a tenant may still only see its own row.
REGISTRY_TABLE = "workspaces"

ALL_PROTECTED = (
    WORKSPACE_OWNED + tuple(t for t, _, _ in CHILD_TABLES) + (REGISTRY_TABLE,)
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite: the repository layer is the only enforcement available.
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_current_workspace() RETURNS text
        LANGUAGE sql
        STABLE
        AS $$
            SELECT nullif(current_setting('marketai.workspace_id', true), '')
        $$
        """
    )

    for table in ALL_PROTECTED:
        # FORCE applies the policies to the table owner as well. The runtime
        # roles own nothing, so this is belt and braces -- but it is the half
        # that keeps a migration-role mistake from reading across tenants.
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    for table in WORKSPACE_OWNED:
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON "{table}"
            USING (workspace_id = marketai_current_workspace())
            WITH CHECK (workspace_id = marketai_current_workspace())
            """
        )

    for table, fk_column, parent in CHILD_TABLES:
        # WITH CHECK as well as USING: without it a row could be inserted
        # pointing at another Workspace's parent and simply become invisible,
        # which is a silent write into someone else's data.
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON "{table}"
            USING (
                EXISTS (
                    SELECT 1 FROM "{parent}" AS parent
                    WHERE parent.id = "{table}"."{fk_column}"
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM "{parent}" AS parent
                    WHERE parent.id = "{table}"."{fk_column}"
                )
            )
            """
        )

    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON "{REGISTRY_TABLE}"
        USING (id = marketai_current_workspace())
        WITH CHECK (id = marketai_current_workspace())
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    for table in ALL_PROTECTED:
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    op.execute("DROP FUNCTION IF EXISTS marketai_current_workspace()")
