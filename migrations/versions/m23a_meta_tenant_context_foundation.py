"""Platform workspace flag, meta HMAC function, and audit meta_mode.

Revision ID: m23a_meta_foundation
Revises: t1a2b3c4d5e6
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "m23a_meta_foundation"
down_revision = "t1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # 1. Platform workspace flag
    op.add_column(
        "workspaces",
        sa.Column(
            "is_platform_workspace",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 2. Audit meta_mode
    op.add_column(
        "audit_events",
        sa.Column("meta_mode", sa.String(32), nullable=True),
    )

    if not _is_postgres():
        return

    # 3. Meta-aware HMAC RLS function (PostgreSQL only)
    op.execute("""
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
        $$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_meta_context_is_current(text, text) FROM PUBLIC;"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_meta_context_is_current(text, text) "
        "TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;"
    )

    # 4. Register the custom GUC for meta_mode
    op.execute("""
        SELECT set_config('marketai.meta_mode', '', false);
    """)


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP FUNCTION IF EXISTS marketai_meta_context_is_current(text, text);")
    op.drop_column("audit_events", "meta_mode")
    op.drop_column("workspaces", "is_platform_workspace")
