# app/meta/observation_sync.py
"""Scheduled observation sync: dispatch, tick and batch executor (#107, #101 S1).

Three steps take a due ``MetaObservationSyncScheduleRecord`` to published,
attested evidence, each a durable Job the Worker runs:

1. **Dispatch** (:func:`dispatch_due_observation_ticks`). The main Worker lane
   calls it at most once a minute. On PostgreSQL the schedules are FORCE RLS
   and the Worker has no tenant to read them under, so it calls the narrow
   SECURITY DEFINER ``marketai_dispatch_meta_observation_ticks_v1``: due
   active schedules (``FOR UPDATE SKIP LOCKED``) become one
   ``meta_observation_tick`` Job per ``(connection, fire time, authorization
   epoch)``, keyed ``meta-obs-tick:{connection_id}:{fire_at}:{epoch}`` with
   ``ON CONFLICT DO NOTHING`` and a ``job.enqueued`` audit event. It never
   moves ``next_fire_at``. The epoch is in the key because the claim fails a
   tick whose epoch went stale (``authorization_epoch_stale``) while its slot
   stays due: the next dispatch gives that slot one tick under the new epoch
   instead of finding the rejected one and stalling the schedule. On SQLite
   :func:`app.meta.sync_pipeline.dispatch_schedules` reads the same rows and
   the Jobs go through ``enqueue_in_session``.
2. **Tick** (:func:`run_observation_tick`), in a TenantContext with the Meta
   mode from the payload. Under the schedule rows' lock it re-checks that the
   persisted fire time is still the one due, advances ``next_fire_at``, and
   for every query instance of every due generation's read plan
   (:func:`app.meta.observation.observation_read_plan`, #101 S2) creates one
   Operation and one sync run for target day D (the fire time's local date
   minus one -- from the persisted fire time, never the clock), occurrence
   ``obs:{generation_id}:{D}:{instance}``. Then it enqueues one
   ``meta_observation_sync`` batch Job for the connection and day. A
   generation that no longer matches the Scale read contract it names gets
   no run and an audit event instead. A fire time older than the latest due
   slot (the Worker was down) is not run: the schedule jumps to the latest
   slot and a tick for that slot is enqueued instead (#101 Q9). Everything
   commits in one transaction.
3. **Batch** (:func:`run_observation_sync`). One Job per connection and day
   drives each of its runs to a terminal state, page by page: resolve the
   adapter, commit a call intent and a ``started`` fetch attempt, read the
   page, then record the outcome and the page and advance the run's cursor
   checkpoint in one transaction (§6.3-§6.4). Each query instance has its own
   run and so its own page chain; the configuration instance pages through
   its edges (account, ads, ad sets, campaigns) in order, its checkpoint
   naming the edge. The terminal page, the run's ``succeeded`` transition,
   the publication and the attestation (watermarked with the instance's
   window) commit together (§6.5), with the Operation settled in the same
   transaction; the coverage-ledger entry for D is written with the last of
   a generation's instances to succeed. A Worker that dies mid-run resumes
   from the last committed page. A run still failing on the Job's last
   attempt is marked ``failed`` and gets no attestation; the other runs of
   the batch finish as usual.

Every write runs in a TenantContext signed for the Job's Workspace and the
payload's Meta mode, under the same RLS as any Worker transaction. The
executor only ever reads through :class:`MetaObservationReadPort`; the
adapter is resolved per transaction and never held by the Worker runtime
(ADR 0017).
"""
from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..adapters.persistence.database import platform_session, tenant_session
from ..adapters.persistence.models import JobRecord, WorkspaceRecord, new_id, utcnow
from ..platform.audit import append_event
from ..platform.jobs import enqueue_in_session
from ..platform.operations import (
    OperationRecord,
    OperationResultRef,
    OperationState,
    create_operation,
    transition_operation,
)
from ..platform.runtime_role import WORKER_ROLE, runs_as
from ..platform.tenancy import (
    VALID_META_MODES,
    TenantContext,
    apply_to_session,
    commit_and_rebind,
    is_postgres,
    system_context,
)
from .credential_resolution import create_call_intent, resolve_adapter
from .evidence import create_attestation, publish_evidence
from .models import (
    EvidenceAttestationRecord,
    MetaCallOutcomeRecord,
    MetaConnectionRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
)
from .observation import (
    ReadPlan,
    latest_fire_at_or_before,
    next_fire_after,
    observation_read_plan,
    observation_target_date,
    record_coverage,
)
from .scale_read_contracts import (
    CONFIGURATION_EDGES,
    CONFIGURATION_READ,
    PAGE_LIMIT,
    QueryInstance,
    ScaleReadContractError,
)
from .sync_pipeline import (
    TERMINAL_RUN_STATES,
    complete_sync_run,
    create_sync_run,
    dispatch_schedules,
    fail_sync_run,
    record_page,
)

