"""Settle legacy Operations whose Job ended without its handler (#22).

A legacy (``admission_contract_version`` 0) Operation is moved only by its
own domain handler -- the generic Job aggregation deliberately leaves it
alone (``app.platform.operations.AGGREGATED_ADMISSION_CONTRACT_VERSION``).
That leaves nobody to settle it when the handler never runs:

* the dispatcher rejects the Job because the Workspace's authorization epoch
  changed after it was enqueued (``failed`` / ``authorization_epoch_stale``);
* the Job's lease expires after ``running`` and recovery leaves it
  ``uncertain`` (``paid_call_outcome_unknown``);
* the Worker that claimed it had no handler for its type
  (``failed`` / ``unknown_job_type``, e.g. ``scale_export`` before #55).

The user then watches an Operation that is ``queued`` or ``running``
forever. This module is the reconciler for exactly those endings. Each
registered legacy Job type has a resolver that finds its Operation by the
way that command correlates them (``job.operation_id``, an id in the
payload, or a domain row) and knows how to move the domain resource to its
terminal state. A Job whose correlation does not check out is left alone.

Every fold runs in one transaction under the Operation's row lock and
re-reads the Job and the Operation first, so running the reconciler twice,
or from two Workers at once, settles an Operation once.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session, aliased

from .adapters.persistence.database import platform_session, tenant_session
from .adapters.persistence.models import JobRecord, WorkspaceRecord, utcnow
from .platform.audit import append_event
from .platform.job_policy import CrashPolicy, policy_for
from .platform.operations import (
    AGGREGATED_ADMISSION_CONTRACT_VERSION,
    OperationRecord,
    OperationState,
    transition_operation,
)
from .platform.tenancy import TenantContext, system_context, tenant_scope
from .platform.runtime_role import WORKER_ROLE, runs_as

logger = logging.getLogger(__name__)

AUTHORIZATION_EPOCH_STALE = "authorization_epoch_stale"
UNKNOWN_JOB_TYPE = "unknown_job_type"
PAID_CALL_OUTCOME_UNKNOWN = "paid_call_outcome_unknown"

#: ``(Job state, Job error_code)`` of an ending no handler saw, and the state
#: it settles the legacy Operation in. Nothing else is folded: a Job that a
#: handler failed or left uncertain had that handler settle its Operation.
TRIGGERS: dict[tuple[str, str], OperationState] = {
    ("failed", AUTHORIZATION_EPOCH_STALE): OperationState.FAILED,
    ("failed", UNKNOWN_JOB_TYPE): OperationState.FAILED,
    ("uncertain", PAID_CALL_OUTCOME_UNKNOWN): OperationState.UNCERTAIN,
}

#: The idle tick runs every few seconds; this sweep is bounded and only
#: needs to run once a minute per process.
SWEEP_INTERVAL_SECONDS = 60.0

_FOLDABLE_OPERATION_STATES = (OperationState.QUEUED.value, OperationState.RUNNING.value)


@dataclass(frozen=True)
class Correlation:
    """What a resolver found for one Job.

    ``fail`` moves the domain resource to its terminal failure and
    ``uncertain`` to its ambiguous state; ``None`` means the resource has
    no state of its own to move. ``dispatched`` is the resource's evidence
    that external work may already have started -- an ``epoch_stale``
    rejection is then not a failure but an unknown outcome.
    """

    operation_id: str
    fail: Callable[[str], bool | None] | None = None
    uncertain: Callable[[], None] | None = None
    dispatched: bool = False
    settles_operation: bool = False
    """The domain settles the Operation itself from the resource (a
    Generation Run's projection over all its items): ``fail`` moves the
    resource and returns whether it did, and this reconciler writes no
    Operation transition of its own."""


Resolver = Callable[[Session, TenantContext, JobRecord], "Correlation | None"]


@dataclass(frozen=True)
class LegacyCommand:
    command_types: frozenset[str]
    resolve: Resolver
    needs_meta_mode: bool = False
    """The resource's RLS policy needs the payload's ``meta_mode``."""


def _payload(job: JobRecord) -> dict:
    return dict(job.payload_json or {})


def _payload_text(job: JobRecord, key: str) -> str:
    return str(_payload(job).get(key) or "")


# ---------------------------------------------------------------------------
# Resolvers, one per legacy Job type
# ---------------------------------------------------------------------------


def _operation_only(key: str = "operation_id") -> Resolver:
    """Commands whose payload names the Operation and own no resource state."""

    def resolve(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
        operation_id = _payload_text(job, key)
        return Correlation(operation_id) if operation_id else None

    return resolve


def _resolve_scale_export(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    # ``create_export`` sets ``job.operation_id``. A ScaleExport has no state
    # column: ``completed_at`` stays empty and its Operation *is* its state,
    # so a failed Operation is the export's terminal state (downloads refuse
    # an export that never completed).
    return Correlation(job.operation_id) if job.operation_id else None


def _resolve_reference_gate(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .adapters.persistence.models import ReferenceObjectRecord
    from .library import references

    operation_id = _payload_text(job, "operation_id")
    object_id = _payload_text(job, "object_id")
    if not operation_id or not object_id:
        return None
    record = session.scalar(
        select(ReferenceObjectRecord)
        .where(
            ReferenceObjectRecord.id == object_id,
            ReferenceObjectRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if record is None:
        return None

    def fail(code: str) -> None:
        # ``rejected`` is a content verdict: it purges the original and bans
        # the same bytes from ever being uploaded again. A gate that never
        # ran decided nothing, so the object stays unreleased (quarantined,
        # never usable) with the reason recorded, exactly as a gate whose
        # content-safety service was unavailable leaves it. Uploading the
        # same file again re-runs the gate on this object.
        if record.state != references.STATE_QUARANTINED:
            return
        checks = dict(record.checks_json or {})
        checks["failure"] = {"code": code, "detail": "上传门禁未运行：任务在执行前已失效"}
        record.checks_json = checks
        record.updated_at = utcnow()

    return Correlation(operation_id, fail=fail)


def _resolve_export_package(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .generation.models import ExportPackageRecord

    package_id = _payload_text(job, "package_id")
    if not package_id:
        return None
    package = session.scalar(
        select(ExportPackageRecord)
        .where(
            ExportPackageRecord.id == package_id,
            ExportPackageRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if package is None:
        return None

    def fail(code: str) -> None:
        if package.state not in ("pending", "building"):
            return
        package.state = "failed"
        package.failure_evidence = json.dumps(
            {"code": code, "detail": "导出任务在构建前已失效"}
        )
        package.updated_at = utcnow()

    return Correlation(package.operation_id, fail=fail)


def _resolve_upload_verification(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .platform import upload_intents

    operation_id = _payload_text(job, "operation_id")
    intent_id = _payload_text(job, "intent_id")
    if not operation_id or not intent_id:
        return None

    def fail(code: str) -> None:
        # The handler's own rejection for a stale epoch (spec §7.8 step 2):
        # ``rejected`` plus the staging-cleanup Job, only from pending/verifying.
        # Any other ending is not an upload verdict; the intent stays as it is.
        if code in upload_intents.REJECTION_CODES:
            upload_intents.reject_before_verification(session, intent_id, code)

    return Correlation(operation_id, fail=fail)


def _resolve_launch_provisioning(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .launch.models import LaunchProvisioningRecord
    from .launch.saga import record_provisioning_failure

    operation_id = _payload_text(job, "operation_id")
    provisioning_id = _payload_text(job, "provisioning_id")
    if not operation_id or not provisioning_id:
        return None
    provisioning = session.scalar(
        select(LaunchProvisioningRecord).where(
            LaunchProvisioningRecord.id == provisioning_id,
            LaunchProvisioningRecord.workspace_id == context.workspace_id,
            LaunchProvisioningRecord.meta_mode == context.meta_mode,
            LaunchProvisioningRecord.operation_id == operation_id,
        )
    )
    if provisioning is None:
        return None

    def fail(code: str) -> None:
        record_provisioning_failure(session, context, provisioning_id, reason=code)

    # Any step past ``pending`` may have written to Meta: that is an unknown
    # outcome, which Launch recovery (not this reconciler) resolves.
    return Correlation(
        operation_id, fail=fail, dispatched=provisioning.provisioning_state != "pending",
    )


def _resolve_launch_recovery(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .launch.models import LaunchReconciliationRecord

    reconciliation_id = _payload_text(job, "reconciliation_id")
    if not reconciliation_id:
        return None
    reconciliation = session.scalar(
        select(LaunchReconciliationRecord).where(
            LaunchReconciliationRecord.id == reconciliation_id,
            LaunchReconciliationRecord.workspace_id == context.workspace_id,
            LaunchReconciliationRecord.meta_mode == context.meta_mode,
        )
    )
    if reconciliation is None:
        return None
    # The reconciliation stays ``open``: its only terminal state records the
    # *original* Operation's outcome, which nobody has read back yet.
    return Correlation(reconciliation.recovery_operation_id)


def _resolve_planning_call(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .generation.paid_execution import _settle
    from .marketing.models import PlanningCallRecord
    from .marketing.planner import _reservation

    call = session.scalar(
        select(PlanningCallRecord)
        .where(
            PlanningCallRecord.id == _payload_text(job, "call_id"),
            PlanningCallRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if call is None or call.job_id != job.id:
        return None

    def fail(code: str) -> None:
        if call.state != "queued":
            return
        call.state, call.error_code = "failed", code
        reservation = _reservation(session, context, call)
        if reservation:
            _settle(session, reservation, None, called=False)

    def uncertain() -> None:
        # ``execute_planning``'s own reading of a call left ``running``.
        if call.state != "running":
            return
        call.state, call.error_code = "uncertain", "planner_outcome_unknown"
        reservation = _reservation(session, context, call)
        if reservation:
            reservation.state = "uncertain"

    return Correlation(
        call.operation_id, fail=fail, uncertain=uncertain, dispatched=call.state == "running",
    )


def _resolve_ai_qc_call(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .entitlements.cost_models import ProviderCallAttemptRecord
    from .generation.paid_execution import _settle
    from .marketing.ai_qc_models import MarketingAIQCRecord
    from .marketing.ai_qc_worker import reservation_for

    call = session.scalar(
        select(MarketingAIQCRecord)
        .where(
            MarketingAIQCRecord.id == _payload_text(job, "call_id"),
            MarketingAIQCRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if call is None or call.job_id != job.id:
        return None

    def fail(code: str) -> None:
        if call.state != "queued":
            return
        call.state, call.error_code = "failed", code
        reservation = reservation_for(session, context, call)
        if reservation:
            _settle(session, reservation, None, called=False)

    def uncertain() -> None:
        # ``ai_qc_worker.uncertain`` without its Operation transition.
        if call.state != "running":
            return
        call.state, call.error_code = "uncertain", "qc_provider_outcome_unknown"
        reservation = reservation_for(session, context, call)
        if reservation:
            reservation.state = "uncertain"
        attempt = session.scalar(
            select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == context.workspace_id,
                ProviderCallAttemptRecord.call_semantic_key_hash == call.id,
            )
        )
        if attempt:
            attempt.dispatch_state, attempt.error_code = "uncertain", call.error_code

    return Correlation(
        call.operation_id, fail=fail, uncertain=uncertain, dispatched=call.state == "running",
    )


def _resolve_image_edit(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .marketing.workflow_models import MarketingImageEditRecord

    edit = session.scalar(
        select(MarketingImageEditRecord)
        .where(
            MarketingImageEditRecord.id == _payload_text(job, "edit_id"),
            MarketingImageEditRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if edit is None or job.operation_id != edit.operation_id:
        return None

    def fail(code: str) -> None:
        if edit.state == "queued":
            edit.state, edit.error_code = "failed", code

    return Correlation(edit.operation_id, fail=fail)


def _resolve_handoff(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .marketing.workflow_models import MarketingHandoffRecord

    handoff = session.scalar(
        select(MarketingHandoffRecord)
        .where(
            MarketingHandoffRecord.id == _payload_text(job, "handoff_id"),
            MarketingHandoffRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if handoff is None:
        return None
    # ``retry_handoff`` points the handoff at a new Operation and enqueues a
    # new Job. Only the newest Job speaks for ``handoff.operation_id``.
    newest = session.scalar(
        select(JobRecord.id)
        .where(
            JobRecord.workspace_id == context.workspace_id,
            JobRecord.type == job.type,
            JobRecord.resource_type == "marketing_handoff",
            JobRecord.resource_id == handoff.id,
        )
        .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
        .limit(1)
    )
    if newest != job.id:
        return None

    def fail(code: str) -> None:
        if handoff.state not in ("ready", "failed"):
            # ``failed`` is what ``retry_handoff`` accepts.
            handoff.state, handoff.error_code = "failed", code

    return Correlation(handoff.operation_id, fail=fail)


def _resolve_run_item(session: Session, context: TenantContext, job: JobRecord) -> Correlation | None:
    from .entitlements.cost_models import SpendReservationRecord
    from .generation.models import RunItemRecord
    from .generation.paid_execution import _settle
    from .generation.worker import fail_run_item_after_handler_fault

    # A paid item left uncertain by lease expiry is the Run's own recovery
    # (``generation.reconcile``), not an ending this reconciler decides.
    if job.state != "failed" or not job.operation_id:
        return None
    item = session.scalar(
        select(RunItemRecord)
        .where(
            RunItemRecord.id == _payload_text(job, "run_item_id"),
            RunItemRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if item is None or item.id != job.resource_id:
        return None

    def fail(code: str) -> bool:
        # Only an item that never started: a ``running`` one may have sent
        # its paid call, and the Run's recovery reads that back.
        if item.state not in ("pending", "queued"):
            return False
        reservation = session.scalar(
            select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == context.workspace_id,
                SpendReservationRecord.job_id == job.id,
            )
        )
        if reservation is not None and reservation.state == "reserved":
            _settle(session, reservation, None, called=False)
        # Moves the item to ``failed`` and recomputes the Run, whose
        # projection settles the command Operation (``run_operations``).
        fail_run_item_after_handler_fault(session, context, item.id, error_summary=code)
        return True

    return Correlation(job.operation_id, fail=fail, settles_operation=True)


#: Every legacy command whose Operation only its handler settles, keyed by
#: Job type. ``tests/test_legacy_operations.py`` fails when a legacy
#: Operation is created next to an enqueue of a type this does not list.
LEGACY_COMMANDS: dict[str, LegacyCommand] = {
    "scale_export": LegacyCommand(frozenset({"scale_export"}), _resolve_scale_export),
    "library.reference_gate": LegacyCommand(frozenset({"reference_gate"}), _resolve_reference_gate),
    "generation.export_package": LegacyCommand(
        frozenset({"generation.export"}), _resolve_export_package),
    "verify_upload_intent": LegacyCommand(
        frozenset({"finalize_upload"}), _resolve_upload_verification),
    "launch_provisioning": LegacyCommand(
        frozenset({"launch_provisioning"}), _resolve_launch_provisioning, needs_meta_mode=True),
    "launch_recovery": LegacyCommand(
        frozenset({"launch_recovery"}), _resolve_launch_recovery, needs_meta_mode=True),
    "library.connection_check": LegacyCommand(frozenset({"connection_check"}), _operation_only()),
    "library.manual_fact_import": LegacyCommand(frozenset({"manual_fact_import"}), _operation_only()),
    "marketing.plan": LegacyCommand(
        frozenset({"marketing.plan", "marketing.understanding", "marketing.explanation"}),
        _resolve_planning_call),
    "marketing.ai_qc": LegacyCommand(frozenset({"marketing.ai_qc"}), _resolve_ai_qc_call),
    "marketing.image_edit": LegacyCommand(frozenset({"marketing.image_edit"}), _resolve_image_edit),
    "marketing.export": LegacyCommand(frozenset({"marketing.export"}), _resolve_handoff),
    "marketing.resolve_call": LegacyCommand(frozenset({"marketing.resolve_call"}), _operation_only()),
    "generation.run_item": LegacyCommand(
        frozenset({"generation.submit", "generation.retry_failed_items", "generation.resume_paused_items"}),
        _resolve_run_item),
}


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def _trigger(job: JobRecord) -> OperationState | None:
    return TRIGGERS.get((job.state, job.error_code or ""))


def _claim_operation(
    session: Session, context: TenantContext, operation_id: str, command: LegacyCommand,
    *, compare_and_set: bool = True,
) -> OperationRecord | None:
    """The legacy Operation under its row lock, or ``None``.

    ``None`` when the id names no Operation of this command in this
    Workspace, an aggregated (v1) one, or one already settled -- including
    by a concurrent fold. The compare-and-set write is what serializes two
    folds on SQLite, which ignores ``FOR UPDATE`` and reads outside its
    write transaction.
    """
    operation = session.scalars(
        select(OperationRecord)
        .where(
            OperationRecord.id == operation_id,
            OperationRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        operation is None
        or operation.admission_contract_version >= AGGREGATED_ADMISSION_CONTRACT_VERSION
        or operation.command_type not in command.command_types
        or operation.state not in _FOLDABLE_OPERATION_STATES
    ):
        return None
    if not compare_and_set:
        return operation
    claimed = session.execute(
        update(OperationRecord)
        .where(
            OperationRecord.id == operation_id,
            OperationRecord.workspace_id == context.workspace_id,
            OperationRecord.state == operation.state,
        )
        .values(updated_at=utcnow())
    )
    if claimed.rowcount != 1:
        return None
    session.refresh(operation)
    return operation


def _settle_failed(
    session: Session, context: TenantContext, operation: OperationRecord, reason: str,
    *, meta_mode: str | None,
) -> None:
    transition_operation(
        session, operation.id, OperationState.FAILED, reason=reason, actor=context.executor,
        meta_mode=meta_mode,
    )


def _usage_meta_mode(job: JobRecord, operation: OperationRecord) -> str | None:
    """The Meta mode a Meta Operation ran in, for its Usage bucket (#146).

    Its Job's payload names it. ``scale_export`` folds without binding it
    (its resolver reads no Meta row, and a Job enqueued before #55 carries
    none), so this reads the payload rather than the fold's context.
    """
    from .entitlements.operation_usage import META_OPERATION_COMMAND_TYPES

    if operation.command_type not in META_OPERATION_COMMAND_TYPES:
        return None
    return _payload_text(job, "meta_mode") or None


def _settle_uncertain(session: Session, context: TenantContext, operation: OperationRecord, reason: str) -> None:
    if operation.state == OperationState.QUEUED.value:
        # The Job reached ``running`` (or dispatched external work) before its
        # handler could move the Operation. Record that dispatch rather than an
        # impossible ``queued -> uncertain`` jump -- the same two events Launch
        # recovery writes.
        transition_operation(
            session, operation.id, OperationState.RUNNING,
            reason="dispatch observed while reconciling a legacy Operation",
            actor=context.executor,
        )
    transition_operation(session, operation.id, OperationState.UNCERTAIN, reason=reason, actor=context.executor)


def _fold_in_session(session: Session, base: TenantContext, job_id: str) -> str | None:
    """Settle the Operation of one handler-less Job; return its new state."""
    job = session.scalars(
        select(JobRecord)
        .where(JobRecord.id == job_id, JobRecord.workspace_id == base.workspace_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if job is None:
        return None
    command = LEGACY_COMMANDS.get(job.type)
    outcome = _trigger(job)
    if command is None or outcome is None:
        return None
    # The Job's initiator, for the evidence; the Workspace's current epoch,
    # because the Job's own is the one that got it rejected.
    context = _fold_context(job, base.authorization_epoch)
    if command.needs_meta_mode:
        meta_mode = _payload_text(job, "meta_mode")
        if not meta_mode:
            return None
        context = replace(context, meta_mode=meta_mode)
    with tenant_scope(session, context):
        correlation = command.resolve(session, context, job)
        if correlation is None or not correlation.operation_id:
            return None
        # A domain that settles its own Operation serializes on its own
        # resource (the RunItem's and the Run's revision compare-and-set).
        operation = _claim_operation(
            session, context, correlation.operation_id, command,
            compare_and_set=not correlation.settles_operation,
        )
        if operation is None:
            return None

        reason = job.error_code
        if correlation.settles_operation:
            if outcome is not OperationState.FAILED or correlation.fail is None:
                return None
            if not correlation.fail(job.error_code):
                return None
            session.refresh(operation)
            to_state = operation.state
        else:
            dispatched = correlation.dispatched or (
                # An unfenced paid/external Job whose handler already moved
                # the Operation to ``running`` may have sent its call.
                policy_for(job.type).crash is CrashPolicy.UNCERTAIN
                and operation.state == OperationState.RUNNING.value
            )
            if outcome is OperationState.FAILED and dispatched:
                outcome = OperationState.UNCERTAIN
            if outcome is OperationState.FAILED:
                if correlation.fail is not None:
                    correlation.fail(job.error_code)
                _settle_failed(session, context, operation, reason, meta_mode=_usage_meta_mode(job, operation))
            else:
                if correlation.uncertain is not None:
                    correlation.uncertain()
                _settle_uncertain(session, context, operation, reason)
            to_state = outcome.value
        append_event(
            session,
            event_type="operation.legacy_reconciled",
            resource_type="operation",
            resource_id=operation.id,
            details={
                "job_id": job.id,
                "job_type": job.type,
                "job_error_code": job.error_code,
                "to_state": to_state,
            },
        )
        return to_state


def fold_legacy_job(job_id: str, *, workspace_id: str, authorization_epoch: int) -> str | None:
    """Settle one handler-less legacy Job's Operation, in its own transaction.

    ``authorization_epoch`` is the Workspace's *current* one: the Job's own
    is the stale one that got it rejected. Returns the state the Operation
    was moved to, or ``None`` when nothing was (not a legacy ending, unclear
    correlation, or already settled).
    """
    base = system_context(
        workspace_id, initiator_id=None, authorization_epoch=authorization_epoch,
    )
    with tenant_session(base) as session:
        return _fold_in_session(session, base, job_id)


def _fold_context(job: JobRecord, authorization_epoch: int) -> TenantContext:
    return system_context(
        job.workspace_id,
        initiator_id=job.initiator_id,
        initiator_role=job.initiator_role if job.initiator_role in ("owner", "member") else None,
        request_id=job.request_id,
        trace_id=job.trace_id,
        authorization_epoch=authorization_epoch,
    )


def _candidate_filter():
    return and_(
        JobRecord.type.in_(tuple(LEGACY_COMMANDS)),
        or_(*(
            and_(JobRecord.state == state, JobRecord.error_code == code)
            for state, code in TRIGGERS
        )),
    )


def _legacy_candidates(session: Session, limit: int) -> list[tuple[str, int, str]]:
    """``(workspace_id, current authorization epoch, job_id)``, newest first.

    On PostgreSQL the Worker role's untenanted session sees no row, so
    ``marketai_legacy_operation_candidates_v1`` lists ids across Workspaces;
    SQLite has no RLS and runs the same query directly. A Job that names its
    Operation is listed only while that Operation is open.
    """
    if session.get_bind().dialect.name == "postgresql":
        rows = session.execute(
            text(
                "SELECT workspace_id, authorization_epoch, job_id "
                "FROM public.marketai_legacy_operation_candidates_v1("
                "CAST(:job_types AS text[]), CAST(:candidate_limit AS integer))"
            ),
            {"job_types": sorted(LEGACY_COMMANDS), "candidate_limit": limit},
        ).all()
    else:
        operation = aliased(OperationRecord)
        named = func.coalesce(JobRecord.operation_id, JobRecord.payload_json["operation_id"].as_string())
        rows = session.execute(
            select(JobRecord.workspace_id, WorkspaceRecord.authorization_epoch, JobRecord.id)
            .join(WorkspaceRecord, WorkspaceRecord.id == JobRecord.workspace_id)
            .outerjoin(
                operation,
                and_(operation.workspace_id == JobRecord.workspace_id, operation.id == named),
            )
            .where(
                _candidate_filter(),
                or_(operation.id.is_(None), operation.state.in_(_FOLDABLE_OPERATION_STATES)),
            )
            .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
            .limit(limit)
        ).all()
    return [(str(ws), int(epoch), str(job_id)) for ws, epoch, job_id in rows]


@runs_as(WORKER_ROLE)
def reconcile_legacy_operations(limit: int = 200) -> int:
    """Fold every handler-less legacy Job's Operation, newest first.

    Each Job is folded in its own transaction under a TenantContext signed
    for its Workspace's current epoch. Returns how many Operations moved.
    """
    with platform_session() as session:
        candidates = _legacy_candidates(session, limit)
    moved = 0
    for workspace_id, authorization_epoch, job_id in candidates:
        try:
            if fold_legacy_job(
                job_id, workspace_id=workspace_id, authorization_epoch=authorization_epoch,
            ) is not None:
                moved += 1
        except Exception:  # noqa: BLE001 -- one bad row must not stop the sweep
            logger.exception("legacy_operation_fold_failed", extra={"job_id": job_id})
    if moved:
        from .platform.ops_alerts import AlertCategory, Severity, emit as ops_emit

        ops_emit(
            Severity.INFO,
            AlertCategory.RECONCILIATION,
            f"Legacy Operation reconciliation: settled {moved} operations",
            details={"settled": moved},
        )
    return moved


_sweep_lock = threading.Lock()
_last_sweep: float | None = None


@runs_as(WORKER_ROLE)
def reconcile_legacy_operations_if_due(now: float | None = None) -> int:
    """The Worker's idle-tick entry: at most one sweep per interval per process."""
    global _last_sweep
    now = time.monotonic() if now is None else now
    with _sweep_lock:
        if _last_sweep is not None and now - _last_sweep < SWEEP_INTERVAL_SECONDS:
            return 0
        _last_sweep = now
    return reconcile_legacy_operations()
