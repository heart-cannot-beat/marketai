"""Keep expired Launch Jobs and Operations crash-consistent.

Revision ID: b79c2d3e4f5a
Revises: a79b1c2d3e4f

The PostgreSQL dispatcher is the production lease reaper.  A Launch worker
persists ``Job.state = running`` before it advances the user-facing Operation,
so a process death can leave the Operation in either ``queued`` or ``running``.
This wrapper resolves that crash window in one database statement, including
both append-only histories, before delegating all other dispatch behaviour to
the previously-installed authenticated dispatcher.
"""
from __future__ import annotations

from alembic import op


revision = "b79c2d3e4f5a"
down_revision = "a79b1c2d3e4f"
branch_labels = None
depends_on = None

_INNER_FUNCTION = "marketai_claim_next_job_v2_before_launch_recovery"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        "ALTER FUNCTION marketai_claim_next_job_v2(text, integer) "
        f"RENAME TO {_INNER_FUNCTION}"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_INNER_FUNCTION}(text, integer) "
        "FROM PUBLIC, marketai_domain_worker"
    )
    op.execute(
        f"""
        CREATE FUNCTION marketai_claim_next_job_v2(
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
            initiator_role varchar,
            request_id varchar,
            trace_id varchar,
            authorization_epoch integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            checked_at timestamptz := clock_timestamp();
        BEGIN
            WITH expired_launch AS MATERIALIZED (
                SELECT job.id AS job_id,
                       job.workspace_id,
                       job.initiator_id,
                       job.initiator_role,
                       job.request_id,
                       job.trace_id,
                       job.authorization_epoch
                  FROM public.jobs AS job
                  JOIN public.workspaces AS workspace
                    ON workspace.id = job.workspace_id
                   AND workspace.authorization_epoch = job.authorization_epoch
                 WHERE job.type = 'launch_provisioning'
                   AND job.state = 'running'
                   AND job.lease_expires_at IS NOT NULL
                   AND job.lease_expires_at < checked_at
                 FOR UPDATE OF job
            ),
            changed_jobs AS (
                UPDATE public.jobs AS job
                   SET state = 'uncertain',
                       error_code = 'paid_call_outcome_unknown',
                       error_detail =
                           'Launch worker lease expired; provisioning outcome is unknown',
                       lease_owner = NULL,
                       lease_expires_at = NULL,
                       heartbeat_at = NULL
                  FROM expired_launch AS expired
                 WHERE job.id = expired.job_id
                RETURNING job.*
            ),
            operation_candidates AS MATERIALIZED (
                SELECT operation.id, operation.workspace_id,
                       operation.state AS operation_from_state
                  FROM public.operation_records AS operation
                  JOIN changed_jobs AS job
                    ON operation.workspace_id = job.workspace_id
                   AND operation.id = job.payload_json->>'operation_id'
                 WHERE operation.state IN ('queued', 'running')
                 FOR UPDATE OF operation
            ),
            changed_operations AS (
                UPDATE public.operation_records AS operation
                   SET state = 'uncertain', updated_at = checked_at
                  FROM operation_candidates AS candidate
                 WHERE operation.id = candidate.id
                   AND operation.workspace_id = candidate.workspace_id
                   AND operation.state IN ('queued', 'running')
                RETURNING operation.id, operation.workspace_id,
                          candidate.operation_from_state
            ),
            operation_event_rows AS (
                SELECT operation.id, operation.workspace_id,
                       'queued'::varchar AS from_state,
                       'running'::varchar AS to_state,
                       'launch dispatch observed during expired worker recovery'::text
                           AS reason,
                       1 AS event_sequence
                  FROM changed_operations AS operation
                 WHERE operation.operation_from_state = 'queued'
                UNION ALL
                SELECT operation.id, operation.workspace_id,
                       'running'::varchar AS from_state,
                       'uncertain'::varchar AS to_state,
                       'launch worker lease expired after dispatch'::text AS reason,
                       2 AS event_sequence
                  FROM changed_operations AS operation
            ),
            operation_events AS (
                INSERT INTO public.operation_state_events (
                    id, workspace_id, operation_id, from_state, to_state,
                    reason, actor, created_at
                )
                SELECT 'ose_' || md5(
                           operation.id || '|launch-lease|' ||
                           operation.event_sequence::text || '|' || checked_at::text
                       ),
                       operation.workspace_id, operation.id,
                       operation.from_state, operation.to_state,
                       operation.reason,
                       'system:worker', checked_at
                  FROM operation_event_rows AS operation
                RETURNING id
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(job.id || '|launch-uncertain|' || checked_at::text),
                   job.workspace_id, job.initiator_id, job.initiator_role,
                   'system:worker', job.request_id, job.trace_id,
                   job.authorization_epoch, 'job.uncertain', 'job', job.id,
                   json_build_object('error_code', job.error_code), checked_at
              FROM changed_jobs AS job;

            RETURN QUERY
            SELECT *
              FROM public.{_INNER_FUNCTION}(claim_owner, lease_seconds);
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_claim_next_job_v2(text, integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_claim_next_job_v2(text, integer) "
        "TO marketai_domain_worker"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP FUNCTION marketai_claim_next_job_v2(text, integer)")
    op.execute(
        f"ALTER FUNCTION {_INNER_FUNCTION}(text, integer) "
        "RENAME TO marketai_claim_next_job_v2"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_claim_next_job_v2(text, integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_claim_next_job_v2(text, integer) "
        "TO marketai_domain_worker"
    )
