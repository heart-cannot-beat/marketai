"""Let the platform Meta API find connections across Workspaces (#102).

Revision ID: zzd3a4b5c6d7
Revises: zzc2f3a4b5c6

``app.meta.platform_api`` lists, reads and reconciles Meta connections for a
platform operator who does not know the Workspace up front. Those three routes
queried ``meta_connection_records`` and ``meta_data_source_binding_records``
in an untenanted session. Both tables are FORCE RLS behind
``marketai_meta_context_is_current(workspace_id, meta_mode)`` and
``marketai_domain_api`` is NOBYPASSRLS, so on PostgreSQL the list answered 200
with no items and a connection by id answered 404.

Two narrow SECURITY DEFINER functions answer only where a connection lives:

* ``marketai_meta_connection_ids_v1(after_connection_id, limit)`` lists
  connections across Workspaces in the list's order (``created_at``, then
  ``id``), starting after the given connection when there is one, capped at
  500 rows. Each row carries the connection id, its Workspace and Meta mode,
  and the Workspace's current authorization epoch and platform flag: what the
  API needs to sign a TenantContext, and nothing else. An unknown
  ``after_connection_id`` lists nothing; the route refuses it first.
* ``marketai_meta_connection_workspace_v1(connection_id)`` returns the
  connection's Workspace and Meta mode, or no row.

The Meta mode is part of the answer because the Meta tables' policy binds it:
a context signed for one mode reads none of another mode's rows. Every read
that follows -- the connection, its binding -- runs in a TenantContext signed
for that Workspace, mode and current epoch, under the same RLS as any API
transaction.

Both follow ``marketai_admitted_operation_candidates_v1`` and
``marketai_credential_version_workspace_v1``: read-only and STABLE, owned by
the schema owner, ``search_path`` pinned to ``''`` with every name
schema-qualified, EXECUTE revoked from PUBLIC and granted to
``marketai_domain_api`` alone. The grants are mirrored in
``deploy/postgres/domain-tenant-roles.sql``.

No table changes. The previous release keeps running while this migrates and
never calls the functions; downgrade drops them, after which the routes read
nothing on PostgreSQL again (the state before this revision). SQLite has no
RLS and no roles: there the API reads these rows directly.
"""

from alembic import op


revision = "zzd3a4b5c6d7"
down_revision = "zzc2f3a4b5c6"
branch_labels = None
depends_on = None

_LIST = "public.marketai_meta_connection_ids_v1(text, integer)"
_OWNER = "public.marketai_meta_connection_workspace_v1(text)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_meta_connection_ids_v1(
            p_after_connection_id text,
            p_limit integer
        )
        RETURNS TABLE (
            workspace_id varchar,
            meta_mode varchar,
            authorization_epoch integer,
            is_platform_workspace boolean,
            connection_id varchar
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT connection.workspace_id, connection.meta_mode,
                   workspace.authorization_epoch, workspace.is_platform_workspace,
                   connection.id
              FROM public.meta_connection_records AS connection
              JOIN public.workspaces AS workspace
                ON workspace.id = connection.workspace_id
             WHERE p_after_connection_id IS NULL
                OR (connection.created_at, connection.id) > (
                    SELECT after_connection.created_at, after_connection.id
                      FROM public.meta_connection_records AS after_connection
                     WHERE after_connection.id = p_after_connection_id
                )
             ORDER BY connection.created_at, connection.id
             LIMIT LEAST(GREATEST(COALESCE(p_limit, 0), 0), 500)
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LIST} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LIST} TO marketai_domain_api")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_meta_connection_workspace_v1(
            p_connection_id text
        )
        RETURNS TABLE (
            workspace_id varchar,
            meta_mode varchar
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT connection.workspace_id, connection.meta_mode
              FROM public.meta_connection_records AS connection
             WHERE connection.id = p_connection_id
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_OWNER} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OWNER} TO marketai_domain_api")


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP FUNCTION IF EXISTS {_OWNER}")
    op.execute(f"DROP FUNCTION IF EXISTS {_LIST}")
