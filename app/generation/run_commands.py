"""Generation Run mutation commands (Issue #27 Task 3 / spec §5).

Four commands: ``cancel``, ``retry_failed_items``, ``resume_paused_items``,
``reconcile`` -- the same ``command_kind`` vocabulary
``GenerationRunCommandRecord`` already CHECK-constrains
(``ck_generation_run_command_kind``). Each command:

1. CAS-bumps the Run's ``revision`` with a real conditional
   ``UPDATE ... WHERE revision = expected`` (the same pattern
   ``app.generation.draft.update_draft`` uses), so two concurrent commands
   against the same Run can never silently clobber each other.
2. Appends an immutable ``GenerationRunCommandRecord`` -- append-only
   evidence of the command, not a mutable command queue.
3. Layers idempotency on the platform's Operation protocol
   (``app.platform.idempotency.create_or_replay_operation``), matching
   ``app.generation.submission.submit_generation``.

``retry_failed_items``/``resume_paused_items`` take a ``preview_id``/
``preview_hash`` pair confirming a target selection, mirroring Atomic
Submission's Preview -> Confirm shape (spec: recovery is "a second, smaller
Atomic Submission"). Both delegate their entire body to
``app.generation.recovery_preview.consume_recovery_preview`` (Issue #51 Task
6), which validates the preview against a live ``ResolutionPreviewRecord`` of
``purpose="run_recovery"``, CAS-bumps the Run, records the
``GenerationRunCommandRecord``, reopens the targeted RunItems, and enqueues
their retry/resume jobs -- all inside one atomic idempotency-first sequence.
``cancel`` and ``reconcile`` remain implemented directly in this module since
they carry no RunItem re-enqueue step.

Every command's Operation is settled by the domain, never by the generic Job
aggregator (they are ``admission_contract_version`` 0):
``app.generation.run_operations`` closes ``cancel``/``reconcile`` in their own
transaction and lets the Run projection close ``submit``/retry/resume.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import GenerationRunRecord, new_id, utcnow
from app.generation.models import GenerationRunCommandRecord
from app.generation.run_operations import (
    settle_run_command_operations,
    settle_synchronous_command_operation,
)
from app.generation.run_queries import get_run
from app.platform.idempotency import create_or_replay_operation
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext

__all__ = [
    "RunCommandError",
    "cancel_run",
    "retry_failed_items",
    "resume_paused_items",
    "reconcile_run",
]

#: Statuses from which a Run may still be interrupted. Matches
#: ``run_queries.run_actions``' "cancel" eligibility check.
_CANCELLABLE_STATUSES = ("pending", "running", "qc_pending")


class RunCommandError(ValueError):
    """Carries a machine-readable ``code`` alongside the human ``detail``."""

    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


def _cas_bump_revision(
    session: Session, ctx: TenantContext, run_id: str, expected_revision: int, *, status: str | None = None,
) -> None:
    values: dict = {"revision": expected_revision + 1, "updated_at": utcnow()}
    if status is not None:
        values["status"] = status
    result = session.execute(
        update(GenerationRunRecord)
        .where(
            GenerationRunRecord.id == run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
            GenerationRunRecord.revision == expected_revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise RunCommandError(
            "revision_conflict",
            f"Run {run_id} expected revision {expected_revision}, was concurrently modified",
        )
    session.flush()


def _record_command(
    session: Session, ctx: TenantContext, run_id: str, *,
    operation_id: str, command_kind: str, expected_run_revision: int,
    target_selection: dict | None = None,
) -> GenerationRunCommandRecord:
    cmd = GenerationRunCommandRecord(
        id=new_id("grc"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run_id,
        operation_id=operation_id,
        command_kind=command_kind,
        target_selection_json=target_selection or {},
        expected_run_revision=expected_run_revision,
        actor_id=ctx.initiator_id or ctx.executor,
        authorization_epoch=ctx.authorization_epoch,
        created_at=utcnow(),
    )
    session.add(cmd)
    session.flush()
    return cmd


def cancel_run(
    session: Session, ctx: TenantContext, *,
    run_id: str, expected_revision: int, idempotency_key: str | None = None,
) -> dict:
    run = get_run(session, ctx, run_id)
    if run.status not in _CANCELLABLE_STATUSES:
        raise RunCommandError(
            "run_not_cancellable", f"Run status {run.status!r} cannot be cancelled",
        )

    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.cancel",
        fingerprint_hash=canonical_hash({"command": "cancel", "run_id": run_id, "revision": expected_revision}),
        actor=ctx.initiator_id or ctx.executor,
    )
    _cas_bump_revision(session, ctx, run_id, expected_revision, status="cancelled")
    _record_command(
        session, ctx, run_id,
        operation_id=operation.id, command_kind="cancel", expected_run_revision=expected_revision,
    )
    actor = ctx.initiator_id or ctx.executor
    # The Run is ``cancelled`` as of this transaction, without passing through
    # ``_sync_run_state``: close the open submit/retry/resume Operations here
    # so nothing keeps reporting "running" work under a cancelled Run.
    settle_run_command_operations(session, ctx, run_id, "cancelled", actor=actor)
    # The cancel command's whole effect is the status write above.
    settle_synchronous_command_operation(
        session, operation.id, actor=actor, reason=f"generation run {run_id} cancelled",
    )
    return {"operation_id": operation.id, "run_id": run_id}


def retry_failed_items(
    session: Session, ctx: TenantContext, *,
    run_id: str, expected_revision: int,
    preview_id: str, preview_hash: str,
    idempotency_key: str | None = None,
) -> dict:
    """Delegates to ``app.generation.recovery_preview.consume_recovery_preview``.

    The atomic validate -> lock -> confirm -> reserve -> enqueue -> mark-consumed
    sequence lives there (Issue #51 Task 6); this wrapper only fills in the
    ``command`` discriminator and a default idempotency key so existing
    callers (routes, tests) keep the same signature.
    """
    from app.generation.recovery_preview import consume_recovery_preview

    return consume_recovery_preview(
        session, ctx,
        run_id=run_id,
        command="retry_failed_items",
        preview_id=preview_id,
        preview_hash=preview_hash,
        expected_run_revision=expected_revision,
        idempotency_key=idempotency_key or f"retry:{run_id}:{preview_id}",
    )


def resume_paused_items(
    session: Session, ctx: TenantContext, *,
    run_id: str, expected_revision: int,
    preview_id: str, preview_hash: str,
    idempotency_key: str | None = None,
) -> dict:
    """Delegates to ``app.generation.recovery_preview.consume_recovery_preview``.

    See :func:`retry_failed_items`.
    """
    from app.generation.recovery_preview import consume_recovery_preview

    return consume_recovery_preview(
        session, ctx,
        run_id=run_id,
        command="resume_paused_items",
        preview_id=preview_id,
        preview_hash=preview_hash,
        expected_run_revision=expected_revision,
        idempotency_key=idempotency_key or f"resume:{run_id}:{preview_id}",
    )


def reconcile_run(
    session: Session, ctx: TenantContext, *,
    run_id: str, expected_revision: int,
    uncertain_targets: list[str],
    operator_note: str = "",
    idempotency_key: str | None = None,
) -> dict:
    get_run(session, ctx, run_id)
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.reconcile",
        fingerprint_hash=canonical_hash({"command": "reconcile", "run_id": run_id,
            "targets": sorted(uncertain_targets), "operator_note": operator_note}),
        actor=ctx.initiator_id or ctx.executor,
    )
    recorded = session.scalar(select(GenerationRunCommandRecord.id).where(
        GenerationRunCommandRecord.workspace_id == ctx.workspace_id,
        GenerationRunCommandRecord.generation_run_id == run_id,
        GenerationRunCommandRecord.operation_id == operation.id,
    ))
    if recorded:
        return {"operation_id": operation.id, "run_id": run_id}
    _cas_bump_revision(session, ctx, run_id, expected_revision)
    _record_command(
        session, ctx, run_id,
        operation_id=operation.id, command_kind="reconcile",
        expected_run_revision=expected_revision,
        target_selection={"uncertain_targets": uncertain_targets, "operator_note": operator_note},
    )
    # The reconcile command is the append-only record of the operator's
    # decision written just above; it is complete once that record exists.
    settle_synchronous_command_operation(
        session,
        operation.id,
        actor=ctx.initiator_id or ctx.executor,
        reason=f"reconcile command recorded for generation run {run_id}",
    )
    return {"operation_id": operation.id, "run_id": run_id}
