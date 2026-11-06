"""Recovery ResolutionPreview creation (Issue #51 Task 5).

``app.generation.preview.create_preview`` freezes a *Draft* into a Preview
of ``purpose="initial_generation"``. Retrying failed RunItems or resuming
paused ones needs the analogous freeze for an existing *Run*: a
``ResolutionPreviewRecord`` of ``purpose="run_recovery"`` (the schema
already reserves this -- see ``ck_resolution_preview_purpose`` and
``ck_resolution_preview_recovery_cmd_kind`` on
``app.adapters.persistence.models.ResolutionPreviewRecord``) naming exactly
which RunItems a subsequent ``run_commands.retry_failed_items`` /
``resume_paused_items`` call would target, and at what cost.

This module resolves and validates the target RunItem selection against the
Run's *current* state, freezes it into an immutable preview row, and prices
the additional work (:func:`create_recovery_preview`). Consumption --
validate -> lock -> confirm -> reserve -> enqueue -> mark
(:func:`consume_recovery_preview`) -- happens atomically in one transaction,
mirroring ``app.generation.submission.submit_generation``'s Preview ->
Confirm -> Run shape.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    GenerationRunRecord,
    JobRecord,
    ResolutionPreviewRecord,
    new_id,
    utcnow,
)
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    GenerationRunCommandRecord,
    ResolutionPreviewConfirmationRecord,
    RunItemRecord,
)
from app.generation.run_queries import get_run
from app.generation.spend import SpendReservationError, is_charged, reserve_run_item_spend
from app.platform.idempotency import create_or_replay_intent, create_or_replay_operation
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, transition_operation
from app.platform.tenancy import TenantContext

#: How long a confirmed-but-unconsumed recovery preview stays eligible.
RECOVERY_PREVIEW_TTL = timedelta(minutes=30)

#: Bumped whenever the shape of the hashed recovery payload changes, so old
#: previews never silently compare equal to a differently-shaped new one.
CANONICALIZATION_VERSION = "rv1"

#: RunItem states a given recovery command is allowed to target.
_ELIGIBLE_STATES = {
    "retry_failed_items": {"failed"},
    "resume_paused_items": {"paused"},
}


class RecoveryPreviewError(ValueError):
    """Carries a machine-readable ``code`` alongside the human ``detail``."""

    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


def create_recovery_preview(
    session: Session,
    ctx: TenantContext,
    run_id: str,
    *,
    command: str,
    selection_kind: str,
    selection_item_ids: list[str],
    expected_run_revision: int,
    idempotency_key: str,
) -> dict:
    """Resolve ``selection_kind``/``selection_item_ids`` against ``run_id`` at
    ``expected_run_revision`` and freeze it into a ``purpose="run_recovery"``
    :class:`ResolutionPreviewRecord`.

    Raises :class:`RecoveryPreviewError` with:

    * ``stale_run_revision`` -- the Run has moved on since the caller read it.
    * ``invalid_command`` -- ``command`` is not a recognised recovery command.
    * ``invalid_selection_kind`` -- ``selection_kind`` is not recognised.
    * ``target_not_found`` -- an explicit ``item_ids`` selection names RunItems
      that do not exist in this Workspace/Run.
    * ``invalid_target_state`` -- a targeted RunItem is not in the state the
      command requires (e.g. retrying a succeeded item).
    * ``not_resumable`` -- a targeted paused RunItem is not
      ``same_snapshot_resumable``.

    Idempotent: replaying the same ``idempotency_key`` (same actor, same
    request fingerprint) returns the original preview rather than creating a
    second one; the same key against a different request raises
    ``IdempotencyKeyReusedError`` (409) from ``app.platform.idempotency``.
    """
    run = get_run(session, ctx, run_id)
    if run.revision != expected_run_revision:
        raise RecoveryPreviewError(
            "stale_run_revision",
            f"Expected revision {expected_run_revision}, run is at {run.revision}",
        )

    if command not in _ELIGIBLE_STATES:
        raise RecoveryPreviewError(
            "invalid_command",
            f"command must be one of {sorted(_ELIGIBLE_STATES)}",
        )

    targets = _resolve_targets(session, ctx, run, command, selection_kind, selection_item_ids)
    frozen_targets = _freeze_targets(targets)
    cost_amount, cost_currency = _compute_additional_cost(run, targets)

    fingerprint = _recovery_fingerprint(
        run_id, command, frozen_targets, expected_run_revision,
    )

    def _create(s: Session) -> tuple[str, str, dict]:
        preview = _persist_preview(
            s, ctx, run, command=command,
            frozen_targets=frozen_targets,
            cost_amount=cost_amount,
            cost_currency=cost_currency,
            expected_run_revision=expected_run_revision,
        )
        result = _to_response(preview)
        return "resolution_preview", preview.id, result

    def _replay(resource_type: str, resource_id: str, s: Session) -> dict:
        preview = s.get(ResolutionPreviewRecord, resource_id)
        if preview is None:
            raise RecoveryPreviewError("preview_not_found", resource_id)
        return _to_response(preview)

    return create_or_replay_intent(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.create_recovery_preview",
        fingerprint_hash=fingerprint,
        create_fn=_create,
        replay_fn=_replay,
    )


def _resolve_targets(
    session: Session, ctx: TenantContext,
    run: GenerationRunRecord, command: str,
    selection_kind: str, selection_item_ids: list[str],
) -> list[RunItemRecord]:
    eligible_states = _ELIGIBLE_STATES[command]

    if selection_kind == "item_ids":
        items = list(session.scalars(
            select(RunItemRecord).where(
                RunItemRecord.workspace_id == ctx.workspace_id,
                RunItemRecord.generation_run_id == run.id,
                RunItemRecord.id.in_(selection_item_ids),
            )
        ).all())
        if len(items) != len(set(selection_item_ids)):
            found = {i.id for i in items}
            missing = set(selection_item_ids) - found
            raise RecoveryPreviewError(
                "target_not_found",
                f"RunItems not found: {sorted(missing)}",
            )
        invalid = [i for i in items if i.state not in eligible_states]
        if invalid:
            raise RecoveryPreviewError(
                "invalid_target_state",
                f"Items not in {sorted(eligible_states)}: "
                f"{[(i.id, i.state) for i in invalid]}",
            )
        if command == "resume_paused_items":
            non_resumable = [i for i in items if not i.same_snapshot_resumable]
            if non_resumable:
                raise RecoveryPreviewError(
                    "not_resumable",
                    f"Items not same_snapshot_resumable: "
                    f"{[i.id for i in non_resumable]}",
                )
        return items

    elif selection_kind == "all_eligible_failed":
        return list(session.scalars(
            select(RunItemRecord).where(
                RunItemRecord.workspace_id == ctx.workspace_id,
                RunItemRecord.generation_run_id == run.id,
                RunItemRecord.state == "failed",
            )
        ).all())

    elif selection_kind == "all_resumable_paused":
        return list(session.scalars(
            select(RunItemRecord).where(
                RunItemRecord.workspace_id == ctx.workspace_id,
                RunItemRecord.generation_run_id == run.id,
                RunItemRecord.state == "paused",
                RunItemRecord.same_snapshot_resumable == True,  # noqa: E712
            )
        ).all())

    else:
        raise RecoveryPreviewError(
            "invalid_selection_kind", f"Unknown selection kind: {selection_kind}",
        )


def _freeze_targets(targets: list[RunItemRecord]) -> list[dict]:
    return [
        {"id": t.id, "state": t.state, "revision": t.revision}
        for t in targets
    ]


def _compute_additional_cost(
    run: GenerationRunRecord, targets: list[RunItemRecord],
) -> tuple[Decimal, str]:
    """Proportional share of the Run's estimated cost the targeted items
    would consume on retry/resume.

    Mock execution is always zero cost (spec: Controlled Mock never carries
    real evidence or spend). For real modes (byok/sandbox), each RunItem is
    assumed to cost an equal share of the Run's total estimated cost --
    ``run.est_cost / run.total`` per item -- since RunItems within a Run do
    not carry their own individual cost estimate.
    """
    currency = run.currency or "USD"
    if not targets:
        return Decimal("0"), currency
    if run.execution_mode == "mock":
        return Decimal("0"), currency
    if run.total <= 0:
        return Decimal("0"), currency
    per_item = run.est_cost / run.total
    return per_item * len(targets), currency


def _recovery_fingerprint(
    run_id: str, command: str, frozen_targets: list[dict],
    expected_run_revision: int,
) -> str:
    canonical = json.dumps(
        {
            "run_id": run_id,
            "command": command,
            "targets": sorted(frozen_targets, key=lambda t: t["id"]),
            "expected_run_revision": expected_run_revision,
            "canonicalization_version": CANONICALIZATION_VERSION,
        },
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persist_preview(
    session: Session,
    ctx: TenantContext,
    run: GenerationRunRecord,
    *,
    command: str,
    frozen_targets: list[dict],
    cost_amount: Decimal,
    cost_currency: str,
    expected_run_revision: int,
) -> ResolutionPreviewRecord:
    now = utcnow()

    snapshot_content = {
        "run_id": run.id,
        "command": command,
        "expected_run_revision": expected_run_revision,
        "items": frozen_targets,
        "cost": {"amount": str(cost_amount), "currency": cost_currency},
    }
    preview_hash = _canonical_hash({
        "run_id": run.id,
        "command": command,
        "targets": sorted(frozen_targets, key=lambda t: t["id"]),
        "expected_run_revision": expected_run_revision,
        "cost_amount": str(cost_amount),
        "cost_currency": cost_currency,
        "canonicalization_version": CANONICALIZATION_VERSION,
    })
    recipe_input_hash = _canonical_hash({
        "run_id": run.id,
        "recipe_snapshot_id": run.recipe_snapshot_id,
        "canonicalization_version": CANONICALIZATION_VERSION,
    })

    preview = ResolutionPreviewRecord(
        id=new_id("rpv"),
        workspace_id=ctx.workspace_id,
        generation_draft_id=None,
        draft_revision=None,
        snapshot_json=snapshot_content,
        preview_hash=preview_hash,
        recipe_input_hash=recipe_input_hash,
        execution_mode=run.execution_mode,
        valid_until=now + RECOVERY_PREVIEW_TTL,
        created_by=ctx.initiator_id or "unknown",
        created_at=now,
        purpose="run_recovery",
        recovery_run_id=run.id,
        recovery_command_kind=command,
        recovery_target_json={"items": frozen_targets},
        recovery_max_additional_cost=cost_amount,
        recovery_cost_currency=cost_currency,
    )
    session.add(preview)
    session.flush()
    return preview


def _to_response(preview: ResolutionPreviewRecord) -> dict:
    snapshot = preview.snapshot_json or {}
    frozen_targets = snapshot.get("items", [])
    target_ids = [t["id"] for t in frozen_targets]
    return {
        "purpose": "run_recovery",
        "id": preview.id,
        "preview_hash": preview.preview_hash,
        "command": preview.recovery_command_kind,
        "targets": target_ids,
        "estimated_additional_cost": {
            "amount": str(preview.recovery_max_additional_cost
                          if preview.recovery_max_additional_cost is not None else 0),
            "currency": preview.recovery_cost_currency or "USD",
        },
        "valid_until": preview.valid_until.isoformat()
            if preview.valid_until else None,
        "run_id": preview.recovery_run_id or "",
        "expected_run_revision": snapshot.get("expected_run_revision", 0),
        "allowed_actions": [],
    }


def _cas_bump_revision(
    session: Session, ctx: TenantContext, run_id: str, expected_revision: int,
) -> None:
    """CAS-bump the Run's ``revision`` (inlined -- importing
    ``app.generation.run_commands`` here would be circular, since that module
    delegates its own ``retry_failed_items``/``resume_paused_items`` to
    :func:`consume_recovery_preview`)."""
    result = session.execute(
        update(GenerationRunRecord)
        .where(
            GenerationRunRecord.id == run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
            GenerationRunRecord.revision == expected_revision,
        )
        .values(revision=expected_revision + 1, updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise RecoveryPreviewError(
            "revision_conflict",
            f"Run {run_id} expected revision {expected_revision}, was concurrently modified",
        )
    session.flush()


def consume_recovery_preview(
    session: Session,
    ctx: TenantContext,
    *,
    run_id: str,
    command: str,
    preview_id: str,
    preview_hash: str,
    expected_run_revision: int,
    idempotency_key: str,
) -> dict:
    """Atomic: validate -> lock -> confirm -> reserve -> enqueue -> mark consumed.

    Mirrors ``app.generation.submission.submit_generation``'s idempotency
    shape: :func:`~app.platform.idempotency.create_or_replay_operation` is
    called *before* anything else, including the "preview already consumed"
    guard inside :func:`_check_recovery_preview_valid` -- a replay of the
    same ``idempotency_key`` must return the original ``{operation_id,
    run_id}`` rather than tripping that guard on the second call.
    """
    fingerprint = hashlib.sha256(
        f"recovery:{run_id}:{preview_id}:{preview_hash}".encode("utf-8")
    ).hexdigest()

    # Idempotency replay BEFORE the "already consumed" check below.
    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type=f"generation.{command}",
        fingerprint_hash=fingerprint,
    )
    if operation.result_resource_id is not None:
        return {"operation_id": operation.id, "run_id": operation.result_resource_id}

    # Validate preview identity, purpose, ownership, freshness, and that it
    # has not already been consumed.
    preview = _check_recovery_preview_valid(session, ctx, preview_id, preview_hash, run_id, command)

    frozen = preview.recovery_target_json.get("items", [])
    if not frozen:
        raise RecoveryPreviewError("empty_preview", "Preview has no targets")

    # Lock: verify each frozen target's state+revision still matches what
    # was frozen at preview-creation time.
    _validate_frozen_targets(session, ctx, frozen, command)

    run = get_run(session, ctx, run_id)
    snapshot = preview.snapshot_json or {}
    frozen_revision = snapshot.get("expected_run_revision")
    if frozen_revision is not None and run.revision != frozen_revision:
        raise RecoveryPreviewError(
            "stale_run_revision",
            f"Run revision changed since preview: expected {frozen_revision}, "
            f"now {run.revision}",
        )

    # CAS-bump the Run's revision -- this consumption is itself a mutation of
    # Run state, same as every other command in run_commands.py.
    _cas_bump_revision(session, ctx, run_id, expected_run_revision)

    # Confirm: append-only confirmation evidence, mirroring submit_generation.
    now = utcnow()
    confirmation = ResolutionPreviewConfirmationRecord(
        id=new_id("rpc"),
        workspace_id=ctx.workspace_id,
        preview_id=preview.id,
        preview_hash=preview.preview_hash,
        actor_id=ctx.initiator_id or "unknown",
        confirmed_at=now,
        request_fingerprint=fingerprint,
        quoted_max_provider_amount=preview.recovery_max_additional_cost or Decimal("0"),
        quoted_max_budget_amount=preview.recovery_max_additional_cost or Decimal("0"),
        quoted_currency=preview.recovery_cost_currency or "USD",
    )
    session.add(confirmation)
    session.flush()

    target_ids = [t["id"] for t in frozen]

    # Record command: append-only evidence of this recovery consumption.
    cmd = GenerationRunCommandRecord(
        id=new_id("grc"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run_id,
        operation_id=operation.id,
        command_kind=command,
        target_selection_json={"preview_id": preview.id, "targets": target_ids},
        expected_run_revision=expected_run_revision,
        preview_confirmation_id=confirmation.id,
        actor_id=ctx.initiator_id or "unknown",
        authorization_epoch=ctx.authorization_epoch,
        created_at=now,
    )
    session.add(cmd)
    session.flush()

    # Reserve + enqueue: reopen each targeted RunItem as queued and dispatch
    # its retry/resume job.
    jobs_by_item: list[tuple[RunItemRecord, JobRecord]] = []
    for tid in target_ids:
        item = session.get(RunItemRecord, tid)
        if item is None:
            continue
        result = session.execute(
            update(RunItemRecord)
            .where(
                RunItemRecord.id == tid,
                RunItemRecord.workspace_id == ctx.workspace_id,
                RunItemRecord.revision == item.revision,
            )
            .values(
                state="queued",
                same_snapshot_resumable=False,
                error_summary="",
                revision=item.revision + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise RecoveryPreviewError(
                "target_state_changed", f"Item {tid} was modified concurrently"
            )
        session.flush()

        job = enqueue_in_session(
            session,
            context=ctx,
            job_type="generation.run_item",
            resource_type="run_item",
            resource_id=tid,
            payload={"run_item_id": tid, "snapshot_id": run.recipe_snapshot_id},
            idempotency_key=f"recovery:{command}:{tid}:{preview.id}",
        )
        job.operation_id = operation.id
        jobs_by_item.append((item, job))

    if is_charged(run.execution_mode):
        # A retried/resumed item is a fresh paid call: reserve it under this
        # recovery command's Operation, through the same guardrail check the
        # original submission passed.
        try:
            reserve_run_item_spend(
                session, ctx,
                run=run,
                jobs_by_item=jobs_by_item,
                operation_id=operation.id,
                format_config=_run_format_config(session, run),
            )
        except SpendReservationError as exc:
            raise RecoveryPreviewError(exc.code, exc.detail, params=exc.params) from exc

    # Mark consumed: a preview can only ever be consumed once.
    preview.submitted_run_id = run_id
    preview.submitted_at = now
    session.flush()

    transition_operation(
        session,
        operation.id,
        OperationState.RUNNING,
        reason=f"{command} enqueued against recovery preview {preview.id}",
        actor=ctx.initiator_id or ctx.executor,
        result_ref=OperationResultRef(resource_type="generation_run", resource_id=run_id),
    )

    return {"operation_id": operation.id, "run_id": run_id}


def _run_format_config(session: Session, run: GenerationRunRecord) -> dict:
    """The Run's frozen ``format`` (the recipe snapshot is the source of truth)."""
    snapshot = session.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
    recipe = (snapshot.canonical_json or {}) if snapshot is not None else {}
    return dict(recipe.get("format") or {})


