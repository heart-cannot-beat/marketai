"""Harden onboarding fact delivery on installations that applied early #76.

Revision ID: z1a2b3c4d5e6
Revises: y1a2b3c4d5e6

The first #76 migration was exercised in Compose before its delivery lease was
added. These operations are deliberately idempotent: clean installations get
the final schema from the previous revision, while development/staging
databases that already recorded that revision are safely converged here.
"""

from alembic import op


revision = "z1a2b3c4d5e6"
down_revision = "y1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        "ALTER TABLE onboarding_fact_outbox "
        "ADD COLUMN IF NOT EXISTS delivery_lease_id varchar(160)"
    )
    op.execute(
        "ALTER TABLE onboarding_fact_outbox "
        "ADD COLUMN IF NOT EXISTS delivery_lease_expires_at timestamptz"
    )
    op.execute(
        "ALTER TABLE onboarding_fact_outbox "
        "ADD COLUMN IF NOT EXISTS last_error varchar(500)"
    )
    op.execute('ALTER TABLE "onboarding_fact_outbox" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "onboarding_fact_outbox" FORCE ROW LEVEL SECURITY')
    op.execute(
        "DROP POLICY IF EXISTS onboarding_fact_outbox_tenant "
        "ON onboarding_fact_outbox"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON onboarding_fact_outbox")
    # Early #76 installations created the FK wider than workspaces.id. The
    # policy must be absent while PostgreSQL changes the referenced column.
    op.execute(
        "ALTER TABLE onboarding_fact_outbox "
        "ALTER COLUMN workspace_id TYPE varchar(64)"
    )
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
        CREATE OR REPLACE FUNCTION marketai_claim_next_onboarding_fact(
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
    # The preceding revision now creates this final schema directly. This
    # compatibility/convergence revision therefore has no independent state to
    # remove when stepping back one revision.
    pass
