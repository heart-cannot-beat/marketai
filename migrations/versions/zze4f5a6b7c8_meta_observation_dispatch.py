"""Let the Worker dispatch due Meta observation schedules (#107, #101 S1).

Revision ID: zze4f5a6b7c8
Revises: zzc2f3a4b5c6

A ``customer_sandbox`` observation contract generation now gets a
``meta_sync_schedule_records`` row (``app.meta.observation``), and the Worker
turns due schedules into ``meta_observation_tick`` Jobs. On PostgreSQL that
first step cannot run in a TenantContext: the schedules are FORCE RLS and the
Worker does not yet know whose they are. ``marketai_claim_next_job_v2`` stays
as reviewed; the maintainer agreed (#101, 2026-11-10) to a second, narrow
entry instead:

``marketai_dispatch_meta_observation_ticks_v1(limit)`` selects due ``active``
schedules whose connection and contract generation are still ``active`` and
whose Workspace is ``active`` and not suspended, ``FOR UPDATE OF schedule SKIP
LOCKED`` so concurrent dispatchers take disjoint rows. It inserts one
``meta_observation_tick`` Job per ``(connection, next_fire_at)`` -- payload
``connection_id``, ``meta_mode`` and ``fire_at``; key
``meta-obs-tick:{connection_id}:{fire_at}``; ``ON CONFLICT DO NOTHING`` -- and
one ``job.enqueued`` audit event per Job it creates, and returns how many it
created. It never moves ``next_fire_at``: the tick Job does that under the
schedule's row lock, in the Workspace's signed TenantContext, as the legacy
``meta_schedule_tick`` does. It reads nothing else and returns no row
content. Like ``marketai_admitted_operation_candidates_v1`` it is owned by the
schema owner, pins ``search_path`` to ``''`` with every name schema-qualified,
and is EXECUTE-able by ``marketai_domain_worker`` alone.

The tick Job creates one Operation per sync run it starts, in the Worker:
``marketai_domain_worker`` gains INSERT on ``operation_records`` (it already
updates them and appends their state events). Forced RLS still scopes every
row to the Worker's Workspace.

A partial index on ``meta_sync_schedule_records (next_fire_at) WHERE state =
'active'`` serves the dispatcher's scan.

Every grant is mirrored in ``deploy/postgres/domain-tenant-roles.sql``; the
release applies that file before and after Alembic, so the grants here are
idempotent repeats on an existing cluster. The previous release keeps running
while this migrates and never calls the function. Downgrade drops the
function and the index and revokes the INSERT. SQLite has no RLS and no
roles: there the Worker reads due schedules directly.
"""

import sqlalchemy as sa
from alembic import op


revision = "zze4f5a6b7c8"
down_revision = "zzc2f3a4b5c6"
branch_labels = None
depends_on = None

