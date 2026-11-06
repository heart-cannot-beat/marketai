"""Return is_platform_workspace from marketai_workspace_admission().

Revision ID: aa1_identity_kind
Revises: c80a1b2c3d4e

Rebased onto `c80a1b2c3d4e` ("Permit only contracted Localization state
transitions") rather than the `z1a2b3c4d5e6` this task's brief named as the
head: that migration was added to this shared worktree by concurrent work
after the brief was authored, and `z1a2b3c4d5e6` is no longer the single
head (`alembic heads` showed two heads with this migration still pointed at
it). Chaining after the actual current head keeps a single linear head
instead of requiring a merge migration.

`workspaces.is_platform_workspace` already exists (migration
`m23a_meta_foundation`) and is populated on every dialect -- this migration
adds no column. What it changes is PostgreSQL-only: the pre-tenant
`marketai_workspace_admission()` SECURITY DEFINER function (created by
migration `g1a2b3c4d5e6` and otherwise unchanged since) is extended to also
return `is_platform_workspace`, so `WorkspaceAdmissionReader` (`app.platform.
workspace_admission`) can populate `WorkspaceAdmission.is_platform_workspace`
from the same admission read a platform-operator route (Task 6) needs before
a `TenantContext` exists, rather than a second RLS-blocked query.

PostgreSQL does not allow `CREATE OR REPLACE FUNCTION` to change a
`RETURNS TABLE` column list -- that is a return-type change, which Postgres
rejects with "cannot change return type of existing function". The function
is therefore dropped and recreated, which also drops its grants, so the
`REVOKE`/`GRANT EXECUTE` from `g1a2b3c4d5e6` are re-applied verbatim after
recreation. The function's argument signature (`text`) is unchanged, so
`DROP FUNCTION marketai_workspace_admission(text)` unambiguously names it.
"""
from __future__ import annotations

from alembic import op


revision = "aa1_identity_kind"
down_revision = "c80a1b2c3d4e"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP FUNCTION IF EXISTS marketai_workspace_admission(text)")
    op.execute(
        """
        CREATE FUNCTION marketai_workspace_admission(p_workspace_id text)
        RETURNS TABLE (
            workspace_id text,
            authorization_epoch integer,
            entitlement_version integer,
            suspended boolean,
            is_platform_workspace boolean
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT w.id, w.authorization_epoch, w.entitlement_version,
                   w.suspended, w.is_platform_workspace
            FROM (VALUES (1)) AS seed(n)
            LEFT JOIN public.workspaces w ON w.id = p_workspace_id;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_workspace_admission(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_workspace_admission(text) "
        "TO marketai_domain_api, marketai_domain_worker"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP FUNCTION IF EXISTS marketai_workspace_admission(text)")
    op.execute(
        """
        CREATE FUNCTION marketai_workspace_admission(p_workspace_id text)
        RETURNS TABLE (
            workspace_id text,
            authorization_epoch integer,
            entitlement_version integer,
            suspended boolean
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT w.id, w.authorization_epoch, w.entitlement_version, w.suspended
            FROM (VALUES (1)) AS seed(n)
            LEFT JOIN public.workspaces w ON w.id = p_workspace_id;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_workspace_admission(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_workspace_admission(text) "
        "TO marketai_domain_api, marketai_domain_worker"
    )
