"""Operation model and state machine (spec §5.1-5.2).

An Operation is the user-facing representation of an asynchronous domain
command: a persistent database record (``operation_records``) with an
append-only transition history (``operation_state_events``). The record's
``state`` column is a *projection*, never rewritten directly -- every
transition inserts a new immutable event and advances the projection in the
same flush, so the two can never disagree about what happened.

The actual SQLAlchemy tables live in ``app.adapters.persistence.models``
(the codebase's single home for mapped classes); ``OperationRecord`` and
``OperationStateEvent`` are re-exported here under the names spec §5.1 uses,
so callers only need this module.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    JobRecord,
    OperationRecord,
    OperationStateEventRecord as OperationStateEvent,
    new_id,
    utcnow,
)
from .tenancy import TenantContext, current_context

logger = logging.getLogger(__name__)

__all__ = [
    "OperationState",
    "OperationRecord",
    "OperationStateEvent",
    "OperationResultRef",
    "VALID_TRANSITIONS",
    "TERMINAL_STATES",
    "NON_TERMINAL_STATES",
    "JOB_TERMINAL_STATES",
    "AGGREGATED_ADMISSION_CONTRACT_VERSION",
    "validate_transition",
    "OperationNotFoundError",
    "InvalidOperationTransitionError",
    "create_operation",
    "get_operation",
    "transition_operation",
    "derive_operation_outcome",
    "aggregate_operation_from_jobs",
    "start_operation_for_job",
    "result_ref_of",
]


class OperationState(StrEnum):
    #: Spec §3.2's entry state for a *paid* command: PaidOperationAdmission
    #: has claimed the semantic intent, created the Jobs and reserved the
    #: spend, but no Worker has picked any of them up yet. Distinct from
    #: ``queued``, which the unpriced ``create_operation`` path still uses.
    ADMITTED = "admitted"
    QUEUED = "queued"
    RUNNING = "running"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


#: Terminal states have no outgoing transitions -- they are the end of the
#: machine, not a resting point a reconciler or retry can leave.
TERMINAL_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.PARTIALLY_SUCCEEDED,
        OperationState.CANCELLED,
    }
)


#: Every state an Operation can still leave. The polling endpoint uses this
#: to decide whether to hint ``Retry-After`` (spec §5.6).
NON_TERMINAL_STATES = frozenset(
    {
        OperationState.ADMITTED,
        OperationState.QUEUED,
        OperationState.RUNNING,
        OperationState.UNCERTAIN,
    }
)


#: Spec §5.2. Backward transitions are prohibited except ``uncertain ->
#: running`` -- the one case where a reconciler determines work can safely
#: resume. Neither the user nor an automatic retry may leave ``uncertain``.
#:
#: ``uncertain`` reaches every terminal state, ``partially_succeeded``
#: included: reconciliation can resolve the ambiguous call either way, and a
#: command whose siblings then read "some succeeded, some failed" is exactly
#: partially succeeded. Omitting it would make
#: :func:`aggregate_operation_from_jobs` raise inside the Job transition that
#: called it, rolling back that Job's own settled state.
#:
#: ``admitted`` is the paid-command entry state. It reaches ``running`` when
#: the first sibling Job starts, and it reaches ``cancelled``/``failed``/
#: ``uncertain`` directly because a Job admitted under a since-revoked
#: authorization epoch, or cancelled before any Worker claimed it, is
#: terminal without anything ever having run. It deliberately does *not*
#: reach ``succeeded``/``partially_succeeded``: a success implies a dispatch,
#: so :func:`aggregate_operation_from_jobs` records that dispatch as an
#: explicit ``admitted -> running`` event first rather than inventing a
#: history in which work completed without starting.
#:
#: ``queued`` is the unpriced entry state and reaches ``failed`` directly for
#: the same reason ``admitted`` does: a legacy command whose Job the
#: dispatcher rejected under a since-revoked authorization epoch failed
#: without anything having run (#22, ``app.legacy_operations``).
VALID_TRANSITIONS: dict[OperationState, set[OperationState]] = {
    OperationState.ADMITTED: {
        OperationState.RUNNING,
        OperationState.CANCELLED,
        OperationState.FAILED,
        OperationState.UNCERTAIN,
    },
    OperationState.QUEUED: {
        OperationState.RUNNING,
        OperationState.CANCELLED,
        OperationState.FAILED,
    },
    OperationState.RUNNING: {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.PARTIALLY_SUCCEEDED,
        OperationState.CANCELLED,
        OperationState.UNCERTAIN,
    },
    OperationState.UNCERTAIN: {
        OperationState.SUCCEEDED,
        OperationState.PARTIALLY_SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.RUNNING,
    },
    OperationState.SUCCEEDED: set(),
    OperationState.FAILED: set(),
    OperationState.PARTIALLY_SUCCEEDED: set(),
    OperationState.CANCELLED: set(),
}


def validate_transition(from_state: OperationState, to_state: OperationState) -> bool:
    """Whether the state machine admits ``from_state`` -> ``to_state``.

    Any pair not listed in :data:`VALID_TRANSITIONS` is rejected -- there is
    no default "allow" path.
    """
    return to_state in VALID_TRANSITIONS.get(from_state, set())


@dataclass(frozen=True)
class OperationResultRef:
    """Resource type + id, not a URL.

    The BFF does not rewrite response bodies, so a URL pointing at a private
    FastAPI path would be unusable by the browser. The browser builds its
    own navigation path from ``resource_type`` and ``resource_id`` instead.
    Persisted as the flattened ``result_resource_type`` /
    ``result_resource_id`` columns on :class:`OperationRecord`; see
    :func:`result_ref_of`.
    """

    resource_type: str
    resource_id: str


class OperationNotFoundError(LookupError):
    """Raised when an Operation id does not resolve inside the caller's Workspace."""


