"""Atomic Submission: Preview -> Confirmation -> Snapshot -> Run -> RunItems (Issue #21 Task 2).

One call, one transaction: a confirmed Preview is consumed exactly once and
turned into a GenerationRun with its RunItems, each enqueued as a durable
job for Task 3's worker to execute. Idempotency is layered on top of the
platform's own Operation idempotency protocol
(``app.platform.idempotency.create_or_replay_operation``) rather than
re-derived here: a replayed ``idempotency_key`` returns the Operation (and,
via its ``result_resource_id``, the GenerationRun) an earlier identical
request already created, without re-running -- and therefore without
tripping -- the "preview already submitted" guard in
``app.generation.preview.check_preview_valid``.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import GenerationRunRecord, PlanRecord, new_id, utcnow
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    GenerationRunCommandRecord,
    ResolutionPreviewConfirmationRecord,
    RunItemRecord,
)
from app.entitlements.storage_quota import admit_generation
from app.generation.preview import PreviewError, check_preview_valid
from app.generation.draft import get_draft
from app.generation.selection import check_frozen_selection
from app.generation.snapshot import insert_or_reuse_snapshot
from app.platform.idempotency import create_or_replay_operation
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, transition_operation
from app.platform.tenancy import TenantContext

#: Single-combination fallback used when a recipe's ``combination_strategy``
#: has not yet been expanded into ``selected_combinations`` (Key decision #2).
_DEFAULT_COMBINATIONS = [{"fingerprint": "default"}]


from .submission import SubmissionError


def submit_generation(
    session: Session,
    ctx: TenantContext,
    *,
    preview_id: str,
    preview_hash: str,
    expected_draft_revision: int,
    idempotency_key: str,
) -> dict:
    """Confirm ``preview_id`` and atomically create its GenerationRun.

    Replaying the same ``idempotency_key`` (same actor, same request
    fingerprint) returns the original ``{operation_id, generation_run_id}``
    without re-executing the submission -- the Operation created on the
    first call is the record of "this request already happened".
    """
    fingerprint_hash = _submission_fingerprint(preview_id, preview_hash, expected_draft_revision)

    operation = create_or_replay_operation(
        session,
        idempotency_key=idempotency_key,
        command_type="generation.submit",
        fingerprint_hash=fingerprint_hash,
    )
    if operation.result_resource_id is not None:
        # A replay: the first call already ran the submission below and
        # recorded its GenerationRun as this Operation's result.
        return {
            "operation_id": operation.id,
            "generation_run_id": operation.result_resource_id,
        }

    # Contract §11 step 3, after a replay was answered: no new generation
    # once the Workspace stores its storage.bytes.max (#158).
    admit_generation(session, ctx)

    try:
        preview = check_preview_valid(session, ctx, preview_id, preview_hash)
    except PreviewError as exc:
        raise SubmissionError(exc.code, exc.detail) from exc

    if preview.draft_revision != expected_draft_revision:
        raise SubmissionError(
            "stale_draft_revision",
            f"Expected draft revision {expected_draft_revision}, "
            f"preview was frozen against {preview.draft_revision}",
        )

    draft = get_draft(session, ctx, preview.generation_draft_id)
    if draft.revision != expected_draft_revision or draft.state == "archived":
        raise SubmissionError("stale_draft_revision", "草稿已变化，请重新预览。")

    recipe = dict(preview.snapshot_json.get("recipe") or {})
    check_frozen_selection(session, ctx, recipe)
    quote = (recipe.get("selection") or {}).get("cost_quote") or {}
    maximum_amount = Decimal(quote.get("maximum_amount", "0"))
    product_id = preview.snapshot_json.get("product_id", "")

    now = utcnow()
    confirmation = ResolutionPreviewConfirmationRecord(
        id=new_id("rpc"),
        workspace_id=ctx.workspace_id,
        preview_id=preview.id,
        preview_hash=preview.preview_hash,
        actor_id=ctx.initiator_id or "unknown",
        confirmed_at=now,
        request_fingerprint=fingerprint_hash,
        quoted_max_provider_amount=maximum_amount,
        quoted_max_budget_amount=maximum_amount,
        quoted_currency="USD",
        pricing_version=quote.get("pricing_version", ""),
    )
    session.add(confirmation)
    session.flush()

    snapshot = insert_or_reuse_snapshot(
        session,
        ctx,
        canonical_json=recipe,
        execution_mode=preview.execution_mode,
    )

    # A GenerationRun is FK-anchored to a Plan (pre-existing schema, spanning
    # the legacy library-driven flow too). Atomic Submission mints a minimal
    # one-off Plan per Run rather than requiring the caller to pre-plan.
    plan = PlanRecord(
        id=new_id("plan"),
        workspace_id=ctx.workspace_id,
        product_id=product_id,
        mode="generation",
        cap=0,
        total_full=0,
        snapshot_hash=snapshot.recipe_hash,
        created_at=now,
    )
    session.add(plan)
    session.flush()

    format_config = recipe.get("format") or {}
    run = GenerationRunRecord(
        id=new_id("gr"),
        workspace_id=ctx.workspace_id,
        product_id=product_id,
        plan_id=plan.id,
        provider=recipe.get("provider", "mock"),
        model=recipe.get("model", "mock-v1"),
        aspect_ratio=format_config.get("aspect_ratio", "1:1"),
        image_size=format_config.get("image_size", "1024x1024"),
        variants=format_config.get("variants", 1),
        use_ref=bool(recipe.get("selection", {}).get("product_references") or recipe.get("selection", {}).get("source_image")
                     or recipe.get("selection", {}).get("brand_logo")),
        status="running",
        total=0,
        done=0,
        failed=0,
        est_cost=maximum_amount,
        currency="USD",
        revision=1,
        recipe_snapshot_id=snapshot.id,
        creation_preview_confirmation_id=confirmation.id,
        creation_operation_id=operation.id,
        submission_kind="confirmed_preview",
        execution_mode=preview.execution_mode,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    session.flush()

    items = _create_run_items(session, ctx, run, snapshot)
    run.total = len(items)
    session.flush()

    command = GenerationRunCommandRecord(
        id=new_id("grc"),
        workspace_id=ctx.workspace_id,
        generation_run_id=run.id,
        operation_id=operation.id,
        command_kind="submit",
        expected_run_revision=run.revision,
        preview_confirmation_id=confirmation.id,
        actor_id=ctx.initiator_id or "unknown",
        created_at=now,
    )
    session.add(command)
    session.flush()

    job_ids_by_call_key = {}
    for item in items:
        if item.state != "pending":
            continue
        job = enqueue_in_session(
            session,
            context=ctx,
            job_type="generation.run_item",
            resource_type="run_item",
            resource_id=item.id,
            payload={"run_item_id": item.id, "snapshot_id": snapshot.id},
            idempotency_key=f"gen_item:{item.id}",
            # This handler first checks the committed RunItem/Attempt fence:
            # pending resumes, running becomes uncertain, succeeded is a
            # no-op. Requeueing the Job never blindly repeats a paid call.
            safe_to_retry_after_crash=True if "selection" in recipe else None,
        )
        job.operation_id = operation.id
        job_ids_by_call_key[item.id] = job.id

    if preview.execution_mode == "byok":
        from .paid_execution import reserve_run
        reserve_run(session, ctx, run, items, job_ids_by_call_key, quote, confirmation.id)
    elif preview.execution_mode == "mock":
        # Reservations are request-owned, including the zero-payable identity
        # required by a controlled provider attempt. Worker has no INSERT grant.
        from .worker import _get_or_create_reservation_id
        for item in items:
            if item.id in job_ids_by_call_key:
                _get_or_create_reservation_id(session, ctx, item, operation.id, job_ids_by_call_key[item.id])

    preview.submitted_run_id = run.id
    preview.submitted_at = now
    session.flush()

    transition_operation(
        session,
        operation.id,
        OperationState.RUNNING,
        reason="generation run created and run items enqueued",
        actor=ctx.initiator_id or ctx.executor,
        result_ref=OperationResultRef(resource_type="generation_run", resource_id=run.id),
    )

    return {"operation_id": operation.id, "generation_run_id": run.id}


def _create_run_items(
    session: Session,
    ctx: TenantContext,
    run: GenerationRunRecord,
    snapshot: GenerationRecipeSnapshotRecord,
) -> list[RunItemRecord]:
    combinations = snapshot.canonical_json.get("selected_combinations") or _DEFAULT_COMBINATIONS

    items: list[RunItemRecord] = []
    for ordinal, combo in enumerate(combinations):
        item = RunItemRecord(
            id=new_id("ri"),
            workspace_id=ctx.workspace_id,
            generation_run_id=run.id,
            ordinal=ordinal,
            combination_identity_json=combo,
            combination_fingerprint=combo.get("fingerprint", f"fp_{ordinal}"),
            semantic_fingerprint=hashlib.sha256(f"{snapshot.recipe_hash}:{run.id}:{ordinal}".encode()).hexdigest(),
            variant_index=combo.get("variant_index", 0),
            execution_mode=run.execution_mode,
            intent_kind="quality_feedback" if (snapshot.canonical_json.get("selection") or {}).get("source_image") else "new_generation",
            source_asset_version_id=((snapshot.canonical_json.get("selection") or {}).get("source_image") or {}).get("asset_version_id"),
            state="pending",
            revision=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(item)
        items.append(item)

    session.flush()
    return items


def _submission_fingerprint(preview_id: str, preview_hash: str, expected_draft_revision: int) -> str:
    raw = json.dumps(
        {
            "preview_id": preview_id,
            "preview_hash": preview_hash,
            "expected_draft_revision": expected_draft_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
