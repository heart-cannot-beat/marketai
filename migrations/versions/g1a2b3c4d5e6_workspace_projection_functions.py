"""Workspace authorization projection: columns plus SECURITY DEFINER functions.

Revision ID: g1a2b3c4d5e6
Revises: f0a1b2c3d4e5

`DelegatedIdentityMiddleware` (spec §3) needs to read `authorization_epoch`,
`entitlement_version`, and `suspended` for a Workspace *before* a
`TenantContext` exists to satisfy Row Level Security -- reading is the
admission check itself. And the projection sync path that keeps those
columns current needs to write them without the runtime role holding any
direct DML grant on `workspaces`.

Both problems get the same answer: a narrow `SECURITY DEFINER` function,
owned by `marketai_domain_owner` (which has `BYPASSRLS`), granted `EXECUTE`
only to the specific runtime roles that need it, with `search_path` pinned
to `''` and every table reference schema-qualified so the function cannot be
hijacked by a search-path change.

* `marketai_workspace_admission(workspace_id)` -- read-only, `STABLE`.
  Granted to both `marketai_domain_api` and `marketai_domain_worker`: the
  Worker re-checks admission before committing an upload (spec §7.8 step 1).
* `marketai_apply_workspace_projection(...)` -- the only way the
  `authorization_epoch`/`entitlement_version`/`suspended`/`projection_version`
  columns are ever written outside a migration. It enforces its own input
  validation and a monotonic `projection_version` guard, so a replayed or
  out-of-order snapshot from the control plane is a no-op rather than a
  regression. Granted to `marketai_domain_api` only -- the Worker never
  originates a projection sync.

Both functions are PostgreSQL-only. The three new columns are added on every
dialect so the SQLAlchemy model stays uniform; on SQLite there is no RLS and
no admission chicken-and-egg problem, so nothing reads these columns through
a function there.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "g1a2b3c4d5e6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(
            sa.Column("entitlement_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("suspended", sa.Boolean(), nullable=False, server_default="false")
        )
        batch_op.add_column(
            sa.Column("projection_version", sa.Integer(), nullable=False, server_default="0")
        )

    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_workspace_admission(p_workspace_id text)
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

    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_apply_workspace_projection(
            p_workspace_id text, p_workspace_name text,
            p_authorization_epoch integer, p_entitlement_version integer,
            p_suspended boolean, p_projection_version integer, p_default_currency text
        )
        RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
        AS $$
        DECLARE affected_rows integer;
        BEGIN
            IF p_workspace_id IS NULL OR p_workspace_name IS NULL
               OR p_authorization_epoch IS NULL OR p_entitlement_version IS NULL
               OR p_suspended IS NULL OR p_projection_version IS NULL
               OR p_default_currency IS NULL
               OR p_workspace_id = '' OR length(p_workspace_id) > 255
               OR p_workspace_name = '' OR length(p_workspace_name) > 200
               OR p_authorization_epoch < 0 OR p_entitlement_version < 0
               OR p_projection_version < 0
               OR p_default_currency !~ '^[A-Z]{3}$' THEN
                RAISE EXCEPTION 'invalid workspace projection snapshot';
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.workspaces AS current
                WHERE current.id = p_workspace_id
                  AND p_projection_version > current.projection_version
                  AND (p_authorization_epoch < current.authorization_epoch
                       OR p_entitlement_version < current.entitlement_version)
            ) THEN
                RAISE EXCEPTION 'newer projection regresses a semantic counter';
            END IF;

            INSERT INTO public.workspaces (
                id, name, default_currency, authorization_epoch,
                entitlement_version, suspended, projection_version, created_at
            ) VALUES (
                p_workspace_id, p_workspace_name, p_default_currency,
                p_authorization_epoch, p_entitlement_version,
                p_suspended, p_projection_version, now()
            )
            ON CONFLICT (id) DO UPDATE SET
                authorization_epoch = GREATEST(public.workspaces.authorization_epoch, EXCLUDED.authorization_epoch),
                entitlement_version = GREATEST(public.workspaces.entitlement_version, EXCLUDED.entitlement_version),
                suspended = EXCLUDED.suspended,
                projection_version = EXCLUDED.projection_version
            WHERE public.workspaces.projection_version < EXCLUDED.projection_version;

            GET DIAGNOSTICS affected_rows = ROW_COUNT;
            RETURN affected_rows > 0;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_apply_workspace_projection"
        "(text, text, integer, integer, boolean, integer, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_apply_workspace_projection"
        "(text, text, integer, integer, boolean, integer, text) TO marketai_domain_api"
    )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "DROP FUNCTION IF EXISTS marketai_apply_workspace_projection"
            "(text, text, integer, integer, boolean, integer, text)"
        )
        op.execute("DROP FUNCTION IF EXISTS marketai_workspace_admission(text)")

    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.drop_column("projection_version")
        batch_op.drop_column("suspended")
        batch_op.drop_column("entitlement_version")
