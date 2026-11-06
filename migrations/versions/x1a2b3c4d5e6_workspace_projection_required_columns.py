"""Keep the v1 workspace projection function aligned with required columns.

Revision ID: x1a2b3c4d5e6
Revises: w1a2b3c4d5e6

The v1 SECURITY DEFINER function was installed before ``lifecycle`` and
``budget_currency`` became NOT NULL.  A repair for a workspace that was not
already present in the domain database therefore failed at the INSERT.  The
function remains the narrow runtime write boundary; this revision only
updates its column projection and does not grant direct table DML.
"""
from __future__ import annotations

from alembic import op


revision = "x1a2b3c4d5e6"
down_revision = "w1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _install_projection_function(*, include_required_columns: bool) -> None:
    inserted_columns = ""
    inserted_values = ""
    updated_columns = ""
    if include_required_columns:
        inserted_columns = ", lifecycle, budget_currency"
        inserted_values = (
            ", CASE WHEN p_suspended THEN 'suspended' ELSE 'active' END, "
            "p_default_currency"
        )
        updated_columns = """
                name = EXCLUDED.name,
                default_currency = EXCLUDED.default_currency,
                lifecycle = EXCLUDED.lifecycle,
                budget_currency = EXCLUDED.budget_currency,"""

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION marketai_apply_workspace_projection(
            p_workspace_id text, p_workspace_name text,
            p_authorization_epoch integer, p_entitlement_version integer,
            p_suspended boolean, p_projection_version integer,
            p_default_currency text
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
               OR p_default_currency !~ '^[A-Z]{{3}}$' THEN
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
                {inserted_columns}
            ) VALUES (
                p_workspace_id, p_workspace_name, p_default_currency,
                p_authorization_epoch, p_entitlement_version,
                p_suspended, p_projection_version, now()
                {inserted_values}
            )
            ON CONFLICT (id) DO UPDATE SET
                {updated_columns}
                authorization_epoch = GREATEST(
                    public.workspaces.authorization_epoch,
                    EXCLUDED.authorization_epoch
                ),
                entitlement_version = GREATEST(
                    public.workspaces.entitlement_version,
                    EXCLUDED.entitlement_version
                ),
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
        "(text, text, integer, integer, boolean, integer, text) "
        "TO marketai_domain_api"
    )


def upgrade() -> None:
    if _is_postgres():
        _install_projection_function(include_required_columns=True)


def downgrade() -> None:
    if _is_postgres():
        _install_projection_function(include_required_columns=False)
