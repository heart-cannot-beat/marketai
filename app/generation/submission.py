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

from app.adapters.persistence.models import ResolutionPreviewRecord, GenerationRunRecord, PlanRecord, new_id, utcnow
from app.generation.creative_recipe import reference_count_of
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    GenerationRunCommandRecord,
    ResolutionPreviewConfirmationRecord,
    RunItemRecord,
)
from app.generation.preview import (
    PreviewError,
    check_preview_valid,
    validate_variants_per_combination,
)
from app.generation.onboarding_facts import emit_onboarding_fact
from app.generation.snapshot import insert_or_reuse_snapshot
from app.generation.spend import (
    SpendReservationError,
    is_charged,
    quote_recipe,
    reserve_run_item_spend,
)
from app.platform.idempotency import create_or_replay_operation
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, transition_operation
from app.platform.tenancy import TenantContext

#: Single-combination fallback used when a recipe's ``combination_strategy``
#: has not yet been expanded into ``selected_combinations`` (Key decision #2).
_DEFAULT_COMBINATIONS = [{"fingerprint": "default"}]


class SubmissionError(ValueError):
    """Carries a machine-readable ``code`` alongside the human ``detail``."""

    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


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
    preview = session.get(ResolutionPreviewRecord, preview_id)
    if preview and preview.workspace_id == ctx.workspace_id and "selection" in (preview.snapshot_json.get("recipe") or {}):
        from .selected_submission import submit_generation as submit_selected
        return submit_selected(session, ctx, preview_id=preview_id, preview_hash=preview_hash, expected_draft_revision=expected_draft_revision, idempotency_key=idempotency_key)

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

    try:
        preview = check_preview_valid(session, ctx, preview_id, preview_hash)
    except PreviewError as exc:
        raise SubmissionError(exc.code, exc.detail, params=exc.params) from exc

    if preview.draft_revision != expected_draft_revision:
        raise SubmissionError(
            "stale_draft_revision",
            f"Expected draft revision {expected_draft_revision}, "
            f"preview was frozen against {preview.draft_revision}",
        )

    recipe = dict(preview.snapshot_json.get("recipe") or {})
    product_id = preview.snapshot_json.get("product_id", "")

    try:
        # Re-checked against the deployed registry (Issue #157): a model limit
        # lowered between preview and submit must refuse here, before any
        # RunItem, job or reservation exists.
        validate_variants_per_combination(
            recipe.get("format") or {},
            adapter_key=str(recipe.get("adapter_key") or ""),
            model=str(recipe.get("model") or ""),
        )
    except PreviewError as exc:
        raise SubmissionError(exc.code, exc.detail, params=exc.params) from exc

    if is_charged(preview.execution_mode):
        # The key the preview was admitted with must still be bound: a
        # credential revoked between preview and submit is not a Run.
        from app.generation.credential_binding import resolve_generation_credential

        if not resolve_generation_credential(session, ctx, recipe.get("provider", "")).get("active"):
            raise SubmissionError(
                "credential_required",
                "BYOK mode requires an active eligible CredentialVersion",
            )

    now = utcnow()
    # The confirmation freezes what the operator agreed to pay. It is written
    # once (the API role never updates confirmations), so the quote is taken
    # from the frozen recipe here, before any row exists; the reservation
    # made below prices the very same calls and lands on the same total.
    quoted = quote_recipe(recipe, preview.execution_mode)
    confirmation = ResolutionPreviewConfirmationRecord(
        id=new_id("rpc"),
        workspace_id=ctx.workspace_id,
        preview_id=preview.id,
        preview_hash=preview.preview_hash,
        actor_id=ctx.initiator_id or "unknown",
        confirmed_at=now,
        request_fingerprint=fingerprint_hash,
        quoted_max_provider_amount=quoted.amount,
        quoted_max_budget_amount=quoted.amount,
        quoted_currency=quoted.currency,
    )
    session.add(confirmation)
    session.flush()

    snapshot = insert_or_reuse_snapshot(
        session,
        ctx,
        canonical_json=recipe,
        execution_mode=preview.execution_mode,
    )
    if snapshot.execution_mode == "mock":
        emit_onboarding_fact(
            session,
            ctx,
            fact_type="generation_recipe_snapshot.mock_created",
            source_resource_id=snapshot.id,
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
        variants=int(format_config.get("variants_per_combination", 1)),
        # Issue #167: the recipe's frozen reference decision, not a constant.
        use_ref=reference_count_of(recipe) > 0,
        status="running",
        total=0,
        done=0,
        failed=0,
        est_cost=Decimal("0"),
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

    jobs_by_item = []
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
        )
        job.operation_id = operation.id
        jobs_by_item.append((item, job))

    if is_charged(run.execution_mode):
        # Real spend is reserved here, in the admission transaction, so a
        # guardrail breach refuses the whole Run before anything is enqueued
        # for dispatch (the exception rolls back Run, items and jobs alike).
        try:
            reserved = reserve_run_item_spend(
                session, ctx,
                run=run,
                jobs_by_item=jobs_by_item,
                operation_id=operation.id,
                format_config=format_config,
            )
        except SpendReservationError as exc:
            raise SubmissionError(exc.code, exc.detail, params=exc.params) from exc
        run.est_cost = reserved.total_budget_amount
        run.currency = reserved.currency
        session.flush()

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
    format_config = snapshot.canonical_json.get("format") or {}
    variants_per_combination = int(format_config.get("variants_per_combination", 1))

    items: list[RunItemRecord] = []
    for selected_ordinal, combo in enumerate(combinations):
        combination_fingerprint = combo.get("fingerprint", f"fp_{selected_ordinal}")
        for variant_index in range(variants_per_combination):
            ordinal = len(items)
            item = RunItemRecord(
                id=new_id("ri"),
                workspace_id=ctx.workspace_id,
                generation_run_id=run.id,
                ordinal=ordinal,
                combination_identity_json=combo,
                combination_fingerprint=combination_fingerprint,
                stable_dimension_signature=combination_fingerprint,
                variant_index=variant_index,
                semantic_fingerprint=_semantic_fingerprint(
                    snapshot.recipe_hash,
                    run.id,
                    selected_ordinal,
                    variant_index,
                ),
                execution_mode=run.execution_mode,
                state="pending",
                revision=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(item)
            items.append(item)

    session.flush()
    return items


def _semantic_fingerprint(
    recipe_hash: str,
    run_id: str,
    selected_ordinal: int,
    variant_index: int,
) -> str:
    """Return the fixed-width identity for one selected-combination variant.

    The fingerprint is the RunItem's *paid intent*: the frozen recipe, the
    submission it belongs to, and its place in the combination/variant
    layout. The Run id is the per-submission nonce the entitlements spec
    requires of every generation intent ("an intentional regeneration
    command requires a distinct intent/seed nonce and receives a distinct
    semantic key"): the recipe snapshot is content-addressed and shared
    between Runs (``app.generation.snapshot``), and the Worker derives the
    ``spend_reservations`` / ``provider_call_attempts`` semantic keys from
    this value -- both unique per Workspace -- so without the nonce every
    item of a second Run of the same recipe collided with the first Run's
    reservations at dispatch. Replaying the same submission never reaches
    here (``create_or_replay_operation`` returns the original Run), so the
    same intent still maps to one identity.
    """
    raw = f"{recipe_hash}:{run_id}:{selected_ordinal}:{variant_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