__all__ = [
    "TICK_JOB_TYPE",
    "SYNC_JOB_TYPE",
    "LOOKBACK_DAYS",
    "ObservationSyncError",
    "ObservationBatchFailed",
    "ObservationLeaseLost",
    "MetaObservationReadPort",
    "MetaInsightsReadPort",
    "TickResult",
    "BatchResult",
    "dispatch_due_observation_ticks",
    "enqueue_tick",
    "observation_job_context",
    "run_observation_tick",
    "run_observation_sync",
    "fire_at_text",
    "parse_fire_at",
    "tick_idempotency_key",
    "sync_idempotency_key",
    "occurrence_id",
]

TICK_JOB_TYPE = "meta_observation_tick"
SYNC_JOB_TYPE = "meta_observation_sync"
DISPATCH_FUNCTION = "public.marketai_dispatch_meta_observation_ticks_v1"
DISPATCH_BATCH_SIZE = 100
#: The main Worker lane dispatches at most this often, busy or idle (#101 Q4).
DISPATCH_INTERVAL_SECONDS = 60.0
#: The daily contract re-reads 14 local days ending with D (#101 Q3), so
#: Meta's revisions of earlier days reach the next sync on their own.
LOOKBACK_DAYS = 14
RUN_GENERATION = 1
SYNC_COMMAND_TYPE = "meta.observation_sync"
#: Spec §8.6 attribution: the dispatcher creates runs, the Worker reads.
SCHEDULER_EXECUTOR = "scheduler:meta-sync"
READ_EXECUTOR = "worker:meta-observation-sync"
_FIRE_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ObservationSyncError(RuntimeError):
    """A scheduled-sync Job that cannot run as given (``code`` is stable)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ObservationLeaseLost(RuntimeError):
    """The batch Job's lease is gone; another claim owns the work now."""


class ObservationBatchFailed(RuntimeError):
    """Runs of a batch did not finish. ``final`` when they were marked failed."""

    def __init__(self, failures: dict[str, str], *, final: bool) -> None:
        self.failures = dict(failures)
        self.final = final
        detail = "; ".join(f"{run_id}: {reason}" for run_id, reason in sorted(failures.items()))
        super().__init__(f"{len(failures)} sync run(s) did not finish: {detail}")


class MetaObservationReadPort(Protocol):
    """The only adapter capabilities the executor uses: two reads."""

    def read_insights(
        self,
        *,
        ad_account_id: str,
        fields: Sequence[str],
        since: date,
        until: date,
        limit: int = ...,
        after: str | None = None,
        time_increment: str = ...,
        breakdowns: Sequence[str] = ...,
        action_attribution_windows: Sequence[str] = ...,
        action_report_time: str | None = ...,
    ) -> Any: ...

    def read_delivery_configuration(
        self,
        *,
        ad_account_id: str,
        edge: str,
        fields: Sequence[str],
        as_of: date,
        limit: int = ...,
        after: str | None = None,
    ) -> Any: ...


#: The #107 name of the port.
MetaInsightsReadPort = MetaObservationReadPort


@dataclass(frozen=True)
class TickResult:
    status: str
    target_dates: tuple[date, ...] = ()
    run_ids: tuple[str, ...] = ()
    sync_job_ids: tuple[str, ...] = ()
    next_tick_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchResult:
    run_ids: tuple[str, ...]
    succeeded: tuple[str, ...]


@dataclass(frozen=True)
class _Slot:
    workspace_id: str
    authorization_epoch: int
    meta_mode: str
    connection_id: str
    fire_at: datetime


# ---------------------------------------------------------------------------
# Keys and payloads
# ---------------------------------------------------------------------------


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def fire_at_text(moment: datetime) -> str:
    """The fire time as the dispatch function writes it (UTC, microseconds)."""
    return _as_utc(moment).strftime(_FIRE_AT_FORMAT)


def parse_fire_at(value: str) -> datetime:
    return datetime.strptime(value, _FIRE_AT_FORMAT).replace(tzinfo=timezone.utc)


def tick_idempotency_key(connection_id: str, fire_at: datetime, *, authorization_epoch: int) -> str:
    """One tick per slot and authorization epoch, as the dispatch function keys it.

    A tick the claim rejects as ``authorization_epoch_stale`` leaves its slot
    due; under the new epoch the slot gets a new key, so it is dispatched
    again rather than stalled behind the rejected Job.
    """
    return f"meta-obs-tick:{connection_id}:{fire_at_text(fire_at)}:{int(authorization_epoch)}"


def sync_idempotency_key(connection_id: str, target_date: date, run_generation: int) -> str:
    return f"meta-obs-sync:{connection_id}:{target_date.isoformat()}:{run_generation}"


def occurrence_id(generation_id: str, target_date: date, instance: str | None = None) -> str:
    """``obs:{generation_id}:{D}:{instance}`` (#101 S2); #107 runs have no instance."""
    base = f"obs:{generation_id}:{target_date.isoformat()}"
    return base if instance is None else f"{base}:{instance}"


