"""Add durable audit events and the identity-preserving dispatcher.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = ("audit_events",)
# The Alembic revision marker is deployment metadata, not tenant data. It is
# the only deliberately platform-global relation in the domain database.
PLATFORM_GLOBAL_TABLES = ("alembic_version",)
_AUDIT_FK = "fk_audit_events_workspace_id_workspaces"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_or_harden_audit_table() -> None:
    """Create the table, while also repairing databases that ran the old c7.

    An early, pushed version of c7 created this relation with ``ON DELETE
    CASCADE``.  The conditional path makes this corrective revision safe for
    those review databases as well as for a fresh upgrade from main.
    """
    bind = op.get_bind()
    already_exists = "audit_events" in sa.inspect(bind).get_table_names()
    if not already_exists:
        op.create_table(
            "audit_events",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("workspace_id", sa.String(length=64), nullable=False),
            sa.Column("initiator_id", sa.String(length=100), nullable=True),
            sa.Column("initiator_role", sa.String(length=20), nullable=False),
            sa.Column("executor", sa.String(length=100), nullable=False),
            sa.Column("request_id", sa.String(length=100), nullable=False),
            sa.Column("trace_id", sa.String(length=100), nullable=False),
            sa.Column("authorization_epoch", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("resource_type", sa.String(length=64), nullable=False),
            sa.Column("resource_id", sa.String(length=100), nullable=False),
            sa.Column("details_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"],
                ["workspaces.id"],
                name=op.f(_AUDIT_FK),
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        )
        op.create_index(
            op.f("ix_audit_events_workspace_id"), "audit_events", ["workspace_id"]
        )
        op.create_index(
            op.f("ix_audit_events_event_type"), "audit_events", ["event_type"]
        )
        op.create_index(
            op.f("ix_audit_events_resource_id"), "audit_events", ["resource_id"]
        )
        op.create_index(
            op.f("ix_audit_events_created_at"), "audit_events", ["created_at"]
        )
        return

    # The table can only pre-exist on a review database that ran the pushed c7
    # before this fix. Replace its cascading FK without losing its evidence.
    if _is_postgres():
        op.drop_constraint(_AUDIT_FK, "audit_events", type_="foreignkey")
        op.create_foreign_key(
            _AUDIT_FK,
            "audit_events",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    else:
        with op.batch_alter_table("audit_events") as batch_op:
            batch_op.drop_constraint(_AUDIT_FK, type_="foreignkey")
            batch_op.create_foreign_key(
                _AUDIT_FK,
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="RESTRICT",
            )


def _install_append_only_guard() -> None:
    if not _is_postgres():
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_reject_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_reject_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only');
            END
            """
        )
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_reject_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_reject_audit_mutation() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS audit_events_reject_mutation ON audit_events")
    op.execute(
        """
        CREATE TRIGGER audit_events_reject_mutation
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION marketai_reject_audit_mutation()
        """
    )