class InvalidOperationTransitionError(ValueError):
    """Raised when a requested transition is not permitted by the state machine."""

    def __init__(self, from_state: str, to_state: str):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"invalid Operation transition: {from_state!r} -> {to_state!r}")


def create_operation(
    session: Session,
    *,
    command_type: str,
    idempotency_key_hash: str | None = None,
    operation_id: str | None = None,
    actor: str | None = None,
    context: TenantContext | None = None,
    initial_state: OperationState = OperationState.QUEUED,
    reason: str = "Operation created",
    semantic_key_hash: str | None = None,
    request_fingerprint_hash: str | None = None,
    result_ref: OperationResultRef | None = None,
    admission_contract_version: int = 0,
    now: datetime | None = None,
) -> OperationRecord:
    """Create a new Operation and append its ``None -> initial_state`` event.

    Both writes happen in the caller's transaction (spec §5.5): an Operation
    is never persisted without its initial event, or vice versa. This is the
    only constructor -- the paid path (:mod:`app.entitlements.admission`)
    comes through here with ``initial_state=OperationState.ADMITTED`` rather
    than assembling an :class:`OperationRecord` of its own, so an admitted
    Operation can never be born without the history row that
    :func:`transition_operation` and every later reader assume exists.
    """
    context = context or current_context(session)
    now = now or utcnow()
    operation = OperationRecord(
        id=operation_id or new_id("op"),
        workspace_id=context.workspace_id,
        initiator_id=context.initiator_id,
        command_type=command_type,
        state=initial_state.value,
        idempotency_key_hash=idempotency_key_hash,
        semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash,
        result_resource_type=result_ref.resource_type if result_ref else None,
        result_resource_id=result_ref.resource_id if result_ref else None,
        admission_contract_version=admission_contract_version,
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    session.flush()
    session.add(
        OperationStateEvent(
            id=new_id("ose"),
            workspace_id=context.workspace_id,
            operation_id=operation.id,
            from_state=None,
            to_state=initial_state.value,
            reason=reason,
            actor=actor or context.initiator_id or context.executor,
            created_at=now,
        )
    )
    session.flush()
    return operation


def get_operation(session: Session, operation_id: str) -> OperationRecord:
    """Fetch an Operation scoped to the session's bound Workspace, or raise."""
    context = current_context(session)
    operation = session.get(OperationRecord, operation_id)
    if operation is None or operation.workspace_id != context.workspace_id:
        raise OperationNotFoundError(operation_id)
    return operation


def transition_operation(
    session: Session,
    operation_id: str,
    to_state: OperationState,
    reason: str | None,
    actor: str,
    *,
    result_ref: OperationResultRef | None = None,
) -> OperationRecord:
    """Validate and apply one state transition, appending its history event.

    The projection (``operation.state``) and the append-only event log are
    written in the same flush and can never disagree: an invalid transition
    raises :class:`InvalidOperationTransitionError` before either is touched.

    A transition into a terminal state also records the Operation's Usage
    (``operations`` and ``processing_seconds``, Issue #8) in the same
    transaction; if that write fails, the transition fails with it. Every
    caller -- v1 aggregation, legacy commands and the idle-time folds --
    settles an Operation through here, so this is the one place it is
    counted.
    """
    operation = get_operation(session, operation_id)
    from_state = OperationState(operation.state)
    if not validate_transition(from_state, to_state):
        raise InvalidOperationTransitionError(from_state.value, to_state.value)

    now = utcnow()
    operation.state = to_state.value
    operation.updated_at = now
    if result_ref is not None:
        operation.result_resource_type = result_ref.resource_type
        operation.result_resource_id = result_ref.resource_id

    session.add(
        OperationStateEvent(
            id=new_id("ose"),
            workspace_id=operation.workspace_id,
            operation_id=operation.id,
            from_state=from_state.value,
            to_state=to_state.value,
            reason=reason,
            actor=actor,
            created_at=now,
        )
    )
    session.flush()
    if to_state in TERMINAL_STATES:
        # Imported here: the entitlements package imports this module.
        from ..entitlements.operation_usage import record_operation_settled

        record_operation_settled(session, current_context(session), operation, settled_at=now)
    return operation


# ---------------------------------------------------------------------------
# Job -> Operation aggregation
# ---------------------------------------------------------------------------

#: A Job in one of these states will never move again on its own.
#: ``retry_wait`` is deliberately absent: a Job waiting to be retried is
#: still in flight, and folding its Operation to a terminal state there
#: would report an outcome the command has not reached yet.
JOB_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "uncertain"})


