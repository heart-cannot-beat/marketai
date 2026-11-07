"""Persistent job queue with leases and explicit crash ambiguity.

The queue is deliberately database-backed.  A paid non-idempotent operation
that dies after entering ``running`` becomes ``uncertain`` instead of being
silently retried and potentially charged twice.
"""
from __future__ import annotations

import json

import hashlib
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, text, update

from .. import config
from ..adapters.persistence.database import engine, platform_session, tenant_session
from ..adapters.persistence.models import AssetRecord, JobRecord, WorkspaceRecord, utcnow
from .audit import append_event
from .job_policy import resolve_safe_to_retry_after_crash
from .operations import (
    AGGREGATED_ADMISSION_CONTRACT_VERSION,
    NON_TERMINAL_STATES,
    OperationRecord,
    OperationState,
    aggregate_operation_from_jobs,
    get_operation,
    start_operation_for_job,
    transition_operation,
)
from .tenancy import TenantContext, current_context, system_context, tenant_scope


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    type: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_owner: str
    context: TenantContext


def _frozen_system_context(job: JobRecord) -> TenantContext:
    initiator_role = (
        job.initiator_role if job.initiator_role in ("owner", "member") else None
    )
    return system_context(
        job.workspace_id,
        initiator_id=job.initiator_id,
        initiator_role=initiator_role,
        request_id=job.request_id,
        trace_id=job.trace_id,
        authorization_epoch=job.authorization_epoch,
    )


def _audit_dispatch_transition(
    session, job: JobRecord, event_type: str, details: dict[str, Any]
) -> None:
    with tenant_scope(session, _frozen_system_context(job)):
        append_event(
            session,
            event_type=event_type,
            resource_type="job",
            resource_id=job.id,
            details=details,
        )


def _aggregate_operation(
    session, context: TenantContext, operation_id: str | None, *, reason: str
) -> None:
    """Fold the Operation this Job belongs to, if it now has an outcome.

    Every Job terminal transition routes through here so the user-facing
    Operation cannot be left ``running`` forever by whichever sibling
    happened to finish last -- including the ones that finish inside
    recovery sweeps rather than a Worker fence. Operations below the
    aggregated admission contract version are ignored, which is what keeps
    the legacy ``queued`` Operations owned by their own domain handlers.
    """
    if not operation_id:
        return
    aggregate_operation_from_jobs(
        session,
        operation_id,
        actor=context.executor or "system:worker",
        context=context,
        reason=reason,
    )


def _aggregate_operation_for_job(
    session, job: JobRecord, *, reason: str
) -> None:
    """``_aggregate_operation`` for the recovery sweeps, which run on a
    platform session with no bound tenant of their own."""
    if not job.operation_id:
        return
    context = _frozen_system_context(job)
    with tenant_scope(session, context):
        _aggregate_operation(session, context, job.operation_id, reason=reason)