def _occurrence_of(run: MetaObservationSyncRunRecord) -> tuple[date, str | None]:
    """Target day and query instance of a scheduled run."""
    parts = run.occurrence_id.split(":")
    if parts[0] != "obs" or len(parts) not in (3, 4) or parts[1] != run.contract_generation_id:
        raise ObservationSyncError("meta_sync_occurrence_invalid", f"run {run.id} is not a scheduled occurrence")
    try:
        target_date = date.fromisoformat(parts[2])
    except ValueError:
        raise ObservationSyncError(
            "meta_sync_occurrence_invalid", f"run {run.id} is not a scheduled occurrence",
        ) from None
    return target_date, (parts[3] if len(parts) == 4 else None)


def _plan_occurrences(generation_id: str, target_date: date, plan: ReadPlan) -> list[str]:
    occurrences = [occurrence_id(generation_id, target_date, instance.name) for instance in plan.instances]
    if len(plan.instances) == 1:
        occurrences.append(occurrence_id(generation_id, target_date))  # a #107 run
    return occurrences


def observation_job_context(claimed: TenantContext, payload: dict[str, Any]) -> TenantContext:
    """The claimed Job's context with the payload's Meta mode.

    The Job row records no Meta mode, and RLS shows Meta rows only to a
    context that names one; a payload without a valid mode cannot run.
    """
    mode = str(payload.get("meta_mode") or "")
    if mode not in VALID_META_MODES:
        raise ObservationSyncError("missing_input", "missing or invalid meta_mode")
    return replace(claimed, meta_mode=mode)


def _scheduler_context(workspace_id: str, authorization_epoch: int, meta_mode: str) -> TenantContext:
    return replace(
        system_context(workspace_id, initiator_id=None, authorization_epoch=authorization_epoch),
        meta_mode=meta_mode,
    )


# ---------------------------------------------------------------------------
# 1. Dispatch
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def dispatch_due_observation_ticks(*, limit: int = DISPATCH_BATCH_SIZE, now: datetime | None = None) -> int:
    """Enqueue one tick Job per due ``(connection, fire time)``; return how many are new.

    Replays and concurrent dispatchers converge on the same Jobs: the tick
    key names the connection, the persisted fire time and the Workspace's
    authorization epoch, and nothing here moves ``next_fire_at``. A slot whose
    tick an epoch change rejected gets a new one. ``now`` is honoured on
    SQLite only; PostgreSQL reads its own clock.
    """
    with platform_session() as session:
        if is_postgres(session):
            return int(session.execute(
                text(f"SELECT {DISPATCH_FUNCTION}(CAST(:dispatch_limit AS integer))"),
                {"dispatch_limit": limit},
            ).scalar_one())
        slots = _due_slots(session, limit=limit, now=now or utcnow())
    dispatched = 0
    for slot in slots:
        context = _scheduler_context(slot.workspace_id, slot.authorization_epoch, slot.meta_mode)
        with tenant_session(context) as session:
            _job, created = enqueue_tick(session, context, connection_id=slot.connection_id, fire_at=slot.fire_at)
        dispatched += int(created)
    return dispatched


def _due_slots(session: Session, *, limit: int, now: datetime) -> list[_Slot]:
    """SQLite's view of what the dispatch function selects."""
    schedules = dispatch_schedules(session, limit, now=now)
    workspace_ids = {schedule.workspace_id for schedule in schedules}
    workspaces = {
        row.id: row for row in session.scalars(
            select(WorkspaceRecord).where(WorkspaceRecord.id.in_(workspace_ids))
        )
    } if workspace_ids else {}
    slots = {}
    for schedule in schedules:
        workspace = workspaces.get(schedule.workspace_id)
        if workspace is None or workspace.suspended or workspace.lifecycle != "active":
            continue
        fire_at = _as_utc(schedule.next_fire_at)
        key = (schedule.workspace_id, schedule.meta_mode, schedule.meta_connection_id, fire_at)
        slots[key] = _Slot(
            workspace_id=schedule.workspace_id,
            authorization_epoch=int(workspace.authorization_epoch),
            meta_mode=schedule.meta_mode,
            connection_id=schedule.meta_connection_id,
            fire_at=fire_at,
        )
    return [slots[key] for key in sorted(slots, key=lambda k: (k[3], k[2]))]


def enqueue_tick(
    session: Session, context: TenantContext, *, connection_id: str, fire_at: datetime,
) -> tuple[JobRecord, bool]:
    """The tick Job for ``(connection, fire_at)`` under the context's epoch, and
    whether this call created it.

    Same type, payload and key as the dispatch function writes, so the two
    paths name one Job.
    """
    key = tick_idempotency_key(connection_id, fire_at, authorization_epoch=context.authorization_epoch)
    existed = session.scalar(
        select(JobRecord.id).where(
            JobRecord.workspace_id == context.workspace_id, JobRecord.idempotency_key == key,
        )
    ) is not None
    job = enqueue_in_session(
        session,
        context=context,
        job_type=TICK_JOB_TYPE,
        payload={
            "connection_id": connection_id,
            "meta_mode": context.meta_mode,
            "fire_at": fire_at_text(fire_at),
        },
        idempotency_key=key,
        resource_type="meta_connection",
        resource_id=connection_id,
    )
    return job, not existed


