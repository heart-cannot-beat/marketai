"""Add typed onboarding fact outbox.

Revision ID: y1a2b3c4d5e6
Revises: b79c2d3e4f5a
"""
from alembic import op
import sqlalchemy as sa


revision = "y1a2b3c4d5e6"
down_revision = "b79c2d3e4f5a"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("onboarding_fact_outbox",)


def upgrade() -> None:
    op.create_table(
        "onboarding_fact_outbox",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("fact_type", sa.String(length=80), nullable=False),
        sa.Column("source_resource_id", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivery_lease_id", sa.String(length=160), nullable=True),
        sa.Column(
            "delivery_lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "fact_type IN ('resolution_preview.validated', "
            "'generation_recipe_snapshot.mock_created', "
            "'asset_version.qualified', 'quality_review.human_recorded')",
            name="ck_onboarding_fact_type",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "fact_type", "source_resource_id",
            name="uq_onboarding_fact_source",
        ),
    )
    op.create_index(
        "ix_onboarding_fact_outbox_workspace_id",
        "onboarding_fact_outbox", ["workspace_id"], unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "onboarding_fact_outbox" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "onboarding_fact_outbox" FORCE ROW LEVEL SECURITY')
        op.execute(
            "CREATE POLICY tenant_isolation ON onboarding_fact_outbox "
            "USING (marketai_context_is_current(workspace_id)) "
            "WITH CHECK (marketai_context_is_current(workspace_id))"
        )
        op.execute(
            "GRANT SELECT, INSERT ON TABLE onboarding_fact_outbox TO "
            "marketai_domain_api, marketai_domain_worker"
        )
        op.execute(
            "GRANT UPDATE ON TABLE onboarding_fact_outbox TO marketai_domain_worker"
        )
        op.execute(
            "GRANT SELECT ON TABLE onboarding_fact_outbox TO marketai_domain_readonly"
        )
        op.execute(
            """
            CREATE FUNCTION marketai_claim_next_onboarding_fact(
                claim_owner text,
                lease_seconds integer
            )
            RETURNS TABLE (
                event_id varchar,
                workspace_id varchar,
                fact_type varchar,
                source_resource_id varchar,
                observed_at timestamptz,
                authorization_epoch integer
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $$
            BEGIN
                IF claim_owner IS NULL OR length(claim_owner) < 3 THEN
                    RAISE EXCEPTION 'claim_owner is required';
                END IF;
                IF lease_seconds < 1 OR lease_seconds > 600 THEN
                    RAISE EXCEPTION 'lease_seconds must be between 1 and 600';
                END IF;

                RETURN QUERY
                WITH candidate AS (
                    SELECT pending.id
                    FROM public.onboarding_fact_outbox AS pending
                    WHERE pending.delivered_at IS NULL
                      AND (
                        pending.delivery_lease_expires_at IS NULL
                        OR pending.delivery_lease_expires_at <= clock_timestamp()
                      )
                    ORDER BY pending.observed_at, pending.id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                ), claimed AS (
                    UPDATE public.onboarding_fact_outbox AS pending
                    SET attempts = pending.attempts + 1,
                        delivery_lease_id = claim_owner,
                        delivery_lease_expires_at =
                            clock_timestamp() + make_interval(secs => lease_seconds),
                        last_error = NULL
                    FROM candidate
                    WHERE pending.id = candidate.id
                    RETURNING pending.*
                )
                SELECT claimed.id,
                       claimed.workspace_id,
                       claimed.fact_type,
                       claimed.source_resource_id,
                       claimed.observed_at,
                       workspace.authorization_epoch
                FROM claimed
                JOIN public.workspaces AS workspace
                  ON workspace.id = claimed.workspace_id;
            END
            $$
            """
        )
        op.execute(
            "REVOKE ALL ON FUNCTION "
            "marketai_claim_next_onboarding_fact(text, integer) FROM PUBLIC"
        )
        op.execute(
            "GRANT EXECUTE ON FUNCTION "
            "marketai_claim_next_onboarding_fact(text, integer) "
            "TO marketai_domain_worker"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "marketai_claim_next_onboarding_fact(text, integer)"
        )
        op.execute(
            "REVOKE ALL ON TABLE onboarding_fact_outbox FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation ON onboarding_fact_outbox"
        )
        op.execute(
            'ALTER TABLE "onboarding_fact_outbox" NO FORCE ROW LEVEL SECURITY'
        )
        op.execute(
            'ALTER TABLE "onboarding_fact_outbox" DISABLE ROW LEVEL SECURITY'
        )
    op.drop_index(
        "ix_onboarding_fact_outbox_workspace_id",
        table_name="onboarding_fact_outbox",
    )
    op.drop_table("onboarding_fact_outbox")
