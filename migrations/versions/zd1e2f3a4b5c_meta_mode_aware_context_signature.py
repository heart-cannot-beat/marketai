"""Authenticate meta-mode-bound tenant contexts on Workspace-owned tables.

Revision ID: zd1e2f3a4b5c
Revises: zc1d2e3f4a5b

``tenant_context_signature()`` (app/platform/tenancy.py) appends ``meta_mode``
to the HMAC payload whenever the context carries one, and
``is_platform_workspace`` whenever it is true. ``marketai_context_is_current``
-- the policy behind every Workspace-owned table such as ``experiments`` --
still hashed only ``workspace_id`` and ``authorization_epoch``, so a request
that binds ``meta_mode`` (every multi-candidate Launch route) could read
Meta-domain rows but not the frozen Experiment they point at: PostgreSQL
answered ``Frozen Experiment not found`` while SQLite, which has no RLS,
accepted the same request.

Both policy functions now derive the payload from the same settings the API
writes with ``SET LOCAL``: the mode suffix comes from ``marketai.meta_mode``
(the meta-aware function keeps requiring it to equal the row's ``meta_mode``)
and the platform suffix from ``marketai.is_platform_workspace``. A signature
minted for one mode still fails for another, and clearing the mode setting
does not turn a meta-bound signature into a plain one.
"""
from __future__ import annotations

from alembic import op


revision = "zd1e2f3a4b5c"
down_revision = "zc1d2e3f4a5b"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


# Mirrors tenant_context_signature(): base payload, then "\n<meta_mode>" when a
# mode is bound, then "\ntrue" when the Workspace is the platform Workspace.
_SIGNATURE_PAYLOAD = """
    convert_to(
        'marketai-tenant-context-v1' || chr(10) ||
        candidate_workspace || chr(10) ||
        workspace.authorization_epoch::text ||
        CASE
            WHEN {mode_expression} IS NULL THEN ''
            ELSE chr(10) || {mode_expression}
        END ||
        CASE
            WHEN current_setting('marketai.is_platform_workspace', true) = 'true'
                THEN chr(10) || 'true'
            ELSE ''
        END,
        'UTF8'
    )
"""

_PLAIN_MODE = "nullif(current_setting('marketai.meta_mode', true), '')"
_META_MODE = "candidate_mode"


def _signature_matches(mode_expression: str) -> str:
    payload = _SIGNATURE_PAYLOAD.format(mode_expression=mode_expression)
    return f"""
        encode(
            hmac(
                {payload},
                convert_to(context_key.secret, 'UTF8'),
                'sha256'
            ),
            'hex'
        ) = COALESCE(current_setting('marketai.context_signature', true), '')
    """


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION marketai_context_is_current(candidate_workspace text)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT candidate_workspace = nullif(
                       current_setting('marketai.workspace_id', true), ''
                   )
               AND EXISTS (
                    SELECT 1
                    FROM public.workspaces AS workspace
                    CROSS JOIN public.tenant_context_secrets AS context_key
                    WHERE context_key.key_id = 1
                      AND workspace.id = candidate_workspace
                      AND workspace.authorization_epoch = COALESCE(
                            NULLIF(
                                current_setting('marketai.authorization_epoch', true),
                                ''
                            )::integer,
                            -1
                      )
                      AND {_signature_matches(_PLAIN_MODE)}
               )
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION marketai_meta_context_is_current(
            candidate_workspace text, candidate_mode text
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT candidate_workspace = nullif(
                       current_setting('marketai.workspace_id', true), ''
                   )
               AND candidate_mode = nullif(
                       current_setting('marketai.meta_mode', true), ''
                   )
               AND EXISTS (
                    SELECT 1
                    FROM public.workspaces AS workspace
                    CROSS JOIN public.tenant_context_secrets AS context_key
                    WHERE context_key.key_id = 1
                      AND workspace.id = candidate_workspace
                      AND workspace.authorization_epoch = COALESCE(
                            NULLIF(
                                current_setting('marketai.authorization_epoch', true),
                                ''
                            )::integer,
                            -1
                      )
                      AND {_signature_matches(_META_MODE)}
               )
        $$
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    # e9f0a1b2c3d4: workspace + epoch only.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_context_is_current(candidate_workspace text)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT candidate_workspace = nullif(
                       current_setting('marketai.workspace_id', true), ''
                   )
               AND EXISTS (
                    SELECT 1
                    FROM public.workspaces AS workspace
                    CROSS JOIN public.tenant_context_secrets AS context_key
                    WHERE context_key.key_id = 1
                      AND workspace.id = candidate_workspace
                      AND workspace.authorization_epoch = COALESCE(
                            NULLIF(
                                current_setting(
                                    'marketai.authorization_epoch', true
                                ),
                                ''
                            )::integer,
                            -1
                      )
                      AND encode(
                            hmac(
                                convert_to(
                                    'marketai-tenant-context-v1' || chr(10) ||
                                    candidate_workspace || chr(10) ||
                                    workspace.authorization_epoch::text,
                                    'UTF8'
                                ),
                                convert_to(context_key.secret, 'UTF8'),
                                'sha256'
                            ),
                            'hex'
                      ) = COALESCE(
                            current_setting(
                                'marketai.context_signature', true
                            ),
                            ''
                      )
               )
        $$
        """
    )
    # m23a_meta_foundation: workspace + epoch + mode.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_meta_context_is_current(
            candidate_workspace text, candidate_mode text
        )
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT candidate_workspace = nullif(
                       current_setting('marketai.workspace_id', true), ''
                   )
               AND candidate_mode = nullif(
                       current_setting('marketai.meta_mode', true), ''
                   )
               AND EXISTS (
                    SELECT 1
                    FROM public.workspaces AS workspace
                    CROSS JOIN public.tenant_context_secrets AS context_key
                    WHERE context_key.key_id = 1
                      AND workspace.id = candidate_workspace
                      AND workspace.authorization_epoch = COALESCE(
                            NULLIF(
                                current_setting('marketai.authorization_epoch', true),
                                ''
                            )::integer,
                            -1
                      )
                      AND encode(
                            hmac(
                                convert_to(
                                    'marketai-tenant-context-v1' || chr(10) ||
                                    candidate_workspace || chr(10) ||
                                    workspace.authorization_epoch::text || chr(10) ||
                                    candidate_mode,
                                    'UTF8'
                                ),
                                convert_to(context_key.secret, 'UTF8'),
                                'sha256'
                            ),
                            'hex'
                      ) = COALESCE(
                            current_setting('marketai.context_signature', true),
                            ''
                      )
               )
        $$
        """
    )