def _install_dispatcher() -> None:
    """Install the only unscoped Worker seam and audit its state changes."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION marketai_claim_next_job_v2(
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
            claimed_at timestamptz := clock_timestamp();
            selected_job_id varchar;
            claimed_job public.jobs%ROWTYPE;
        BEGIN
            WITH scheduled AS (
                INSERT INTO public.jobs (
                    id, workspace_id, initiator_id, initiator_role, request_id,
                    trace_id, authorization_epoch, type, resource_type, resource_id,
                    payload_version, payload_json, idempotency_key, state,
                    safe_to_retry_after_crash, attempt, max_attempts, error_code,
                    error_detail, created_at
                )
                SELECT 'job_sched_' || md5(schedule.id || ':' || schedule.next_run_at::text),
                       schedule.workspace_id, NULL, 'system', '', '',
                       workspace.authorization_epoch, 'meta_schedule_tick',
                       'meta_schedule', schedule.id, 1,
                       json_build_object('schedule_id', schedule.id),
                       'meta-schedule:' || schedule.id || ':' || schedule.next_run_at::text,
                       'queued', true, 0, 3, '', '', claimed_at
                  FROM public.meta_sync_schedules AS schedule
                  JOIN public.workspaces AS workspace
                    ON workspace.id = schedule.workspace_id
                 WHERE schedule.enabled AND schedule.next_run_at <= claimed_at
                ON CONFLICT ON CONSTRAINT uq_job_idempotency DO NOTHING
                RETURNING *
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(scheduled_job.id || ':enqueued:' || claimed_at::text),
                   scheduled_job.workspace_id, scheduled_job.initiator_id,
                   scheduled_job.initiator_role, 'system:worker',
                   scheduled_job.request_id, scheduled_job.trace_id,
                   scheduled_job.authorization_epoch, 'job.enqueued',
                   'job', scheduled_job.id,
                   json_build_object('job_type', scheduled_job.type), claimed_at
              FROM scheduled AS scheduled_job;

            WITH changed AS (
                UPDATE public.jobs AS job
                   SET state = 'succeeded', finished_at = claimed_at,
                       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                  FROM public.assets AS asset
                 WHERE job.state IN ('leased', 'running')
                   AND job.lease_expires_at < claimed_at
                   AND job.type = 'generate_asset'
                   AND asset.id = job.payload_json->>'asset_id'
                   AND asset.workspace_id = job.workspace_id
                   AND asset.status = 'ok'
                RETURNING job.*
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(changed_job.id || ':recovered:' || claimed_at::text),
                   changed_job.workspace_id, changed_job.initiator_id,
                   changed_job.initiator_role, 'system:worker',
                   changed_job.request_id, changed_job.trace_id,
                   changed_job.authorization_epoch, 'job.recovered_succeeded',
                   'job', changed_job.id, '{}'::json, claimed_at
              FROM changed AS changed_job;

            WITH changed AS (
                UPDATE public.jobs AS job
                   SET state = 'failed', finished_at = claimed_at,
                       error_code = 'authorization_epoch_stale',
                       error_detail = 'The authorization epoch changed after this job was enqueued',
                       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                  FROM public.workspaces AS workspace
                 WHERE workspace.id = job.workspace_id
                   AND workspace.authorization_epoch <> job.authorization_epoch
                   AND (
                        job.state IN ('queued', 'retry_wait')
                        OR (job.state IN ('leased', 'running') AND job.lease_expires_at < claimed_at)
                   )
                RETURNING job.*
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(changed_job.id || ':stale:' || claimed_at::text),
                   changed_job.workspace_id, changed_job.initiator_id,
                   changed_job.initiator_role, 'system:worker',
                   changed_job.request_id, changed_job.trace_id,
                   changed_job.authorization_epoch,
                   'job.authorization_rejected', 'job', changed_job.id,
                   json_build_object('error_code', changed_job.error_code), claimed_at
              FROM changed AS changed_job;

            WITH changed AS (
                UPDATE public.jobs AS job
                   SET state = 'queued', error_code = 'lease_expired',
                       error_detail = 'Worker lease expired; job safely requeued',
                       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                 WHERE job.state IN ('leased', 'running')
                   AND job.lease_expires_at < claimed_at
                   AND (job.state = 'leased' OR job.safe_to_retry_after_crash)
                RETURNING job.*
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(changed_job.id || ':requeued:' || claimed_at::text),
                   changed_job.workspace_id, changed_job.initiator_id,
                   changed_job.initiator_role, 'system:worker',
                   changed_job.request_id, changed_job.trace_id,
                   changed_job.authorization_epoch, 'job.requeued',
                   'job', changed_job.id,
                   json_build_object('error_code', changed_job.error_code), claimed_at
              FROM changed AS changed_job;

            WITH changed AS (
                UPDATE public.jobs AS job
                   SET state = 'uncertain', error_code = 'paid_call_outcome_unknown',
                       error_detail = 'External paid call outcome is unknown; automatic retry refused',
                       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                 WHERE job.state = 'running'
                   AND job.lease_expires_at < claimed_at
                   AND NOT job.safe_to_retry_after_crash
                RETURNING job.*
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            )
            SELECT 'audit_' || md5(changed_job.id || ':uncertain:' || claimed_at::text),
                   changed_job.workspace_id, changed_job.initiator_id,
                   changed_job.initiator_role, 'system:worker',
                   changed_job.request_id, changed_job.trace_id,
                   changed_job.authorization_epoch, 'job.uncertain',
                   'job', changed_job.id,
                   json_build_object('error_code', changed_job.error_code), claimed_at
              FROM changed AS changed_job;

            SELECT queued.id INTO selected_job_id
              FROM public.jobs AS queued
             WHERE queued.state IN ('queued', 'retry_wait')
               AND (queued.next_attempt_at IS NULL OR queued.next_attempt_at <= claimed_at)
             ORDER BY queued.created_at, queued.id
             FOR UPDATE SKIP LOCKED
             LIMIT 1;

            IF selected_job_id IS NULL THEN
                RETURN;
            END IF;

            UPDATE public.jobs AS queued
               SET state = 'leased', lease_owner = claim_owner,
                   lease_expires_at = claimed_at + make_interval(secs => lease_seconds),
                   heartbeat_at = claimed_at, attempt = queued.attempt + 1,
                   started_at = COALESCE(queued.started_at, claimed_at),
                   error_code = '', error_detail = ''
             WHERE queued.id = selected_job_id
            RETURNING queued.* INTO claimed_job;

            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, event_type,
                resource_type, resource_id, details_json, created_at
            ) VALUES (
                'audit_' || md5(claimed_job.id || ':leased:' || claimed_at::text),
                claimed_job.workspace_id, claimed_job.initiator_id,
                claimed_job.initiator_role, 'system:worker', claimed_job.request_id,
                claimed_job.trace_id, claimed_job.authorization_epoch, 'job.leased',
                'job', claimed_job.id,
                json_build_object('lease_owner', claim_owner, 'attempt', claimed_job.attempt),
                claimed_at
            );

            RETURN QUERY SELECT claimed_job.id, claimed_job.type,
                claimed_job.payload_json, claimed_job.attempt, claimed_job.max_attempts,
                claimed_job.workspace_id, claimed_job.initiator_id,
                claimed_job.initiator_role, claimed_job.request_id,
                claimed_job.trace_id, claimed_job.authorization_epoch;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION marketai_claim_next_job(text, integer) FROM marketai_domain_worker")
    op.execute("REVOKE ALL ON FUNCTION marketai_claim_next_job_v2(text, integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_claim_next_job_v2(text, integer) "
        "TO marketai_domain_worker"
    )


def upgrade() -> None:
    _create_or_harden_audit_table()
    _install_append_only_guard()

    if not _is_postgres():
        return

    op.execute('ALTER TABLE "audit_events" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "audit_events" FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "audit_events"')
    op.execute(
        '''CREATE POLICY tenant_isolation ON "audit_events"
           USING (marketai_context_is_current(workspace_id))
           WITH CHECK (marketai_context_is_current(workspace_id))'''
    )
    op.execute("GRANT INSERT ON TABLE audit_events TO marketai_domain_api")
    op.execute("GRANT INSERT ON TABLE audit_events TO marketai_domain_worker")
    op.execute("GRANT SELECT ON TABLE audit_events TO marketai_domain_readonly")
    _install_dispatcher()


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP FUNCTION IF EXISTS marketai_claim_next_job_v2(text, integer)")
        op.execute(
            "GRANT EXECUTE ON FUNCTION marketai_claim_next_job(text, integer) "
            "TO marketai_domain_worker"
        )
        op.execute("DROP TRIGGER IF EXISTS audit_events_reject_mutation ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS marketai_reject_audit_mutation()")
        op.execute('DROP POLICY IF EXISTS tenant_isolation ON "audit_events"')
        op.execute('ALTER TABLE "audit_events" NO FORCE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "audit_events" DISABLE ROW LEVEL SECURITY')
    else:
        op.execute("DROP TRIGGER IF EXISTS audit_events_reject_update")
        op.execute("DROP TRIGGER IF EXISTS audit_events_reject_delete")
    op.drop_table("audit_events")