# ---------------------------------------------------------------------------
# 2. Tick
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def run_observation_tick(
    session: Session,
    context: TenantContext,
    *,
    connection_id: str,
    fire_at: datetime,
    now: datetime | None = None,
) -> TickResult:
    """Turn one due firing of a connection's schedules into sync runs and a batch Job.

    Runs in the caller's transaction (``session`` is bound to ``context``).
    A replay -- the schedules already moved past ``fire_at`` -- changes
    nothing.
    """
    now = _as_utc(now or utcnow())
    fire_at = _as_utc(fire_at)
    schedule = MetaObservationSyncScheduleRecord
    schedules = list(session.scalars(
        select(schedule)
        .where(
            schedule.workspace_id == context.workspace_id,
            schedule.meta_mode == context.meta_mode,
            schedule.meta_connection_id == connection_id,
            schedule.state == "active",
            schedule.next_fire_at == fire_at,
        )
        .order_by(schedule.contract_generation_id, schedule.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    if not schedules or fire_at > now:
        return TickResult("not_due")
    connection = session.scalar(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == context.meta_mode,
            MetaConnectionRecord.id == connection_id,
        )
    )
    if connection is None or connection.state != "active":
        return TickResult("connection_inactive")
    generations = {
        row.id: row for row in session.scalars(
            select(ObservationContractGenerationRecord).where(
                ObservationContractGenerationRecord.workspace_id == context.workspace_id,
                ObservationContractGenerationRecord.meta_mode == context.meta_mode,
                ObservationContractGenerationRecord.id.in_({s.contract_generation_id for s in schedules}),
            )
        )
    }

    due: list[tuple[MetaObservationSyncScheduleRecord, ObservationContractGenerationRecord]] = []
    skipped_to: dict[datetime, list[str]] = defaultdict(list)
    for row in schedules:
        generation = generations.get(row.contract_generation_id)
        if generation is None or generation.state != "active":
            row.state = "retired"  # its generation no longer collects
            continue
        latest = latest_fire_at_or_before(row.cron_expression, generation.local_timezone, now)
        if latest > fire_at:
            # The Worker missed later slots. Older days are inside the next
            # run's 14-day lookback, so only the latest slot runs (#101 Q9).
            row.next_fire_at = latest
            skipped_to[latest].append(row.id)
            continue
        due.append((row, generation))

    next_ticks = [
        enqueue_tick(session, context, connection_id=connection_id, fire_at=latest)[0].id
        for latest in sorted(skipped_to)
    ]

    runs_by_date: dict[date, list[str]] = defaultdict(list)
    for row, generation in due:
        row.last_fired_at = fire_at
        row.next_fire_at = next_fire_after(row.cron_expression, generation.local_timezone, fire_at)
        plan = _plan_or_audit(session, generation)
        if plan is None:
            continue
        target_date = observation_target_date(fire_at, generation.local_timezone)
        for instance in plan.instances:
            run = _ensure_run(session, context, generation, target_date, instance.name)
            runs_by_date[target_date].append(run.id)

    sync_jobs = [
        enqueue_in_session(
            session,
            context=context,
            job_type=SYNC_JOB_TYPE,
            payload={
                "connection_id": connection_id,
                "meta_mode": context.meta_mode,
                "target_date": target_date.isoformat(),
                "run_generation": RUN_GENERATION,
            },
            idempotency_key=sync_idempotency_key(connection_id, target_date, RUN_GENERATION),
            resource_type="meta_connection",
            resource_id=connection_id,
        ).id
        for target_date in sorted(runs_by_date)
    ]
    session.flush()
    if not due:
        status = "fast_forwarded" if next_ticks else "generation_inactive"
    else:
        status = "dispatched"
    return TickResult(
        status=status,
        target_dates=tuple(sorted(runs_by_date)),
        run_ids=tuple(run_id for day in sorted(runs_by_date) for run_id in runs_by_date[day]),
        sync_job_ids=tuple(sync_jobs),
        next_tick_job_ids=tuple(next_ticks),
    )


def _plan_or_audit(session: Session, generation: ObservationContractGenerationRecord) -> ReadPlan | None:
    """The generation's read plan; a drifted Scale contract is audited, not read."""
    try:
        return observation_read_plan(generation)
    except ScaleReadContractError as exc:
        append_event(
            session,
            event_type="meta.observation.contract_mismatch",
            resource_type="observation_contract_generation",
            resource_id=generation.id,
            details={"code": exc.code, "detail": str(exc)[:500]},
        )
        return None


def _ensure_run(
    session: Session,
    context: TenantContext,
    generation: ObservationContractGenerationRecord,
    target_date: date,
    instance: str,
) -> MetaObservationSyncRunRecord:
    """The run for ``(generation, D, instance)`` and its Operation, created once."""
    occurrence = occurrence_id(generation.id, target_date, instance)
    existing = session.scalar(
        select(MetaObservationSyncRunRecord).where(
            MetaObservationSyncRunRecord.workspace_id == context.workspace_id,
            MetaObservationSyncRunRecord.meta_mode == context.meta_mode,
            MetaObservationSyncRunRecord.occurrence_id == occurrence,
            MetaObservationSyncRunRecord.run_generation == RUN_GENERATION,
        )
    )
    if existing is not None:
        return existing
    semantic_key = hashlib.sha256(
        f"{SYNC_COMMAND_TYPE}:{occurrence}:{RUN_GENERATION}".encode("utf-8")
    ).hexdigest()
    operation = session.scalar(
        select(OperationRecord).where(
            OperationRecord.workspace_id == context.workspace_id,
            OperationRecord.semantic_key_hash == semantic_key,
        )
    ) or create_operation(
        session,
        command_type=SYNC_COMMAND_TYPE,
        actor=SCHEDULER_EXECUTOR,
        context=context,
        semantic_key_hash=semantic_key,
        reason="scheduled Meta observation sync",
    )
    return create_sync_run(
        session,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        contract_generation_id=generation.id,
        occurrence_id=occurrence,
        operation_id=operation.id,
        executor=SCHEDULER_EXECUTOR,
        run_generation=RUN_GENERATION,
    )


# ---------------------------------------------------------------------------
# 3. Batch
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def run_observation_sync(
    context: TenantContext,
    *,
    connection_id: str,
    target_date: date,
    run_generation: int = RUN_GENERATION,
    final_attempt: bool,
    heartbeat: Callable[[], bool] | None = None,
) -> BatchResult:
    """Drive every run of ``(connection, D)`` to a terminal state, in order.

    A run that fails does not stop the others; the Job is retried for the
    ones that did not finish, which resume from their last committed page.
    On the final attempt those runs are marked ``failed`` before
    :class:`ObservationBatchFailed` is raised.
    """
    with tenant_session(context) as session:
        run_ids = _batch_run_ids(session, context, connection_id, target_date, run_generation)
    failures: dict[str, str] = {}
    succeeded: list[str] = []
    for run_id in run_ids:
        try:
            if _execute_run(context, run_id, heartbeat=heartbeat) == "succeeded":
                succeeded.append(run_id)
        except ObservationLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 -- one run must not stop the batch
            failures[run_id] = f"{type(exc).__name__}: {exc}"[:500]
    if failures:
        if final_attempt:
            for run_id, reason in failures.items():
                _fail_run(context, run_id, reason)
        raise ObservationBatchFailed(failures, final=final_attempt)
    return BatchResult(run_ids=tuple(run_ids), succeeded=tuple(succeeded))


def _batch_run_ids(
    session: Session, context: TenantContext, connection_id: str, target_date: date, run_generation: int,
) -> list[str]:
    generations = session.scalars(
        select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == context.workspace_id,
            ObservationContractGenerationRecord.meta_mode == context.meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection_id,
        )
    ).all()
    occurrences = []
    for generation in generations:
        try:
            plan = observation_read_plan(generation)
        except ScaleReadContractError:
            plan = None  # the tick created no run for it
        if plan is not None:
            occurrences.extend(_plan_occurrences(generation.id, target_date, plan))
    if not occurrences:
        return []
    return list(session.scalars(
        select(MetaObservationSyncRunRecord.id)
        .where(
            MetaObservationSyncRunRecord.workspace_id == context.workspace_id,
            MetaObservationSyncRunRecord.meta_mode == context.meta_mode,
            MetaObservationSyncRunRecord.occurrence_id.in_(occurrences),
            MetaObservationSyncRunRecord.run_generation == run_generation,
        )
        .order_by(MetaObservationSyncRunRecord.contract_generation_id, MetaObservationSyncRunRecord.occurrence_id,
                  MetaObservationSyncRunRecord.id)
    ))


def _execute_run(context: TenantContext, run_id: str, *, heartbeat: Callable[[], bool] | None) -> str:
    """Page until the run is terminal; return its terminal state."""
    with tenant_session(context) as session:
        while True:
            state = _sync_next_page(session, context, run_id)
            if state in TERMINAL_RUN_STATES:
                return state
            if heartbeat is not None and not heartbeat():
                raise ObservationLeaseLost(f"lease lost while syncing run {run_id}")


def _lock_run(session: Session, run_id: str) -> MetaObservationSyncRunRecord:
    run = session.scalar(
        select(MetaObservationSyncRunRecord)
        .where(MetaObservationSyncRunRecord.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise ObservationSyncError("meta_sync_run_not_found", f"no sync run {run_id!r}")
    return run


@dataclass(frozen=True)
class _PageRead:
    """One page to fetch: where the intent says it goes, and how to read it."""

    endpoint: str
    request: dict[str, Any]
    edge: str | None = None
    adapter_cursor: str | None = None


def _contract_identity(plan: ReadPlan, instance: QueryInstance) -> dict[str, Any]:
    identity: dict[str, Any] = {"instance": instance.name}
    if plan.contract is not None:
        identity.update({
            "scale_read_contract": plan.contract.contract_type,
            "version": plan.contract.version,
            "definition_hash": plan.contract.contract_hash,
        })
    return identity


def _edge_cursor(cursor: str | None) -> tuple[str, str | None]:
    """The configuration checkpoint: which edge is next, and where in it."""
    if cursor is None:
        return CONFIGURATION_EDGES[0][0], None
    try:
        state = json.loads(cursor)
        edge, after = state["edge"], state.get("after")
    except (ValueError, TypeError, KeyError):
        raise ObservationSyncError("meta_sync_checkpoint_invalid", f"unreadable configuration cursor {cursor!r}") \
            from None
    if edge not in dict(CONFIGURATION_EDGES) or (after is not None and not isinstance(after, str)):
        raise ObservationSyncError("meta_sync_checkpoint_invalid", f"unreadable configuration cursor {cursor!r}")
    return edge, after


def _encode_edge_cursor(edge: str, after: str | None) -> str:
    return json.dumps({"edge": edge, "after": after}, sort_keys=True, separators=(",", ":"))


def _page_read(
    generation: ObservationContractGenerationRecord,
    plan: ReadPlan,
    instance: QueryInstance,
    target_date: date,
    cursor: str | None,
) -> _PageRead:
    """The persisted request identity of one page (``request_parameters``)."""
    request: dict[str, Any] = {"contract": _contract_identity(plan, instance), "limit": PAGE_LIMIT}
    if instance.read == CONFIGURATION_READ:
        edge, after = _edge_cursor(cursor)
        request.update({"edge": edge, "fields": list(dict(CONFIGURATION_EDGES)[edge]),
                        "as_of": target_date.isoformat()})
        if after is not None:
            request["after"] = after
        endpoint = f"/{generation.ad_account_id}" if edge == "account" else f"/{generation.ad_account_id}/{edge}"
        return _PageRead(endpoint, request, edge=edge, adapter_cursor=after)
    since, until = instance.time_range(target_date)
    request.update({
        "level": "ad",
        "fields": list(plan.fields),
        "time_range": {"since": since.isoformat(), "until": until.isoformat()},
        "time_increment": instance.time_increment,
        "attribution": plan.attribution,
        "breakdowns": list(plan.breakdowns),
    })
    if cursor is not None:
        request["after"] = cursor
    return _PageRead(f"/{generation.ad_account_id}/insights", request, adapter_cursor=cursor)


def _read_page(port: MetaObservationReadPort, generation: ObservationContractGenerationRecord, plan: ReadPlan,
               instance: QueryInstance, target_date: date, read: _PageRead) -> tuple[Any, str | None]:
    """Fetch the page; return it and the run's checkpoint after it (``None``: last page)."""
    if read.edge is not None:
        page = port.read_delivery_configuration(
            ad_account_id=generation.ad_account_id, edge=read.edge, fields=read.request["fields"],
            as_of=target_date, limit=PAGE_LIMIT, after=read.adapter_cursor,
        )
        if page.next_cursor is not None:
            return page, _encode_edge_cursor(read.edge, page.next_cursor)
        edges = [edge for edge, _fields in CONFIGURATION_EDGES]
        position = edges.index(read.edge)
        return page, (_encode_edge_cursor(edges[position + 1], None) if position + 1 < len(edges) else None)
    since, until = instance.time_range(target_date)
    page = port.read_insights(
        ad_account_id=generation.ad_account_id,
        fields=list(plan.fields),
        since=since,
        until=until,
        limit=PAGE_LIMIT,
        after=read.adapter_cursor,
        time_increment=instance.time_increment or "1",
        breakdowns=list(plan.breakdowns),
        action_attribution_windows=list(plan.attribution.get("action_attribution_windows") or []),
        action_report_time=plan.attribution.get("action_report_time"),
    )
    return page, page.next_cursor


def _watermark(instance: QueryInstance, target_date: date) -> str:
    """The days an instance's attestation vouches for: its window, or D."""
    if instance.read == CONFIGURATION_READ:
        return f"[{target_date.isoformat()},{target_date.isoformat()}]"
    since, until = instance.time_range(target_date)
    return f"[{since.isoformat()},{until.isoformat()}]"


def _sync_next_page(session: Session, context: TenantContext, run_id: str) -> str:
    """Fetch and record the run's next page; return the run's state afterwards."""
    run = _lock_run(session, run_id)
    if run.state in TERMINAL_RUN_STATES:
        return run.state
    generation = session.get(ObservationContractGenerationRecord, run.contract_generation_id)
    target_date, instance_name = _occurrence_of(run)
    try:
        plan = observation_read_plan(generation)
    except ScaleReadContractError as exc:
        raise ObservationSyncError(exc.code, str(exc)) from exc
    if plan is None:
        raise ObservationSyncError(
            "meta_schedule_contract_unsupported", f"generation {generation.id} has no read plan",
        )
    instance = plan.instance(instance_name)
    page_order = (run.last_completed_page_order or 0) + 1
    input_cursor = run.output_cursor if page_order > 1 else None
    if page_order > 1 and input_cursor is None:
        raise ObservationSyncError(
            "meta_sync_checkpoint_invalid", f"run {run.id} has page {page_order - 1} without a cursor",
        )
    connection = session.scalar(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == context.meta_mode,
            MetaConnectionRecord.id == generation.meta_connection_id,
        )
    )
    resolved = resolve_adapter(
        session,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        connection_id=generation.meta_connection_id,
        ad_account_id=generation.ad_account_id,
    )
    read = _page_read(generation, plan, instance, target_date, input_cursor)

    # The intent and a `started` attempt commit before the read (§4.2,
    # §6.3): a Worker that dies mid-page leaves them as the evidence of it,
    # and its retry opens the next attempt for the same page.
    intent = create_call_intent(
        session,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        connection=connection,
        binding=resolved.binding,
        operation_id=run.operation_id,
        meta_api_method="GET",
        meta_api_endpoint=read.endpoint,
        request_parameters=read.request,
        call_type="page_fetch",
        scenario_version_id=resolved.scenario_version_id,
        executor=READ_EXECUTOR,
    )
    attempt_number = (session.scalar(
        select(func.max(MetaPageFetchAttemptRecord.attempt_number)).where(
            MetaPageFetchAttemptRecord.sync_run_id == run.id,
            MetaPageFetchAttemptRecord.page_order == page_order,
        )
    ) or 0) + 1
    attempt = MetaPageFetchAttemptRecord(
        id=new_id("mpfa"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sync_run_id=run.id,
        intent_id=intent.id,
        page_order=page_order,
        attempt_number=attempt_number,
        state="started",
        input_cursor=input_cursor,
    )
    session.add(attempt)
    if run.state == "queued":
        run.state = "running"
        _move_operation(session, run.operation_id, OperationState.RUNNING, "scheduled sync started")
    # The sandbox adapter reads this database: no network dispatch to mark.
    network = not getattr(resolved.adapter, "is_test_adapter", False)
    if network:
        intent.network_dispatched_at = utcnow()
    session.flush()
    intent_id, attempt_id, operation_id = intent.id, attempt.id, run.operation_id
    commit_and_rebind(session)

    run = _lock_run(session, run_id)
    if run.state != "running" or (run.last_completed_page_order or 0) != page_order - 1:
        # Another claim of this Job moved the run on meanwhile.
        _close_attempt(session, attempt_id, "failed", "superseded by another attempt")
        commit_and_rebind(session)
        return run.state
    port: MetaObservationReadPort = resolved.adapter
    try:
        page, output_cursor = _read_page(port, generation, plan, instance, target_date, read)
    except Exception as exc:
        # Nothing of this page is kept but the evidence that the read failed.
        session.rollback()
        session.begin()
        apply_to_session(session, context)
        session.add(MetaCallOutcomeRecord(
            id=new_id("mco"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            intent_id=intent_id,
            operation_id=operation_id,
            network_attempted=network,
            error_category="adapter_read_failed",
            error_detail=f"{type(exc).__name__}: {exc}"[:2000],
            executor=READ_EXECUTOR,
        ))
        _close_attempt(session, attempt_id, "failed", f"{type(exc).__name__}: {exc}")
        commit_and_rebind(session)
        raise

    raw = json.dumps(page.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    outcome = MetaCallOutcomeRecord(
        id=new_id("mco"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        intent_id=intent_id,
        operation_id=operation_id,
        network_attempted=network,
        http_status=200 if network else None,
        response_evidence_hash=hashlib.sha256(raw).hexdigest(),
        response_summary={"row_count": page.row_count, "has_next_page": page.next_cursor is not None},
        executor=READ_EXECUTOR,
    )
    session.add(outcome)
    session.flush()
    is_terminal = output_cursor is None
    record_page(
        session,
        sync_run_id=run.id,
        page_order=page_order,
        payload_checksum=hashlib.sha256(compressed).hexdigest(),
        payload_compressed=compressed,
        row_count=page.row_count,
        intent_id=intent_id,
        outcome_id=outcome.id,
        input_cursor=input_cursor,
        output_cursor=output_cursor,
        is_terminal=is_terminal,
    )
    _close_attempt(session, attempt_id, "succeeded", None)
    if is_terminal:
        total_rows = session.scalar(
            select(func.coalesce(func.sum(MetaEvidencePageRecord.row_count), 0)).where(
                MetaEvidencePageRecord.sync_run_id == run.id,
            )
        )
        complete_sync_run(session, sync_run_id=run.id, total_pages=page_order, total_rows=int(total_rows))
        _publish(session, run, generation, plan, instance, target_date)
        _move_operation(
            session, run.operation_id, OperationState.SUCCEEDED, "scheduled sync published",
            result_ref=OperationResultRef("meta_sync_run", run.id),
        )
    session.flush()
    commit_and_rebind(session)
    return "succeeded" if is_terminal else "running"


def _close_attempt(session: Session, attempt_id: str, state: str, detail: str | None) -> None:
    attempt = session.get(MetaPageFetchAttemptRecord, attempt_id)
    attempt.state = state
    attempt.error_detail = detail[:2000] if detail else None
    attempt.completed_at = utcnow()
    session.flush()


def _publish(
    session: Session,
    run: MetaObservationSyncRunRecord,
    generation: ObservationContractGenerationRecord,
    plan: ReadPlan,
    instance: QueryInstance,
    target_date: date,
) -> None:
    """Publication and attestation of a run that just succeeded, and coverage of D.

    Each step finds what an earlier pass of the same transaction boundary
    wrote, so each exists once per run. The generation covers D once all its
    query instances for D succeeded: the run that completes the set writes
    the ledger entry.
    """
    publication = publish_evidence(session, sync_run_id=run.id, operation_id=run.operation_id)
    attestation = session.scalar(
        select(EvidenceAttestationRecord)
        .where(EvidenceAttestationRecord.publication_id == publication.id)
        .order_by(EvidenceAttestationRecord.created_at, EvidenceAttestationRecord.id)
        .limit(1)
    )
    watermark = _watermark(instance, target_date)
    if attestation is None:
        attestation = create_attestation(
            session,
            contract_generation_id=generation.id,
            publication_id=publication.id,
            evidence_watermark=watermark,
        )
    covered = session.scalar(
        select(ObservationCoverageLedgerRecord.id).where(
            ObservationCoverageLedgerRecord.contract_generation_id == generation.id,
            ObservationCoverageLedgerRecord.coverage_date == target_date,
        )
    )
    if covered is None and _all_instances_succeeded(session, run, generation, plan, target_date):
        record_coverage(
            session,
            generation_id=generation.id,
            coverage_date=target_date,
            sync_run_id=run.id,
            attestation_id=attestation.id,
        )
    append_event(
        session,
        event_type="evidence.published",
        resource_type="meta_sync_run",
        resource_id=run.id,
        details={
            "contract_generation_id": generation.id,
            "publication_id": publication.id,
            "attestation_id": attestation.id,
            "evidence_watermark": attestation.evidence_watermark,
            "coverage_date": target_date.isoformat(),
            "query_instance": instance.name,
            "total_pages": run.total_pages,
            "total_rows": run.total_rows,
        },
    )


def _all_instances_succeeded(
    session: Session,
    run: MetaObservationSyncRunRecord,
    generation: ObservationContractGenerationRecord,
    plan: ReadPlan,
    target_date: date,
) -> bool:
    if len(plan.instances) == 1:
        return True
    occurrences = [occurrence_id(generation.id, target_date, instance.name) for instance in plan.instances]
    succeeded = set(session.scalars(
        select(MetaObservationSyncRunRecord.occurrence_id).where(
            MetaObservationSyncRunRecord.workspace_id == run.workspace_id,
            MetaObservationSyncRunRecord.meta_mode == run.meta_mode,
            MetaObservationSyncRunRecord.occurrence_id.in_(occurrences),
            MetaObservationSyncRunRecord.run_generation == run.run_generation,
            MetaObservationSyncRunRecord.state == "succeeded",
        )
    ))
    return succeeded == set(occurrences)


def _move_operation(
    session: Session,
    operation_id: str,
    to_state: OperationState,
    reason: str,
    *,
    result_ref: OperationResultRef | None = None,
) -> None:
    """Advance the run's Operation, under its row lock, if it is not there yet."""
    operation = session.scalar(
        select(OperationRecord)
        .where(OperationRecord.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if operation is None or operation.state == to_state.value:
        return
    if to_state is OperationState.SUCCEEDED and operation.state == OperationState.QUEUED.value:
        transition_operation(session, operation_id, OperationState.RUNNING, reason, READ_EXECUTOR)
    if operation.state in (OperationState.QUEUED.value, OperationState.RUNNING.value):
        transition_operation(session, operation_id, to_state, reason, READ_EXECUTOR, result_ref=result_ref)


def _fail_run(context: TenantContext, run_id: str, reason: str) -> None:
    """The batch Job's last attempt gave up on this run (#107)."""
    with tenant_session(context) as session:
        run = fail_sync_run(session, sync_run_id=run_id)
        if run.state != "failed":
            return
        _move_operation(session, run.operation_id, OperationState.FAILED, f"scheduled sync failed: {reason}"[:500])
        append_event(
            session,
            event_type="meta.sync_run.failed",
            resource_type="meta_sync_run",
            resource_id=run.id,
            details={"contract_generation_id": run.contract_generation_id, "reason": reason[:500]},
        )