def _check_recovery_preview_valid(
    session: Session, ctx: TenantContext,
    preview_id: str, expected_hash: str,
    run_id: str, command: str,
) -> ResolutionPreviewRecord:
    preview = session.execute(
        select(ResolutionPreviewRecord).where(
            ResolutionPreviewRecord.id == preview_id,
            ResolutionPreviewRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if preview is None:
        raise RecoveryPreviewError("preview_not_found", f"Preview {preview_id} not found")
    if preview.preview_hash != expected_hash:
        raise RecoveryPreviewError("preview_hash_mismatch", "Preview hash does not match")
    if preview.purpose != "run_recovery":
        raise RecoveryPreviewError("wrong_purpose", f"Preview purpose is {preview.purpose}")
    if preview.recovery_run_id != run_id:
        raise RecoveryPreviewError("wrong_run", "Preview belongs to a different run")
    if preview.recovery_command_kind != command:
        raise RecoveryPreviewError("wrong_command", "Preview command mismatch")
    if preview.invalidated_at is not None:
        raise RecoveryPreviewError("preview_invalidated", preview.invalidation_reason)

    valid_until = preview.valid_until
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)
    if valid_until < utcnow():
        raise RecoveryPreviewError("preview_expired", "Preview has expired")
    if preview.submitted_run_id is not None:
        raise RecoveryPreviewError("preview_already_submitted", "Preview already consumed")
    return preview


def _validate_frozen_targets(
    session: Session, ctx: TenantContext,
    frozen: list[dict], command: str,
) -> None:
    for target in frozen:
        item = session.get(RunItemRecord, target["id"])
        if item is None or item.workspace_id != ctx.workspace_id:
            raise RecoveryPreviewError(
                "target_not_found", f"RunItem {target['id']} not found",
            )
        if item.state != target["state"] or item.revision != target["revision"]:
            raise RecoveryPreviewError(
                "target_state_changed",
                f"RunItem {target['id']} state/revision changed since preview "
                f"(was {target['state']}@{target['revision']}, "
                f"now {item.state}@{item.revision})",
            )