def enqueue_in_session(
    session,
    *,
    context: TenantContext,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    resource_type: str = "",
    resource_id: str = "",
    safe_to_retry_after_crash: bool | None = None,
    max_attempts: int = 3,
) -> JobRecord:
    """Persist one Job, or return the one ``idempotency_key`` already named.

    ``safe_to_retry_after_crash`` comes from ``app.platform.job_policy``:
    leave it ``None``, or pass the registered value. A type the registry
    does not list is refused before anything is written.
    """
    safe_to_retry_after_crash = resolve_safe_to_retry_after_crash(
        job_type, safe_to_retry_after_crash
    )
    bound = current_context(session)
    if bound != context:
        raise ValueError("the job context must match the transaction's TenantContext")
    existing = session.scalar(
        select(JobRecord).where(
            JobRecord.workspace_id == context.workspace_id,
            JobRecord.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.type != job_type or existing.payload_json != payload:
            raise ValueError("相同 idempotency key 对应了不同任务")
        return existing
    job = JobRecord(
        id=f"job_{uuid.uuid4().hex}",
        workspace_id=context.workspace_id,
        initiator_id=context.initiator_id,
        initiator_role=context.audit_initiator_role,
        request_id=context.request_id,
        trace_id=context.trace_id,
        authorization_epoch=context.authorization_epoch,
        type=job_type,
        resource_type=resource_type,
        resource_id=resource_id,
        payload_json=payload,
        idempotency_key=idempotency_key,
        state="queued",
        safe_to_retry_after_crash=safe_to_retry_after_crash,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    append_event(
        session,
        event_type="job.enqueued",
        resource_type="job",
        resource_id=job.id,
        details={
            "job_type": job_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "idempotency_key_sha256": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest(),
        },
    )
    return job


def _normalize_sqlite_datetime(value: datetime) -> datetime:
    """SQLite may return naive values even for timezone-aware columns."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def recover_expired_jobs(now: datetime | None = None) -> dict[str, int]:
    from .ops_alerts import AlertCategory, Severity, emit as ops_emit

    now = now or utcnow()
    # Authorization is checked before looking for paid-call side effects. A
    # completed asset is not authority to resurrect work admitted under an
    # epoch the control plane has since revoked.
    _reject_stale_sqlite_jobs(now)
    recovered = 0
    uncertain = 0
    with platform_session() as session:
        rows = session.scalars(
            select(JobRecord).where(
                JobRecord.state.in_(("leased", "running")),
                JobRecord.lease_expires_at.is_not(None),
                JobRecord.lease_expires_at < now,
            )
        ).all()
        for row in rows:
            asset_id = (row.payload_json or {}).get("asset_id")
            asset = session.get(AssetRecord, asset_id) if asset_id else None
            if (
                row.type == "generate_asset"
                and asset is not None
                and asset.workspace_id == row.workspace_id
                and asset.status == "ok"
            ):
                row.state = "succeeded"
                row.finished_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                recovered += 1
                _audit_dispatch_transition(
                    session, row, "job.recovered_succeeded", {}
                )
                _aggregate_operation_for_job(
                    session, row, reason="sibling Job recovered as succeeded"
                )
                continue
            if row.state == "leased" or row.safe_to_retry_after_crash:
                row.state = "queued"
                row.error_code = "lease_expired"
                row.error_detail = "Worker 租约过期，任务已安全回队"
                row.lease_owner = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                recovered += 1
                _audit_dispatch_transition(
                    session,
                    row,
                    "job.requeued",
                    {"error_code": row.error_code},
                )
            else:
                row.state = "uncertain"
                row.error_code = "paid_call_outcome_unknown"
                row.error_detail = "付费外部调用结果不确定；为避免重复扣费，禁止自动重试"
                row.lease_owner = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                uncertain += 1
                if row.type == "launch_provisioning":
                    operation_id = str((row.payload_json or {}).get("operation_id") or "")
                    meta_mode = str((row.payload_json or {}).get("meta_mode") or "")
                    if operation_id and meta_mode:
                        launch_context = replace(
                            _frozen_system_context(row), meta_mode=meta_mode,
                        )
                        with tenant_scope(session, launch_context):
                            operation = get_operation(session, operation_id)
                            if operation.state in {
                                OperationState.QUEUED.value,
                                OperationState.RUNNING.value,
                            }:
                                # ``jobs.mark_running`` and the Operation transition
                                # are deliberately separate durable transactions.  A
                                # crash in that narrow window leaves a running Job and
                                # a queued Operation; both are nevertheless ambiguous.
                                # Mirror the PostgreSQL dispatcher migration and move
                                # the projection plus its history together here.
                                if operation.state == OperationState.QUEUED.value:
                                    transition_operation(
                                        session,
                                        operation_id,
                                        OperationState.RUNNING,
                                        reason=(
                                            "launch dispatch observed during expired "
                                            "worker recovery"
                                        ),
                                        actor=launch_context.executor,
                                    )
                                transition_operation(
                                    session,
                                    operation_id,
                                    OperationState.UNCERTAIN,
                                    reason="launch worker lease expired after dispatch",
                                    actor=launch_context.executor,
                                )
                _audit_dispatch_transition(
                    session,
                    row,
                    "job.uncertain",
                    {"error_code": row.error_code},
                )
                _aggregate_operation_for_job(
                    session,
                    row,
                    reason="sibling Job left uncertain by lease expiry",
                )
    if recovered or uncertain:
        ops_emit(
            Severity.WARNING if uncertain else Severity.INFO,
            AlertCategory.RECONCILIATION,
            f"Lease recovery: {recovered} requeued, {uncertain} uncertain",
            details={"recovered": recovered, "uncertain": uncertain},
        )
    return {"recovered": recovered, "uncertain": uncertain}


def reconcile_admitted_operations(limit: int = 100) -> int:
    """Fold Operations whose Jobs all went terminal without a Python caller.

    On PostgreSQL the dispatcher's epoch rejection, lease requeue and
    ``uncertain`` release happen inside ``marketai_claim_next_job_v2`` -- one
    SQL statement per class of Job, with no Python transition to hang the
    per-Job aggregation off. Those Operations would otherwise sit
    ``admitted``/``running`` with every Job finished. This sweep is the
    reconciler for exactly that gap: it is idempotent, bounded, and folds
    only Operations that already have their complete terminal Job set.

    Returns the number of Operations this pass moved.
    """
    moved = 0
    with platform_session() as session:
        candidates = session.scalars(
            select(OperationRecord)
            .where(
                OperationRecord.admission_contract_version
                >= AGGREGATED_ADMISSION_CONTRACT_VERSION,
                OperationRecord.state.in_(
                    tuple(state.value for state in NON_TERMINAL_STATES)
                ),
            )
            .order_by(OperationRecord.updated_at)
            .limit(limit)
        ).all()
        for operation in candidates:
            context = system_context(
                operation.workspace_id, initiator_id=operation.initiator_id
            )
            with tenant_scope(session, context):
                changed = aggregate_operation_from_jobs(
                    session,
                    operation.id,
                    actor=context.executor,
                    context=context,
                    reason="reconciled from terminal sibling Jobs",
                )
            if changed is not None:
                moved += 1
    if moved:
        from .ops_alerts import AlertCategory, Severity, emit as ops_emit

        ops_emit(
            Severity.INFO,
            AlertCategory.RECONCILIATION,
            f"Operation reconciliation: folded {moved} operations",
            details={"folded": moved},
        )
    return moved


def _reject_stale_sqlite_jobs(now: datetime) -> int:
    """Mirror the PostgreSQL dispatcher's epoch gate for local SQLite runs."""
    rejected = 0
    with platform_session() as session:
        candidates = session.scalars(
            select(JobRecord).where(
                JobRecord.state.in_(("queued", "retry_wait", "leased", "running"))
            )
        ).all()
        for job in candidates:
            if job.state in ("leased", "running") and (
                job.lease_expires_at is None
                or _normalize_sqlite_datetime(job.lease_expires_at) >= now
            ):
                continue
            workspace = session.get(WorkspaceRecord, job.workspace_id)
            if workspace is not None and workspace.authorization_epoch == job.authorization_epoch:
                continue
            job.state = "failed"
            job.finished_at = now
            job.error_code = "authorization_epoch_stale"
            job.error_detail = "The authorization epoch changed after this job was enqueued"
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            rejected += 1
            _audit_dispatch_transition(
                session,
                job,
                "job.authorization_rejected",
                {"error_code": job.error_code},
            )
            _aggregate_operation_for_job(
                session,
                job,
                reason="sibling Job rejected by a stale authorization epoch",
            )
    return rejected


def claim_next(owner: str, lease_seconds: int | None = None, *,
               job_types: tuple[str, ...] | None = None, exclude_types: tuple[str, ...] = ()) -> ClaimedJob | None:
    now = utcnow()
    lease_seconds = lease_seconds or config.JOB_LEASE_SECONDS
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('marketai.job_types', :types, true), "
                "set_config('marketai.excluded_job_types', :excluded, true)"),
                {'types': json.dumps(job_types), 'excluded': json.dumps(exclude_types)})
            row = connection.execute(
                text("SELECT * FROM marketai_claim_next_job_v2(:owner, :lease_seconds)"),
                {"owner": owner, "lease_seconds": lease_seconds},
            ).mappings().first()
        if row is None:
            return None
        return ClaimedJob(
            id=str(row["job_id"]),
            type=str(row["job_type"]),
            payload=dict(row["payload_json"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            lease_owner=owner,
            context=system_context(
                str(row["workspace_id"]),
                initiator_id=row["initiator_id"],
                initiator_role=(
                    row["initiator_role"]
                    if row["initiator_role"] in ("owner", "member")
                    else None
                ),
                request_id=str(row["request_id"] or ""),
                trace_id=str(row["trace_id"] or ""),
                authorization_epoch=int(row["authorization_epoch"]),
            ),
        )

    recover_expired_jobs(now)
    with platform_session() as session:
        candidate = session.scalar(
            select(JobRecord)
            .where(
                JobRecord.state.in_(("queued", "retry_wait")),
                *([JobRecord.type.in_(job_types)] if job_types is not None else []),
                *([JobRecord.type.not_in(exclude_types)] if exclude_types else []),
                or_(JobRecord.next_attempt_at.is_(None), JobRecord.next_attempt_at <= now),
            )
            .order_by(JobRecord.created_at, JobRecord.id)
            .limit(1)
        )
        if not candidate:
            return None
        result = session.execute(
            update(JobRecord)
            .where(
                JobRecord.id == candidate.id,
                JobRecord.state.in_(("queued", "retry_wait")),
            )
            .values(
                state="leased",
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                attempt=JobRecord.attempt + 1,
                started_at=JobRecord.started_at if candidate.started_at else now,
                error_code="",
                error_detail="",
            )
        )
        if result.rowcount != 1:
            return None
        session.flush()
        session.refresh(candidate)
        _audit_dispatch_transition(
            session,
            candidate,
            "job.leased",
            {"lease_owner": owner, "attempt": candidate.attempt},
        )
        return ClaimedJob(
            id=candidate.id,
            type=candidate.type,
            payload=dict(candidate.payload_json),
            attempt=candidate.attempt,
            max_attempts=candidate.max_attempts,
            lease_owner=owner,
            context=_frozen_system_context(candidate),
        )


def mark_running(
    context: TenantContext, job_id: str, owner: str, lease_seconds: int | None = None
) -> None:
    now = utcnow()
    lease_seconds = lease_seconds or config.JOB_LEASE_SECONDS
    with tenant_session(context) as session:
        result = session.execute(
            update(JobRecord)
            .where(
                JobRecord.id == job_id,
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.state == "leased",
                JobRecord.lease_owner == owner,
            )
            .values(
                state="running",
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("任务租约已丢失，不能开始外部调用")
        append_event(
            session,
            event_type="job.running",
            resource_type="job",
            resource_id=job_id,
            details={"lease_owner": owner},
        )
        # The user-facing Operation starts running when its first Job does.
        # Same transaction as the Job's own ``running`` write, so the two
        # projections cannot disagree about whether the command started.
        row = session.get(JobRecord, job_id)
        if row is not None:
            start_operation_for_job(
                session,
                row.operation_id,
                actor=context.executor or "system:worker",
                context=context,
            )


def heartbeat(
    context: TenantContext, job_id: str, owner: str, lease_seconds: int | None = None
) -> bool:
    now = utcnow()
    lease_seconds = lease_seconds or config.JOB_LEASE_SECONDS
    with tenant_session(context) as session:
        result = session.execute(
            update(JobRecord)
            .where(
                JobRecord.id == job_id,
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.state.in_(("leased", "running")),
                JobRecord.lease_owner == owner,
            )
            .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1


def succeed(context: TenantContext, job_id: str, owner: str) -> None:
    with tenant_session(context) as session:
        result = session.execute(
            update(JobRecord)
            .where(
                JobRecord.id == job_id,
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.lease_owner == owner,
                JobRecord.state == "running",
            )
            .values(
                state="succeeded",
                finished_at=utcnow(),
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("任务租约已丢失，不能提交成功")
        append_event(
            session,
            event_type="job.succeeded",
            resource_type="job",
            resource_id=job_id,
            details={"lease_owner": owner},
        )
        row = session.get(JobRecord, job_id)
        _aggregate_operation(
            session,
            context,
            row.operation_id if row else None,
            reason="sibling Job succeeded",
        )


def record_obsolete(
    context: TenantContext, job_id: str, *, code: str, detail: str = ""
) -> None:
    """Audit that a Job's admitted unit of work no longer needs doing.

    The Job still settles as ``succeeded`` -- it is complete, not failed --
    but ``job.obsolete`` keeps "we did the work" and "there was no work
    left" distinguishable in the evidence a reader reconstructs later.
    """
    with tenant_session(context) as session:
        row = session.get(JobRecord, job_id)
        if not row or row.workspace_id != context.workspace_id:
            return
        append_event(
            session,
            event_type="job.obsolete",
            resource_type="job",
            resource_id=job_id,
            details={"reason_code": code, "detail": detail[:500]},
        )


def mark_uncertain(
    context: TenantContext,
    job_id: str,
    owner: str,
    *,
    code: str,
    detail: str,
) -> None:
    """Release a dispatched Job whose external outcome cannot be determined.

    This is deliberately distinct from :func:`fail`: an ambiguous paid or
    remote call must not look like a confirmed failure, and it must never be
    put back on the automatic retry queue.  Recovery is admitted through the
    associated Operation instead.
    """
    with tenant_session(context) as session:
        row = session.get(JobRecord, job_id)
        if (
            not row
            or row.workspace_id != context.workspace_id
            or row.lease_owner != owner
            or row.state != "running"
        ):
            raise RuntimeError("任务租约已丢失，不能提交不确定状态")
        row.state = "uncertain"
        row.error_code = code
        row.error_detail = detail[:2000]
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        append_event(
            session,
            event_type="job.uncertain",
            resource_type="job",
            resource_id=job_id,
            details={"error_code": code},
        )
        from .ops_alerts import AlertCategory, Severity, emit as ops_emit

        ops_emit(
            Severity.CRITICAL,
            AlertCategory.UNCERTAIN_STATE,
            f"Job entered uncertain state: {code}",
            workspace_id=context.workspace_id,
            job_id=job_id,
            operation_id=row.operation_id or "",
        )
        _aggregate_operation(
            session, context, row.operation_id, reason=f"sibling Job uncertain: {code}"
        )


def fail(
    context: TenantContext,
    job_id: str,
    owner: str,
    *,
    code: str,
    detail: str,
    retryable: bool,
    retry_after_seconds: float | None = None,
) -> str:
    now = utcnow()
    with tenant_session(context) as session:
        row = session.get(JobRecord, job_id)
        if (
            not row
            or row.workspace_id != context.workspace_id
            or row.lease_owner != owner
            or row.state not in ("leased", "running")
        ):
            raise RuntimeError("任务租约已丢失，不能提交失败")
        if retryable and row.attempt < row.max_attempts:
            row.state = "retry_wait"
            delay = retry_after_seconds if retry_after_seconds is not None else min(60, 2 ** row.attempt)
            row.next_attempt_at = now + timedelta(seconds=max(0, min(3600, delay)))
        else:
            row.state = "failed"
            row.finished_at = now
        row.error_code = code
        row.error_detail = detail[:2000]
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        append_event(
            session,
            event_type="job.failed" if row.state == "failed" else "job.retry_scheduled",
            resource_type="job",
            resource_id=job_id,
            details={"error_code": code, "retryable": retryable, "state": row.state},
        )
        # Only a terminal failure folds the Operation. ``retry_wait`` is a
        # Job that has not finished, and reporting its command as failed
        # would be wrong the moment the retry succeeds.
        if row.state == "failed":
            _aggregate_operation(
                session,
                context,
                row.operation_id,
                reason=f"sibling Job failed: {code}",
            )
        return row.state


def cancel_resource(context: TenantContext, resource_type: str, resource_id: str) -> int:
    with tenant_session(context) as session:
        affected = session.scalars(
            select(JobRecord).where(
                JobRecord.resource_type == resource_type,
                JobRecord.resource_id == resource_id,
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.state.in_(("queued", "retry_wait", "leased")),
            )
        ).all()
        operation_ids = {job.operation_id for job in affected if job.operation_id}
        result = session.execute(
            update(JobRecord)
            .where(
                JobRecord.resource_type == resource_type,
                JobRecord.resource_id == resource_id,
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.state.in_(("queued", "retry_wait", "leased")),
            )
            .values(state="cancelled", finished_at=utcnow(), lease_owner=None, lease_expires_at=None)
        )
        if result.rowcount:
            append_event(
                session,
                event_type="jobs.cancelled",
                resource_type=resource_type,
                resource_id=resource_id,
                details={"count": int(result.rowcount)},
            )
            session.flush()
            for operation_id in sorted(operation_ids):
                _aggregate_operation(
                    session,
                    context,
                    operation_id,
                    reason="sibling Job cancelled with its resource",
                )
        return int(result.rowcount or 0)