#: Only Operations admitted under this contract version (or newer) carry the
#: complete Job set that makes aggregation meaningful. Legacy ``queued``
#: Operations predate it, own their transitions through their own domain
#: handlers, and must not be folded by the generic aggregator.
AGGREGATED_ADMISSION_CONTRACT_VERSION = 1


def derive_operation_outcome(
    job_states: Iterable[str],
) -> OperationState | None:
    """Fold sibling Job states into the Operation outcome they imply.

    ``None`` means "not yet": there are no Jobs, or at least one is still
    in flight (including ``retry_wait``), so the Operation stays where it is.

    Precedence is deliberate. ``uncertain`` wins over everything, including a
    sibling success: spec §16.8's rule that an ambiguous paid call is neither
    a success nor a failure applies to the whole command, because reporting
    ``partially_succeeded`` would tell the user the remainder is settled when
    a reconciliation pass may still resolve it either way.
    """
    states = list(job_states)
    if not states:
        return None
    if any(state not in JOB_TERMINAL_STATES for state in states):
        return None
    if any(state == "uncertain" for state in states):
        return OperationState.UNCERTAIN
    succeeded = sum(1 for state in states if state == "succeeded")
    if succeeded == len(states):
        return OperationState.SUCCEEDED
    if succeeded:
        return OperationState.PARTIALLY_SUCCEEDED
    if all(state == "cancelled" for state in states):
        return OperationState.CANCELLED
    return OperationState.FAILED


def _lock_operation_for_aggregation(
    session: Session, workspace_id: str, operation_id: str
) -> OperationRecord | None:
    """Take the Operation row lock the sibling-Job read must happen under.

    ``SELECT ... FOR UPDATE`` serializes the two Workers that finish the last
    two Jobs of a command at the same moment. Without it both read the other's
    Job as still running, both decline to fold, and the Operation stays
    ``running`` forever with every Job terminal. The lock is taken *before*
    the sibling states are read so the second holder re-reads them under
    PostgreSQL's fresh READ COMMITTED snapshot and sees the first one's commit.
    SQLite ignores the clause and serializes writers itself.
    """
    return session.scalar(
        select(OperationRecord)
        .where(
            OperationRecord.id == operation_id,
            OperationRecord.workspace_id == workspace_id,
        )
        .with_for_update()
    )


