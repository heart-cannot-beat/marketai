"""Freeze job identity, reject stale scopes and make parent FKs tenant-safe.

Revision ID: c7d8e9f0a1b2
Revises: f1a2c3d4e5b6
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c7d8e9f0a1b2"
down_revision = "f1a2c3d4e5b6"
branch_labels = None
depends_on = None


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


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(
            sa.Column(
                "authorization_epoch", sa.Integer(), nullable=False, server_default="0"
            )
        )

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("initiator_id", sa.String(100), nullable=True))
        batch_op.add_column(
            sa.Column("initiator_role", sa.String(20), nullable=False, server_default="system")
        )
        batch_op.add_column(
            sa.Column("request_id", sa.String(100), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("trace_id", sa.String(100), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("authorization_epoch", sa.Integer(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.drop_constraint("uq_import_batches_source_hash", type_="unique")
        batch_op.create_unique_constraint(
            "uq_import_batch_workspace_source", ["workspace_id", "source_hash"]
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.create_unique_constraint(
            "uq_products_workspace_id_id", ["workspace_id", "id"]
        )

    with op.batch_alter_table("assets") as batch_op:
        batch_op.drop_constraint(
            "fk_assets_product_id_products", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_assets_workspace_product_products",
            "products",
            ["workspace_id", "product_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_context_is_current(candidate_workspace text)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT candidate_workspace = marketai_current_workspace()
               AND EXISTS (
                    SELECT 1
                    FROM public.workspaces AS workspace
                    WHERE workspace.id = candidate_workspace
                      AND workspace.authorization_epoch = COALESCE(
                            NULLIF(current_setting('marketai.authorization_epoch', true), '')::integer,
                            -1
                      )
               )
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_context_is_current(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_context_is_current(text) "
        "TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
    )

    for table in WORKSPACE_OWNED:
        op.execute(
            f'''ALTER POLICY tenant_isolation ON "{table}"
                USING (marketai_context_is_current(workspace_id))
                WITH CHECK (marketai_context_is_current(workspace_id))'''
        )
    op.execute(
        '''ALTER POLICY tenant_isolation ON "workspaces"
           USING (marketai_context_is_current(id))
           WITH CHECK (marketai_context_is_current(id))'''
    )

    # The Worker cannot set a tenant before it has received a Job.  This is the
    # only cross-tenant dispatcher seam: a SECURITY DEFINER function claims one
    # row and returns the frozen identity; every subsequent statement runs via
    # the ordinary Worker role under that TenantContext and RLS.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_claim_next_job(
            claim_owner text,
            lease_seconds integer
        )
        RETURNS TABLE (
            job_id varchar,
            job_type varchar,
            payload_json json,
            attempt integer,
            max_attempts integer,
            workspace_id varchar,
            initiator_id varchar,
            request_id varchar,
            trace_id varchar,
            authorization_epoch integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            claimed_at timestamptz := clock_timestamp();
        BEGIN
            INSERT INTO public.jobs (
                id, workspace_id, initiator_id, initiator_role, request_id,
                trace_id, authorization_epoch, type, resource_type, resource_id,
                payload_version, payload_json, idempotency_key, state,
                safe_to_retry_after_crash, attempt, max_attempts, error_code,
                error_detail, created_at
            )
            SELECT 'job_sched_' || md5(schedule.id || ':' || schedule.next_run_at::text),
                   schedule.workspace_id,
                   NULL,
                   'system',
                   '',
                   '',
                   workspace.authorization_epoch,
                   'meta_schedule_tick',
                   'meta_schedule',
                   schedule.id,
                   1,
                   json_build_object('schedule_id', schedule.id),
                   'meta-schedule:' || schedule.id || ':' || schedule.next_run_at::text,
                   'queued',
                   true,
                   0,
                   3,
                   '',
                   '',
                   claimed_at
              FROM public.meta_sync_schedules AS schedule
              JOIN public.workspaces AS workspace ON workspace.id = schedule.workspace_id
             WHERE schedule.enabled
               AND schedule.next_run_at <= claimed_at
            ON CONFLICT ON CONSTRAINT uq_job_idempotency DO NOTHING;

            UPDATE public.jobs AS job
               SET state = 'succeeded',
                   finished_at = claimed_at,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   heartbeat_at = NULL
              FROM public.assets AS asset
             WHERE job.state IN ('leased', 'running')
               AND job.lease_expires_at < claimed_at
               AND asset.id = job.payload_json->>'asset_id'
               AND asset.status = 'ok';

            -- A queued job is a frozen authorization decision, not a bearer
            -- token that survives a Workspace membership change forever.
            -- Reject it before returning any identity to the Worker.  A job
            -- that was already executing is allowed to hold its lease until
            -- expiry (the tenant-scoped heartbeat and writes fail closed), at
            -- which point the dispatcher terminates it instead of requeueing
            -- it under an obsolete epoch.
            UPDATE public.jobs AS job
               SET state = 'failed',
                   finished_at = claimed_at,
                   error_code = 'authorization_epoch_stale',
                   error_detail = 'The authorization epoch changed after this job was enqueued',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   heartbeat_at = NULL
              FROM public.workspaces AS workspace
             WHERE workspace.id = job.workspace_id
               AND workspace.authorization_epoch <> job.authorization_epoch
               AND (
                    job.state IN ('queued', 'retry_wait')
                    OR (
                        job.state IN ('leased', 'running')
                        AND job.lease_expires_at < claimed_at
                    )
               );

            UPDATE public.jobs AS job
               SET state = 'queued',
                   error_code = 'lease_expired',
                   error_detail = 'Worker lease expired; job safely requeued',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   heartbeat_at = NULL
             WHERE job.state IN ('leased', 'running')
               AND job.lease_expires_at < claimed_at
               AND (job.state = 'leased' OR job.safe_to_retry_after_crash);

            UPDATE public.jobs AS job
               SET state = 'uncertain',
                   error_code = 'paid_call_outcome_unknown',
                   error_detail = 'External paid call outcome is unknown; automatic retry refused',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   heartbeat_at = NULL
             WHERE job.state = 'running'
               AND job.lease_expires_at < claimed_at
               AND NOT job.safe_to_retry_after_crash;

            RETURN QUERY
            WITH candidate AS (
                SELECT queued.id
                  FROM public.jobs AS queued
                 WHERE queued.state IN ('queued', 'retry_wait')
                   AND (queued.next_attempt_at IS NULL OR queued.next_attempt_at <= claimed_at)
                 ORDER BY queued.created_at, queued.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            ), claimed AS (
                UPDATE public.jobs AS queued
                   SET state = 'leased',
                       lease_owner = claim_owner,
                       lease_expires_at = claimed_at + make_interval(secs => lease_seconds),
                       heartbeat_at = claimed_at,
                       attempt = queued.attempt + 1,
                       started_at = COALESCE(queued.started_at, claimed_at),
                       error_code = '',
                       error_detail = ''
                  FROM candidate
                 WHERE queued.id = candidate.id
                RETURNING queued.*
            )
            SELECT claimed.id,
                   claimed.type,
                   claimed.payload_json,
                   claimed.attempt,
                   claimed.max_attempts,
                   claimed.workspace_id,
                   claimed.initiator_id,
                   claimed.request_id,
                   claimed.trace_id,
                   claimed.authorization_epoch
              FROM claimed;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_claim_next_job(text, integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_claim_next_job(text, integer) "
        "TO marketai_domain_worker"
    )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP FUNCTION IF EXISTS marketai_claim_next_job(text, integer)")
        for table in WORKSPACE_OWNED:
            op.execute(
                f'''ALTER POLICY tenant_isolation ON "{table}"
                    USING (workspace_id = marketai_current_workspace())
                    WITH CHECK (workspace_id = marketai_current_workspace())'''
            )
        op.execute(
            '''ALTER POLICY tenant_isolation ON "workspaces"
               USING (id = marketai_current_workspace())
               WITH CHECK (id = marketai_current_workspace())'''
        )
        op.execute("DROP FUNCTION IF EXISTS marketai_context_is_current(text)")

    with op.batch_alter_table("assets") as batch_op:
        batch_op.drop_constraint(
            "fk_assets_workspace_product_products", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_assets_product_id_products",
            "products",
            ["product_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint("uq_products_workspace_id_id", type_="unique")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("authorization_epoch")
        batch_op.drop_column("trace_id")
        batch_op.drop_column("request_id")
        batch_op.drop_column("initiator_role")
        batch_op.drop_column("initiator_id")
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.drop_constraint("uq_import_batch_workspace_source", type_="unique")
        batch_op.create_unique_constraint(
            "uq_import_batches_source_hash", ["source_hash"]
        )
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.drop_column("authorization_epoch")
