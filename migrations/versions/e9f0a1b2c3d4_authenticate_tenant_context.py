"""Authenticate tenant settings and harden expired-job recovery.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
"""
from __future__ import annotations

from alembic import op


revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


# Deployment metadata and the owner-only HMAC key are deliberately global.
# Neither is domain data and neither is granted to a runtime role.
PLATFORM_GLOBAL_TABLES = ("alembic_version", "tenant_context_secrets")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE tenant_context_secrets (
            key_id smallint PRIMARY KEY CHECK (key_id = 1),
            secret text NOT NULL CHECK (octet_length(secret) >= 32)
        )
        """
    )
    op.execute("REVOKE ALL ON TABLE tenant_context_secrets FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON TABLE tenant_context_secrets FROM "
        "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
    )

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
    op.execute("REVOKE ALL ON FUNCTION marketai_context_is_current(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_context_is_current(text) "
        "TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
    )

    # Preserve the old implementation as an owner-only inner function. The
    # corrected wrapper terminates stale or cross-tenant recovery candidates
    # before the legacy function can inspect completion side effects.
    op.execute(
        "ALTER FUNCTION marketai_claim_next_job_v2(text, integer) "
        "RENAME TO marketai_claim_next_job_v2_legacy"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_claim_next_job_v2_legacy(text, integer) "
        "FROM PUBLIC, marketai_domain_worker"
    )
    op.execute(
        """
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
            WITH changed AS (
                UPDATE public.jobs AS job
                   SET state = 'failed', finished_at = checked_at,
                       error_code = 'authorization_epoch_stale',
                       error_detail = 'The authorization epoch changed after this job was enqueued',
                       lease_owner = NULL, lease_expires_at = NULL,
                       heartbeat_at = NULL
                  FROM public.workspaces AS workspace
                 WHERE workspace.id = job.workspace_id
                   AND workspace.authorization_epoch <> job.authorization_epoch
                   AND (
                        job.state IN ('queued', 'retry_wait')
                        OR (
                            job.state IN ('leased', 'running')
                            AND job.lease_expires_at < checked_at
                        )
                   )
                RETURNING job.*
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(changed.id || '|stale-v3|' || checked_at::text),
                   changed.workspace_id, changed.initiator_id,
                   changed.initiator_role, 'system:worker', changed.request_id,
                   changed.trace_id, changed.authorization_epoch,
                   'job.authorization_rejected', 'job', changed.id,
                   json_build_object('error_code', changed.error_code), checked_at
              FROM changed;

            WITH invalid AS (
                UPDATE public.jobs AS job
                   SET state = CASE
                            WHEN job.state = 'leased'
                              OR job.safe_to_retry_after_crash THEN 'queued'
                            ELSE 'uncertain'
                       END,
                       error_code = CASE
                            WHEN job.state = 'leased'
                              OR job.safe_to_retry_after_crash THEN 'lease_expired'
                            ELSE 'paid_call_outcome_unknown'
                       END,
                       error_detail = CASE
                            WHEN job.state = 'leased'
                              OR job.safe_to_retry_after_crash
                              THEN 'Worker lease expired; job safely requeued'
                            ELSE 'External paid call outcome is unknown; automatic retry refused'
                       END,
                       lease_owner = NULL, lease_expires_at = NULL,
                       heartbeat_at = NULL
                  FROM public.assets AS asset
                 WHERE job.state IN ('leased', 'running')
                   AND job.lease_expires_at < checked_at
                   AND asset.id = job.payload_json->>'asset_id'
                   AND asset.status = 'ok'
                   AND (
                        job.type <> 'generate_asset'
                        OR asset.workspace_id <> job.workspace_id
                   )
                RETURNING job.*
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(invalid.id || '|invalid-recovery|' || checked_at::text),
                   invalid.workspace_id, invalid.initiator_id,
                   invalid.initiator_role, 'system:worker', invalid.request_id,
                   invalid.trace_id, invalid.authorization_epoch,
                   CASE WHEN invalid.state = 'queued' THEN 'job.requeued'
                        ELSE 'job.uncertain' END,
                   'job', invalid.id,
                   json_build_object('error_code', invalid.error_code), checked_at
              FROM invalid;

            RETURN QUERY
            SELECT *
              FROM public.marketai_claim_next_job_v2_legacy(
                    claim_owner, lease_seconds
              );
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_claim_next_job_v2(text, integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_claim_next_job_v2(text, integer) "
        "TO marketai_domain_worker"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP FUNCTION marketai_claim_next_job_v2(text, integer)")
    op.execute(
        "ALTER FUNCTION marketai_claim_next_job_v2_legacy(text, integer) "
        "RENAME TO marketai_claim_next_job_v2"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_claim_next_job_v2(text, integer) "
        "TO marketai_domain_worker"
    )
    op.execute("DROP TABLE tenant_context_secrets")
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
                    SELECT 1 FROM public.workspaces AS workspace
                    WHERE workspace.id = candidate_workspace
                      AND workspace.authorization_epoch = COALESCE(
                            NULLIF(current_setting('marketai.authorization_epoch', true), '')::integer,
                            -1
                      )
               )
        $$
        """
    )