def aggregate_operation_from_jobs(
    session: Session,
    operation_id: str | None,
    *,
    actor: str,
    context: TenantContext | None = None,
    reason: str | None = None,
) -> OperationRecord | None:
    """Advance one Operation to the state its sibling Jobs now imply.

    Returns the Operation when this call moved it, otherwise ``None``. Safe
    to call after *every* Job transition, from any number of Workers, and
    repeatedly for the same Operation:

    * Operations below :data:`AGGREGATED_ADMISSION_CONTRACT_VERSION` are left
      alone -- their domain handlers own their transitions.
    * An Operation already in a terminal state is never transitioned again,
      so no duplicate terminal event can be appended.
    * A derived state equal to the current one is a no-op for the same reason.
    """
    if not operation_id:
        return None
    context = context or current_context(session)
    # Sessions here are ``autoflush=False``: a caller that just assigned
    # ``job.state`` on an ORM instance has not sent it to the database yet,
    # and the sibling read below would decide the outcome from the Job's
    # *previous* state. Flush first so the fold sees the caller's own work.
    session.flush()
    operation = _lock_operation_for_aggregation(
        session, context.workspace_id, operation_id
    )
    if operation is None:
        return None
    if operation.admission_contract_version < AGGREGATED_ADMISSION_CONTRACT_VERSION:
        return None
    current = OperationState(operation.state)
    if current in TERMINAL_STATES:
        return None

    job_states: Sequence[str] = session.scalars(
        select(JobRecord.state).where(
            JobRecord.workspace_id == context.workspace_id,
            JobRecord.operation_id == operation_id,
        )
    ).all()
    outcome = derive_operation_outcome(job_states)
    if outcome is None or outcome == current:
        return None
    if current not in (OperationState.ADMITTED,) and not validate_transition(
        current, outcome
    ):
        # Aggregation is called from inside a Job's own terminal transaction.
        # Raising here would roll that Job back over a state-machine gap it
        # has no part in, so an unreachable pair is reported and skipped;
        # the Job's own state stays the durable record either way.
        logger.warning(
            "operation_aggregation_transition_rejected",
            extra={
                "workspace_id": context.workspace_id,
                "operation_id": operation_id,
                "from_state": current.value,
                "to_state": outcome.value,
            },
        )
        return None

    if current is OperationState.ADMITTED and not validate_transition(current, outcome):
        # A success implies a dispatch that the ``admitted -> running``
        # promotion below records explicitly. Reaching here means the Worker
        # crashed between ``jobs.mark_running`` and that promotion, which are
        # separate durable transactions; the Jobs are the evidence the work
        # ran, so the history gets both events rather than an impossible jump.
        transition_operation(
            session,
            operation_id,
            OperationState.RUNNING,
            reason="dispatch observed while aggregating sibling Jobs",
            actor=actor,
        )
        current = OperationState.RUNNING

    return transition_operation(
        session,
        operation_id,
        outcome,
        reason=reason or f"aggregated from {len(job_states)} sibling Job(s)",
        actor=actor,
    )


def start_operation_for_job(
    session: Session,
    operation_id: str | None,
    *,
    actor: str,
    context: TenantContext | None = None,
) -> OperationRecord | None:
    """Promote an ``admitted`` Operation to ``running`` on its first Job.

    Idempotent: the second and every later Job of the same command find the
    Operation already ``running`` and change nothing.
    """
    if not operation_id:
        return None
    context = context or current_context(session)
    operation = _lock_operation_for_aggregation(
        session, context.workspace_id, operation_id
    )
    if operation is None:
        return None
    if operation.admission_contract_version < AGGREGATED_ADMISSION_CONTRACT_VERSION:
        return None
    if OperationState(operation.state) is not OperationState.ADMITTED:
        return None
    return transition_operation(
        session,
        operation_id,
        OperationState.RUNNING,
        reason="first admitted Job started",
        actor=actor,
    )


def result_ref_of(operation: OperationRecord) -> OperationResultRef | None:
    """Build the public result reference from the flattened DB columns."""
    if operation.result_resource_type is None or operation.result_resource_id is None:
        return None
    return OperationResultRef(
        resource_type=operation.result_resource_type,
        resource_id=operation.result_resource_id,
    )
