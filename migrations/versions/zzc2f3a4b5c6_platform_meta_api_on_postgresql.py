"""Let the platform Meta API create the platform Workspace and find a credential's owner (#91, #94).

Revision ID: zzc2f3a4b5c6
Revises: zzb1e2f3a4b5

``app.meta.platform_api`` acts across Workspaces for a platform operator and
starts without a TenantContext. ``marketai_domain_api`` is NOBYPASSRLS and
``workspaces`` and ``credential_versions`` are FORCE RLS behind a signed,
current-epoch TenantContext, so two of its first steps could not work on
PostgreSQL:

* ``POST /api/platform/v1/meta/bootstrap`` created a missing Workspace with
  ``session.add(WorkspaceRecord(...))``. The API role holds only SELECT on
  ``workspaces`` (#91). Customer Workspaces come only from the control-plane
  projection (``marketai_apply_workspace_projection_v2``), so bootstrap now
  refuses a Workspace that has not been projected. The one Workspace the
  control plane does not project is the platform Workspace
  (``is_platform_workspace = true``, ADR 0009), and
  ``marketai_create_platform_workspace_v1`` is the only way to create it.
* ``POST /api/platform/v1/credentials/{id}/revoke`` read the credential
  version untenanted, and read every column, including the ciphertext the
  API role may not read (#94). ``marketai_credential_version_workspace_v1``
  answers only which Workspace owns a credential version; the revocation
  that follows reads the non-secret columns in a TenantContext signed for
  that Workspace's current epoch, under the same RLS as any API transaction.

Both functions follow ``marketai_workspace_admission`` and
``marketai_legacy_operation_candidates_v1``: owned by the schema owner,
``search_path`` pinned to ``''`` with every name schema-qualified, EXECUTE
revoked from PUBLIC and granted to ``marketai_domain_api`` alone. The grants
are mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

``marketai_create_platform_workspace_v1(workspace_id, initiator_id)`` inserts
only a row with ``is_platform_workspace = true``. A row that already exists
is left exactly as it is -- a customer Workspace is never promoted -- and the
call returns false. A row it creates gets one ``workspace.platform_created``
audit event in the same transaction, attributed to the operator and to the
platform-admin executor.

``marketai_credential_version_workspace_v1(credential_version_id)`` is STABLE
and returns the owning ``workspace_id`` or NULL: never the key, state or any
other column.

No table changes. The previous release keeps running while this migrates and
never calls the functions; downgrade drops them. SQLite has no RLS and no
roles: there the API reads and writes these rows directly.
"""

from alembic import op


revision = "zzc2f3a4b5c6"
down_revision = "zzb1e2f3a4b5"
branch_labels = None
depends_on = None

_CREATE = "public.marketai_create_platform_workspace_v1(text, text)"
_OWNER = "public.marketai_credential_version_workspace_v1(text)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_create_platform_workspace_v1(
            p_workspace_id text,
            p_initiator_id text
        )
        RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ''
        AS $$
        DECLARE created integer;
        BEGIN
            IF p_workspace_id IS NULL OR p_workspace_id = ''
               OR length(p_workspace_id) > 64
               OR p_initiator_id IS NULL OR p_initiator_id = ''
               OR length(p_initiator_id) > 100 THEN
                RAISE EXCEPTION 'invalid platform workspace request'
                    USING ERRCODE = '22023';
            END IF;

            -- Only a platform Workspace, and only a new one: an existing row
            -- (a projected customer Workspace included) keeps every column.
            INSERT INTO public.workspaces (
                id, name, default_currency, authorization_epoch,
                entitlement_version, suspended, projection_version,
                lifecycle, budget_currency, is_platform_workspace, created_at
            ) VALUES (
                p_workspace_id, p_workspace_id, 'USD', 0,
                0, false, 0,
                'active', 'USD', true, now()
            )
            ON CONFLICT (id) DO NOTHING;

            GET DIAGNOSTICS created = ROW_COUNT;
            IF created = 0 THEN
                RETURN false;
            END IF;

            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, meta_mode,
                event_type, resource_type, resource_id, details_json,
                created_at
            ) VALUES (
                'audit_' || md5(p_workspace_id || '|platform-workspace-created'),
                p_workspace_id, p_initiator_id, 'system', 'system:platform_admin',
                '', '', 0, NULL,
                'workspace.platform_created', 'workspace', p_workspace_id,
                json_build_object('is_platform_workspace', true),
                now()
            );
            RETURN true;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_CREATE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_CREATE} TO marketai_domain_api")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_credential_version_workspace_v1(
            p_credential_version_id text
        )
        RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT version.workspace_id::text
              FROM public.credential_versions AS version
             WHERE version.id = p_credential_version_id
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_OWNER} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OWNER} TO marketai_domain_api")


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP FUNCTION IF EXISTS {_OWNER}")
    op.execute(f"DROP FUNCTION IF EXISTS {_CREATE}")
