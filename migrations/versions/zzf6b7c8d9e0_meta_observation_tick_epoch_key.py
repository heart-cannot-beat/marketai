"""Key each observation tick by its authorization epoch too (#119, a #101 S1 follow-up).

Revision ID: zzf6b7c8d9e0
Revises: zzf5a6b7c8d9

``marketai_dispatch_meta_observation_ticks_v1`` (zze4f5a6b7c8) inserts one
``meta_observation_tick`` Job per ``(connection, next_fire_at)``, keyed
``meta-obs-tick:{connection_id}:{fire_at}`` with ``ON CONFLICT DO NOTHING``,
and never moves ``next_fire_at``: only the tick does. When the Workspace's
authorization epoch changes between dispatch and claim,
``marketai_claim_next_job_v2`` fails the tick as
``authorization_epoch_stale``. The slot stays due, every later dispatch
lands on the same key and inserts nothing, and the connection is never read
again.

The tick's Job id and key now also name the epoch the Job carries:
``meta-obs-tick:{connection_id}:{fire_at}:{authorization_epoch}``. A slot
still gets exactly one tick per epoch -- replays and concurrent dispatchers
converge on it as before -- and a slot whose tick an epoch change rejected
gets one tick under the new epoch at the next dispatch. A second tick for a
slot that already ran is a no-op: the tick re-checks the persisted fire time
under the schedule rows' lock. The Job id is derived the same way, so the
insert still conflicts on ``uq_job_idempotency`` and not on the primary key.

Nothing else changes: same signature, same rows read (the epoch comes from
the ``workspaces`` row the function already joins), same ``job.enqueued``
audit event, same owner, ``search_path`` and EXECUTE grant. ``CREATE OR
REPLACE`` keeps the ACL; the REVOKE/GRANT here are idempotent repeats of
``deploy/postgres/domain-tenant-roles.sql``, which already names this
signature. ``marketai_claim_next_job_v2`` is untouched (#101).

Only the tick needed this. The batch Job (``meta-obs-sync:{connection_id}:
{D}:1``) and Scale's collection Job (``scale-collect:{connection_id}:{D}:
{run_generation}``) are each enqueued once, by the Job before them, and
no schedule waits on them: a rejected one loses day D, the next day's
tick fails D's leftover runs (#109) and enqueues D+1 under new keys, and
the daily read's 14-day lookback covers D again.

Rollout: the previous release's Worker calls the replaced function and
gets the new keys; only its tick's Q9 fast-forward (``enqueue_tick``) still
builds the old one. A slot dispatched across the switch can therefore get
two ticks, and the second finds the slot moved and changes nothing. A
schedule that already stalled behind a rejected tick gets a new tick at the
first dispatch after the upgrade: its old key has no epoch. Downgrade
restores the zze4f5a6b7c8 body, and with it the stall. SQLite has no
function: there ``app.meta.observation_sync.enqueue_tick`` builds the same
key.
"""

from alembic import op


revision = "zzf6b7c8d9e0"
down_revision = "zzf5a6b7c8d9"
branch_labels = None
depends_on = None

_DISPATCH = "public.marketai_dispatch_meta_observation_ticks_v1(integer)"

# The tick's Job id and idempotency key, per revision. ``slot`` is a due
# (workspace, mode, connection, fire_at) and ``workspace`` its Workspace row.
_EPOCH_KEYED = (
    "'job_mobs_' || md5(slot.workspace_id || ':' || slot.meta_connection_id"
    " || ':' || slot.fire_at || ':' || workspace.authorization_epoch)",
    "'meta-obs-tick:' || slot.meta_connection_id || ':' || slot.fire_at"
    " || ':' || workspace.authorization_epoch",
)
_SLOT_KEYED = (  # zze4f5a6b7c8
    "'job_mobs_' || md5(slot.workspace_id || ':' || slot.meta_connection_id"
    " || ':' || slot.fire_at)",
    "'meta-obs-tick:' || slot.meta_connection_id || ':' || slot.fire_at",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _dispatch_function(job_id: str, idempotency_key: str) -> str:
    return f"""
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
                SELECT {job_id},
                       slot.workspace_id, NULL, 'system', '', '',
                       workspace.authorization_epoch, 'meta_observation_tick',
                       'meta_connection', slot.meta_connection_id, 1,
                       json_build_object(
                           'connection_id', slot.meta_connection_id,
                           'meta_mode', slot.meta_mode,
                           'fire_at', slot.fire_at
                       ),
                       {idempotency_key},
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


def _replace(job_id: str, idempotency_key: str) -> None:
    op.execute(_dispatch_function(job_id, idempotency_key))
    op.execute(f"REVOKE ALL ON FUNCTION {_DISPATCH} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DISPATCH} TO marketai_domain_worker")


def upgrade() -> None:
    if _is_postgres():
        _replace(*_EPOCH_KEYED)


def downgrade() -> None:
    if _is_postgres():
        _replace(*_SLOT_KEYED)
