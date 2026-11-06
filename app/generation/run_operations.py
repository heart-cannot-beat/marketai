"""Settle a GenerationRun's command Operations from the Run projection.

Every Run command (``submit`` / ``retry_failed_items`` / ``resume_paused_items``
/ ``cancel`` / ``reconcile``) owns an Operation born through the platform's
idempotency protocol at ``admission_contract_version`` 0. The generic
aggregator (:func:`app.platform.operations.aggregate_operation_from_jobs`,
``jobs.reconcile_admitted_operations``) leaves those alone by design -- the
domain owns their transitions -- and until this module nothing in the domain
ever closed them. The ``submit`` Operation stayed ``running`` after its Run
had succeeded and ``cancel`` / ``reconcile`` stayed ``queued`` forever, so
the Run page kept the "⚙️ Running" strip under a succeeded Run and polled at
the fast cadence indefinitely (``web/components/generation/shared/
RunProgressSource.ts`` only settles once the Run *and* its latest command
Operation are terminal).

Two kinds of command, two rules:

* **Item-driving commands** (``submit``, ``retry_failed_items``,
  ``resume_paused_items``) follow the Run. ``_sync_run_state`` calls
  :func:`settle_run_command_operations` right after it recomputes
  ``run.status`` from the RunItems (``derive_run_state``), so the Run
  projection -- which already includes QC -- decides the outcome: a Run that
  ends ``succeeded`` closes its open item-driving Operations ``succeeded``,
  an ``uncertain`` Run parks them ``uncertain`` until reconciliation, and a
  Run that returns to ``running`` after an ``uncertain`` spell resumes them.
  A command whose Operation is already terminal is never touched again, so a
  ``retry_failed_items`` command after a ``failed`` Run reopens the Run under
  its *own* Operation while the ``submit`` Operation keeps its ``failed``
  history.
* **Synchronous commands** (``cancel``, ``reconcile``) are complete the
  moment their durable effect is written in the command's own transaction,
  so :func:`settle_synchronous_command_operation` closes them ``succeeded``
  there and then. ``cancel`` additionally closes the Run's open item-driving
  Operations ``cancelled`` because it moves the Run itself to ``cancelled``
  without going through ``_sync_run_state``.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import OperationRecord
from app.generation.models import GenerationRunCommandRecord
from app.platform.operations import (
    TERMINAL_STATES,
    OperationState,
    transition_operation,
    validate_transition,
)
from app.platform.tenancy import TenantContext

logger = logging.getLogger(__name__)

__all__ = [
    "ITEM_DRIVING_COMMAND_KINDS",
    "RUN_STATUS_TO_OPERATION_STATE",
    "settle_run_command_operations",
    "settle_synchronous_command_operation",
]

#: ``GenerationRunCommandRecord.command_kind`` values whose Operation stands
#: for RunItem work still owed to the Run, and therefore follows the Run.
ITEM_DRIVING_COMMAND_KINDS = ("submit", "retry_failed_items", "resume_paused_items")

#: ``derive_run_state`` outcome -> the Operation state it implies for an open
#: item-driving command. ``running`` is listed so an Operation parked
#: ``uncertain`` (or still ``queued``) resumes when the Run does.
RUN_STATUS_TO_OPERATION_STATE: dict[str, OperationState] = {
    "running": OperationState.RUNNING,
    "uncertain": OperationState.UNCERTAIN,
    "succeeded": OperationState.SUCCEEDED,
    "partially_succeeded": OperationState.PARTIALLY_SUCCEEDED,
    "failed": OperationState.FAILED,
    "cancelled": OperationState.CANCELLED,
}


def settle_run_command_operations(
    session: Session,
    ctx: TenantContext,
    run_id: str,
    run_status: str,
    *,
    actor: str,
    command_kinds: Sequence[str] = ITEM_DRIVING_COMMAND_KINDS,
) -> list[OperationRecord]:
    """Move every open item-driving command Operation of ``run_id`` to the state ``run_status`` implies.

    Returns the Operations this call moved. Safe to call after every RunItem
    transition: terminal Operations are skipped, a derived state equal to
    the current one is a no-op, and a pair the state machine does not admit
    is logged and skipped rather than raised -- this runs inside the Worker's
    RunItem transaction and must never roll that work back.
    """
    target = RUN_STATUS_TO_OPERATION_STATE.get(run_status)
    if target is None:
        return []

    commands = (
        session.execute(
            select(GenerationRunCommandRecord)
            .where(
                GenerationRunCommandRecord.workspace_id == ctx.workspace_id,
                GenerationRunCommandRecord.generation_run_id == run_id,
                GenerationRunCommandRecord.command_kind.in_(tuple(command_kinds)),
            )
            .order_by(GenerationRunCommandRecord.created_at)
        )
        .scalars()
        .all()
    )

    moved: list[OperationRecord] = []
    for command in commands:
        operation = session.get(OperationRecord, command.operation_id)
        if operation is None or operation.workspace_id != ctx.workspace_id:
            continue
        settled = _settle(
            session,
            operation,
            target,
            actor=actor,
            reason=f"generation run {run_id} is {run_status}",
        )
        if settled is not None:
            moved.append(settled)
    return moved


def settle_synchronous_command_operation(
    session: Session,
    operation_id: str,
    *,
    actor: str,
    reason: str,
) -> OperationRecord | None:
    """Close the Operation of a command whose whole effect is already durable.

    ``queued`` -> ``running`` -> ``succeeded`` in one call (the state machine
    has no ``queued -> succeeded`` edge: a success implies a dispatch, and
    the dispatch is the command transaction itself). A replayed command finds
    its Operation terminal already and changes nothing.
    """
    operation = session.get(OperationRecord, operation_id)
    if operation is None:
        return None
    return _settle(session, operation, OperationState.SUCCEEDED, actor=actor, reason=reason)


def _settle(
    session: Session,
    operation: OperationRecord,
    target: OperationState,
    *,
    actor: str,
    reason: str,
) -> OperationRecord | None:
    current = OperationState(operation.state)
    if current in TERMINAL_STATES or current == target:
        return None

    if not validate_transition(current, target):
        # ``queued``/``admitted`` reach a terminal outcome only through
        # ``running``: record the dispatch explicitly rather than inventing a
        # history in which work completed without starting.
        if (
            current in (OperationState.QUEUED, OperationState.ADMITTED)
            and validate_transition(current, OperationState.RUNNING)
            and validate_transition(OperationState.RUNNING, target)
        ):
            transition_operation(
                session,
                operation.id,
                OperationState.RUNNING,
                reason=reason,
                actor=actor,
            )
            current = OperationState.RUNNING
        else:
            logger.warning(
                "generation_command_operation_transition_rejected",
                extra={
                    "workspace_id": operation.workspace_id,
                    "operation_id": operation.id,
                    "command_type": operation.command_type,
                    "from_state": current.value,
                    "to_state": target.value,
                },
            )
            return None

    return transition_operation(
        session,
        operation.id,
        target,
        reason=reason,
        actor=actor,
    )