_DISPATCH = "public.marketai_dispatch_meta_observation_ticks_v1(integer)"
_INDEX = "ix_msched_active_next_fire"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # m23b creates the Meta tables from the live ORM metadata, so a fresh
    # install already has the index; an upgraded one still needs it (the
    # same convergence ab1_scale_lineage_tables uses).
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("meta_sync_schedule_records")}
    if _INDEX not in existing:
        op.create_index(
            _INDEX,
            "meta_sync_schedule_records",
            ["next_fire_at"],
            postgresql_where=sa.text("state = 'active'"),
            sqlite_where=sa.text("state = 'active'"),
        )
    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_dispatch_meta_observation_ticks_v1(
            p_limit integer
        )
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ''
        AS $$
        DECLARE
            dispatched_at timestamptz := clock_timestamp();
            dispatched integer;
        BEGIN
            WITH due AS (
                SELECT schedule.workspace_id, schedule.meta_mode,
                       schedule.meta_connection_id, schedule.next_fire_at
                  FROM public.meta_sync_schedule_records AS schedule
                  JOIN public.meta_connection_records AS connection
                    ON connection.workspace_id = schedule.workspace_id
                   AND connection.meta_mode = schedule.meta_mode
                   AND connection.id = schedule.meta_connection_id
                  JOIN public.observation_contract_generation_records AS generation
                    ON generation.workspace_id = schedule.workspace_id
                   AND generation.meta_mode = schedule.meta_mode
                   AND generation.id = schedule.contract_generation_id
                  JOIN public.workspaces AS workspace
                    ON workspace.id = schedule.workspace_id
                 WHERE schedule.state = 'active'
                   AND schedule.next_fire_at <= dispatched_at
                   AND connection.state = 'active'
                   AND generation.state = 'active'
                   AND workspace.lifecycle = 'active'
                   AND NOT workspace.suspended
                 ORDER BY schedule.next_fire_at, schedule.id
                 LIMIT LEAST(GREATEST(COALESCE(p_limit, 0), 0), 500)
                   FOR UPDATE OF schedule SKIP LOCKED
            ), slots AS (
                SELECT DISTINCT due.workspace_id, due.meta_mode, due.meta_connection_id,
                       to_char(due.next_fire_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS fire_at
                  FROM due
            ), ticks AS (
                INSERT INTO public.jobs (
                    id, workspace_id, initiator_id, initiator_role, request_id,
                    trace_id, authorization_epoch, type, resource_type, resource_id,
                    payload_version, payload_json, idempotency_key, state,
                    safe_to_retry_after_crash, attempt, max_attempts, error_code,
                    error_detail, created_at, execution_contract_version
                )
                SELECT 'job_mobs_' || md5(slot.workspace_id || ':' || slot.meta_connection_id
                                          || ':' || slot.fire_at),
                       slot.workspace_id, NULL, 'system', '', '',
                       workspace.authorization_epoch, 'meta_observation_tick',
                       'meta_connection', slot.meta_connection_id, 1,
                       json_build_object(
                           'connection_id', slot.meta_connection_id,
                           'meta_mode', slot.meta_mode,
                           'fire_at', slot.fire_at
                       ),
                       'meta-obs-tick:' || slot.meta_connection_id || ':' || slot.fire_at,
                       'queued', true, 0, 3, '', '', dispatched_at, 0
                  FROM slots AS slot
                  JOIN public.workspaces AS workspace
                    ON workspace.id = slot.workspace_id
                ON CONFLICT ON CONSTRAINT uq_job_idempotency DO NOTHING
                RETURNING *
            )
            INSERT INTO public.audit_events (
                id, workspace_id, initiator_id, initiator_role, executor,
                request_id, trace_id, authorization_epoch, meta_mode,
                event_type, resource_type, resource_id, details_json,
                created_at
            )
            SELECT 'audit_' || md5(tick.id || '|job.enqueued'),
                   tick.workspace_id, tick.initiator_id, tick.initiator_role,
                   'system:worker', tick.request_id, tick.trace_id,
                   tick.authorization_epoch, NULL,
                   'job.enqueued', 'job', tick.id,
                   json_build_object(
                       'job_type', tick.type,
                       'resource_type', tick.resource_type,
                       'resource_id', tick.resource_id,
                       'idempotency_key_sha256',
                       encode(sha256(convert_to(tick.idempotency_key, 'UTF8')), 'hex')
                   ),
                   dispatched_at
              FROM ticks AS tick;

            GET DIAGNOSTICS dispatched = ROW_COUNT;
            RETURN dispatched;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_DISPATCH} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DISPATCH} TO marketai_domain_worker")
    op.execute("GRANT INSERT ON TABLE operation_records TO marketai_domain_worker")


def downgrade() -> None:
    if _is_postgres():
        op.execute("REVOKE INSERT ON TABLE operation_records FROM marketai_domain_worker")
        op.execute(f"DROP FUNCTION IF EXISTS {_DISPATCH}")
    op.drop_index(_INDEX, table_name="meta_sync_schedule_records")
