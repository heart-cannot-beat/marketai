"""ResolutionPreview creation + submission-time validity check (Issue #21 Task 2).

Freezes a Draft plus its resolved execution-mode policy
(``app.generation.execution_policy.resolve_preview``) into an immutable
``ResolutionPreviewRecord`` the operator confirms before Atomic Submission.

The ``recipe_input_hash`` is deliberately computed over only the *recipe*
portion of the snapshot -- provider, model, execution mode, combination
strategy, format, exclusions, and the resolved adapter/cost/scenario
evidence. ``product_id`` and ``selectors`` are product-specific, not part of
the reusable recipe, so two Drafts that only differ by which Product they
target still resolve to the same recipe identity (this is what lets
``insert_or_reuse_snapshot`` in ``app.generation.snapshot`` share one
``GenerationRecipeSnapshotRecord`` across runs against different Products).
Since Issue #167 the recipe also freezes its provider-neutral ``creative``
inputs (``app.generation.creative_recipe``): what the Product *says*
(name, copy, confirmed must-match items, bound reference images) and the
test-plan directions. Two Products with identical content still share a
recipe; Products that differ in what they ask a provider to make do not.

Combination planning (full product, coverage sampling, template constraints
and manual exclusion rules) lives in ``app.generation.planner`` (Issue
#157); this module freezes the planner's result, validates the variant count
against the resolved model's limit, and writes rules the planner could not
apply back to the Draft as ``exclusions.invalid``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    DimensionConceptKeyRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    ResolutionPreviewRecord,
    new_id,
    utcnow,
)
from app.generation.capabilities import max_variants_per_combination
from app.generation.creative_recipe import RECIPE_KEY as CREATIVE_RECIPE_KEY
from app.generation.creative_recipe import freeze_creative_inputs
from app.generation.draft import get_draft
from app.generation.execution_policy import PreviewRejection, resolve_preview
from app.generation.experiment_conclusions import project_contextual_conclusion_for_context
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.onboarding_facts import emit_onboarding_fact
from app.generation.planner import PlannerError, plan_combinations
from app.generation.vlm_qc import (
    QC_RECIPE_KEY,
    capability_decision as quality_check_decision,
    charged_cost_per_image as quality_check_cost_per_image,
    resolve_qc_policy,
)
from app.platform.tenancy import TenantContext

#: How long a confirmed-but-unsubmitted preview stays eligible for submission.
PREVIEW_TTL = timedelta(minutes=30)

#: Bumped whenever the shape of the hashed "recipe" payload changes, so old
#: previews never silently compare equal to a differently-shaped new one.
#: v2 (Issue #157): ``combination_strategy`` and ``exclusions`` are frozen in
#: their normalised form and ``selected_combinations`` reflect template
#: constraints and coverage sampling.
#: v3 (Issue #167): the recipe carries the frozen ``creative`` block (product
#: brief, confirmed must-match, reference images and their use decision,
#: test-plan directions).
#: v4 (Issue #169): the recipe carries the frozen ``quality_check`` policy
#: (VLM method, model, references compared, per-image maximum) and the
#: creative block may carry ``qc_feedback`` from a regeneration.
CANONICALIZATION_VERSION = "v4"


class PreviewError(ValueError):
    """Carries a machine-readable ``code`` alongside the human ``detail``."""

    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


def create_preview(
    session: Session,
    ctx: TenantContext,
    draft_id: str,
    draft_revision: int,
    *,
    adapter_registry: dict | None = None,
    credential_evidence: dict | None = None,
    experiment_context: str | None = None,
    skip_losers: bool | None = None,
) -> ResolutionPreviewRecord:
    """Resolve ``draft_id`` at ``draft_revision`` and freeze it into a Preview.

    Raises :class:`PreviewError` with code ``stale_draft_revision`` if the
    Draft has moved on, or with the rejection code returned by
    :func:`app.generation.execution_policy.resolve_preview` if the Draft's
    execution mode cannot currently be admitted (e.g. missing BYOK
    credential, unavailable sandbox adapter).

    ``experiment_context`` (Issue #49 Task 8) is the opaque
    ``context_id``/``experiment_context`` value
    :func:`app.generation.experiment_conclusions.project_contextual_conclusion_for_context`
    resolves conclusions against (spec §0.7's ``experiment_id``). When it is
    supplied, ``skip_losers`` defaults to ``True``: every stable dimension
    signature whose newest produced AssetVersion in this Draft's Product
    carries a confirmed Loser conclusion in that context is dropped from the
    preview's included items before cost/count projections are computed, and
    the projection reports how much was skipped (``skipped_signature_count``,
    ``skipped_call_count``, ``estimated_cost_saving``). Passing
    ``skip_losers=False`` explicitly opts out and includes every signature
    regardless of conclusion. With no ``experiment_context``, Loser exclusion
    never runs (``skip_losers`` defaults to ``False``) -- there is no context
    to resolve conclusions against.
    """
    draft = get_draft(session, ctx, draft_id)
    if "prompt" in (draft.selectors_json or {}) and "variants" in (draft.format_json or {}):
        from .selected_preview import create_preview as create_selected_preview
        return create_selected_preview(session, ctx, draft_id, draft_revision, adapter_registry=adapter_registry)
    if draft.revision != draft_revision:
        raise PreviewError(
            "stale_draft_revision",
            f"Expected revision {draft_revision}, draft is at {draft.revision}",
        )

    registry = adapter_registry if adapter_registry is not None else _default_adapter_registry()

    if credential_evidence is None and draft.execution_mode == "byok":
        # The Workspace's own key is the admission evidence for BYOK; look it
        # up here so every caller (router, tests, re-previews) admits the
        # same way instead of each threading a binding lookup through.
        from app.generation.credential_binding import resolve_generation_credential

        credential_evidence = resolve_generation_credential(session, ctx, draft.provider)

    # The provider-neutral creative inputs (Issue #167), frozen here so the
    # reference-image decision, the quote and the Worker's call agree.
    creative = freeze_creative_inputs(
        session, ctx,
        product_id=draft.product_id,
        combination_strategy=draft.combination_strategy_json,
    )

    resolution = resolve_preview(
        session,
        draft_mode=draft.execution_mode,
        provider=draft.provider,
        model=draft.model,
        adapter_registry=registry,
        credential_evidence=credential_evidence,
        format_config=draft.format_json or {},
        reference_images_available=len(creative["reference_images"]),
    )
    if isinstance(resolution, PreviewRejection):
        raise PreviewError(resolution.code, resolution.detail)

    # Issue #169: how every image of this Run is quality-checked, frozen with
    # the recipe so the quote, the reservation and the Worker's call agree.
    quality_check = resolve_qc_policy(
        execution_mode=resolution.effective_mode,
        reference_images_available=len(creative["reference_images"]),
        openai_key_bound=_openai_key_bound(session, ctx) if resolution.effective_mode == "byok" else False,
    )
    qc_feedback = _qc_feedback_of(draft.selectors_json)
    if qc_feedback:
        creative["qc_feedback"] = qc_feedback

    variants_per_combination = validate_variants_per_combination(
        draft.format_json or {},
        adapter_key=resolution.adapter_key,
        model=draft.model,
        adapter_registry=registry,
    )

    effective_skip_losers = skip_losers if skip_losers is not None else bool(experiment_context)
    resolved_selectors = _materialize_dimension_template_concepts(
        session, ctx, dict(draft.selectors_json or {})
    )
    ui_projections = _compute_ui_projections(
        draft,
        resolution,
        selectors=resolved_selectors,
        session=session,
        ctx=ctx,
        experiment_context=experiment_context,
        skip_losers=effective_skip_losers,
        variants_per_combination=variants_per_combination,
        quality_check=quality_check,
    )
    selected_combinations = ui_projections.pop("_selected_combinations")
    strategy_recipe = ui_projections.pop("_strategy_recipe")
    applied_rules = ui_projections.pop("_applied_rules")
    invalid_rules = ui_projections.pop("_invalid_rules")
    _validate_governed_concepts(session, ctx, selected_combinations)
    _write_back_invalid_exclusions(draft, invalid_rules)

    recipe_core = _recipe_core(draft, resolution)
    recipe_core[CREATIVE_RECIPE_KEY] = {
        **creative,
        "reference_policy": dict(resolution.reference_decision or {}),
    }
    recipe_core[QC_RECIPE_KEY] = quality_check
    recipe_core["combination_strategy"] = strategy_recipe
    recipe_core["exclusions"] = {"rules": applied_rules}
    recipe_core["selected_combinations"] = selected_combinations
    snapshot_content = {
        "product_id": draft.product_id,
        "selectors": resolved_selectors,
        "recipe": recipe_core,
    }
    recipe_input_hash = _canonical_hash(
        {"recipe": recipe_core, "canonicalization_version": CANONICALIZATION_VERSION}
    )
    preview_hash = _canonical_hash(
        {
            "recipe_input_hash": recipe_input_hash,
            "mode": resolution.effective_mode,
            "adapter": resolution.adapter_key,
            "capabilities": resolution.capabilities,
            "cost_policy": resolution.cost_policy,
            "scenario": resolution.scenario_evidence,
        }
    )

    snapshot_content["ui_projections"] = ui_projections

    now = utcnow()
    preview = ResolutionPreviewRecord(
        id=new_id("rpv"),
        workspace_id=ctx.workspace_id,
        generation_draft_id=draft_id,
        draft_revision=draft_revision,
        snapshot_json=snapshot_content,
        preview_hash=preview_hash,
        recipe_input_hash=recipe_input_hash,
        execution_mode=resolution.effective_mode,
        valid_until=now + PREVIEW_TTL,
        created_by=ctx.initiator_id or "unknown",
        created_at=now,
        purpose="initial_generation",
    )
    session.add(preview)
    session.flush()
    product_exists = session.scalar(
        select(ProductRecord.id).where(
            ProductRecord.workspace_id == ctx.workspace_id,
            ProductRecord.id == draft.product_id,
        )
    )
    reference_ids = {
        reference_id
        for item in ui_projections.get("items", [])
        for reference_id in item.get("reference_asset_ids", [])
    }
    resolved_reference_ids = set()
    if reference_ids:
        resolved_reference_ids = set(session.scalars(
            select(AssetRecord.id).where(
                AssetRecord.workspace_id == ctx.workspace_id,
                AssetRecord.id.in_(reference_ids),
            )
        ))
    if not product_exists or resolved_reference_ids != reference_ids:
        raise PreviewError(
            "unresolved_preview_references",
            "Preview must resolve its Product and every reference asset",
        )
    emit_onboarding_fact(
        session,
        ctx,
        fact_type="resolution_preview.validated",
        source_resource_id=preview.id,
    )
    return preview


def check_preview_valid(
    session: Session,
    ctx: TenantContext,
    preview_id: str,
    expected_hash: str,
) -> ResolutionPreviewRecord:
    """Submission-time gate: exists, hash matches, not invalidated/expired/consumed."""
    preview = session.execute(
        select(ResolutionPreviewRecord).where(
            ResolutionPreviewRecord.id == preview_id,
            ResolutionPreviewRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if preview is None:
        raise PreviewError("preview_not_found", "Preview does not exist")
    if preview.preview_hash != expected_hash:
        raise PreviewError("preview_hash_mismatch", "Preview hash does not match")
    if preview.invalidated_at is not None:
        raise PreviewError("preview_invalidated", preview.invalidation_reason)
    if _as_aware(preview.valid_until) < utcnow():
        raise PreviewError("preview_expired", "Preview has expired")
    if preview.submitted_run_id is not None:
        raise PreviewError("preview_already_submitted", "Preview already consumed")
    return preview


def validate_variants_per_combination(
    format_config: dict,
    *,
    adapter_key: str,
    model: str,
    adapter_registry: dict | None = None,
) -> int:
    """Return the Draft's variant count after checking it against the model.

    ``format_config.variants_per_combination`` defaults to 1. It must be a
    positive integer (``variants_invalid``) no larger than the resolved
    adapter/model's advertised ``max_variants_per_combination``
    (``variants_exceed_model_limit``) -- the same limit
    ``GET /generation/capabilities`` shows the page. Preview and Atomic
    Submission both call this so a limit lowered between the two is caught
    before anything is reserved.
    """
    raw = format_config.get("variants_per_combination", 1)
    if raw is None:
        raw = 1
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise PreviewError(
            "variants_invalid",
            "format_config.variants_per_combination must be a positive integer",
        )
    registry = adapter_registry if adapter_registry is not None else _default_adapter_registry()
    limit = max_variants_per_combination(registry, adapter_key, model)
    if limit is not None and raw > limit:
        raise PreviewError(
            "variants_exceed_model_limit",
            f"{model} allows at most {limit} variants per combination (requested {raw})",
        )
    return raw


def _write_back_invalid_exclusions(draft, invalid_rules: list[dict]) -> None:
    """Persist the rules this preview could not apply as ``exclusions.invalid``.

    A server-owned annotation, not a user edit: it does not advance the
    Draft's ``revision`` (the preview was frozen against that revision and
    submission compares the two), it only replaces the previous annotation so
    the page's "flagged invalid by the server" list always reflects the
    latest preview.
    """
    current = dict(draft.exclusions_json or {})
    if invalid_rules:
        current["invalid"] = invalid_rules
    elif "invalid" in current:
        current.pop("invalid")
    else:
        return
    draft.exclusions_json = current


def _compute_ui_projections(
    draft,
    resolution,
    *,
    selectors: dict | None = None,
    session: Session | None = None,
    ctx: TenantContext | None = None,
    experiment_context: str | None = None,
    skip_losers: bool = False,
    variants_per_combination: int | None = None,
    quality_check: dict | None = None,
) -> dict:
    """Compute UI-facing projection fields from draft + resolution data.

    Stored in ``snapshot_json["ui_projections"]`` so the router can project
    them without re-deriving. The hash is computed before this is called,
    so these fields do not affect preview identity -- Loser exclusion
    (Issue #49 Task 8) is a per-request/per-context view over the same
    frozen recipe, not a different recipe.

    Combination enumeration, template constraints, manual exclusion rules
    and coverage sampling are :func:`app.generation.planner.plan_combinations`
    (Issue #157). Every included combination carries its own
    ``stable_dimension_signature`` -- the "key=value" dimension fingerprint
    the planner assigns each combo, which is the dimension-only identity that
    survives regeneration rounds (see ``app.generation.regeneration``'s module
    docstring: the *producing* RunItem's own ``stable_dimension_signature``
    column carries this same value once the combo is actually run). Loser
    exclusion looks up, for each such signature, the newest AssetVersion
    already produced with it in this Draft's Product and asks Task 6's
    :func:`app.generation.experiment_conclusions.project_contextual_conclusion_for_context`
    whether that AssetVersion's conclusion in ``experiment_context`` is
    ``"loser"`` -- a signature with no prior AssetVersion, or whose newest
    conclusion is Winner/Learning/unset, is never excluded.
    """
    selectors = selectors if selectors is not None else (draft.selectors_json or {})
    strategy = draft.combination_strategy_json or {}
    exclusions = draft.exclusions_json or {}
    format_config = draft.format_json or {}
    capabilities = resolution.capabilities or {}
    cost_policy = resolution.cost_policy or {}

    try:
        plan = plan_combinations(selectors, strategy, exclusions)
    except PlannerError as exc:
        raise PreviewError(exc.code, exc.detail) from exc
    excluded_items = plan.constraint_dropped + plan.rule_excluded
    included_items = plan.selected

    loser_excluded_items: list[dict] = []
    skipped_signature_count = 0
    if skip_losers and experiment_context and session is not None and ctx is not None:
        loser_signatures = _resolve_loser_signatures(
            session, ctx, draft.product_id, experiment_context
        )
        if loser_signatures:
            still_included = []
            for combo in included_items:
                if combo.get("fingerprint") in loser_signatures:
                    combo["exclusion_reason"] = "loser_conclusion"
                    loser_excluded_items.append(combo)
                else:
                    still_included.append(combo)
            included_items = still_included
            skipped_signature_count = len(
                {c["fingerprint"] for c in loser_excluded_items}
            )

    if not included_items:
        # Submission falls back to a phantom "default" RunItem when the frozen
        # selection is empty; refuse here so the operator sees why instead.
        raise PreviewError(
            "empty_preview",
            "Every combination was excluded by template constraints, exclusion "
            "rules or the Loser filter; nothing is left to generate",
        )

    combination_count = len(included_items)
    if variants_per_combination is None:
        variants_per_combination = int(format_config.get("variants_per_combination") or 1)
    call_count = combination_count * variants_per_combination
    skipped_call_count = len(loser_excluded_items) * variants_per_combination

    cost_amount = "0"
    cost_currency = "USD"
    per_call = Decimal("0")
    if cost_policy.get("true_payable") == "zero":
        cost_amount = "0"
    elif cost_policy.get("true_payable") == "provider_quoted":
        per_call = Decimal(str(cost_policy.get("estimated_per_call") or "0"))
        # Issue #169: each image's quality check is a paid call of its own,
        # reserved next to the image call, so it is part of Maximum cost.
        per_call += quality_check_cost_per_image(quality_check)
        cost_amount = _money(call_count * per_call)
        cost_currency = cost_policy.get("currency", "USD")

    maximum_cost = {"amount": cost_amount, "currency": cost_currency}

    saving_amount = "0"
    if per_call and skipped_call_count:
        saving_amount = _money(skipped_call_count * per_call)
    estimated_cost_saving = {"amount": saving_amount, "currency": cost_currency}

    items = []
    for ordinal, combo in enumerate(included_items):
        items.append({
            "ordinal": ordinal,
            "dimension_tags": combo.get("dimensions", {}),
            "reference_asset_ids": combo.get("reference_asset_ids", []),
            "excluded": False,
            "exclusion_reason": None,
            "stable_dimension_signature": combo.get("fingerprint", ""),
        })
    for combo in excluded_items + loser_excluded_items:
        items.append({
            "ordinal": len(items),
            "dimension_tags": combo.get("dimensions", {}),
            "reference_asset_ids": combo.get("reference_asset_ids", []),
            "excluded": True,
            "exclusion_reason": combo.get("exclusion_reason", "excluded_by_rule"),
            "stable_dimension_signature": combo.get("fingerprint", ""),
        })

    capability_decisions = [
        {"capability": cap, "supported": bool(supported), "detail": None}
        for cap, supported in capabilities.items()
    ]
    reference_decision = getattr(resolution, "reference_decision", None) or {}
    if reference_decision:
        # Issue #167: whether this Run sends the product's reference images
        # is a capability decision of the resolved model, shown with the cost.
        capability_decisions.append({
            "capability": "reference_images",
            "supported": bool(reference_decision.get("use")),
            "detail": reference_decision.get("detail"),
        })
    if quality_check:
        capability_decisions.append(quality_check_decision(quality_check))

    all_excluded = excluded_items + loser_excluded_items
    exclusion_reasons = list({c.get("exclusion_reason", "") for c in all_excluded if c.get("exclusion_reason")})
    exclusion_summary = {
        "excluded_count": len(all_excluded),
        "reasons": sorted(exclusion_reasons),
    }

    final_prompt = _assemble_prompt(selectors)

    return {
        "combination_count": combination_count,
        "call_count": call_count,
        "maximum_cost": maximum_cost,
        "final_prompt": final_prompt,
        "items": items,
        "capability_decisions": capability_decisions,
        "exclusion_summary": exclusion_summary,
        "skipped_signature_count": skipped_signature_count,
        "skipped_call_count": skipped_call_count,
        "estimated_cost_saving": estimated_cost_saving,
        # Issue #157: what the planner actually did, so the page never has to
        # guess whether its strategy/constraints/rules took effect.
        "strategy_applied": plan.strategy_applied(),
        "combinations_dropped_by_constraints": len(plan.constraint_dropped),
        # SkipReasons (Library contract §9.3) for the audit trail; the item
        # list above only carries the ``template_constraint`` reason code.
        "constraint_skips": [
            {
                "dimension_tags": combo.get("dimensions", {}),
                "stable_dimension_signature": combo.get("fingerprint", ""),
                "violations": list(combo.get("constraint_violations") or []),
            }
            for combo in plan.constraint_dropped
        ],
        # Internal hand-offs to create_preview.  They are removed before the
        # UI projection is persisted; the selection is frozen inside the
        # recipe that creates RunItems, so the shown count and the executed
        # combinations stay identical, including Loser exclusions.
        "_selected_combinations": included_items,
        "_strategy_recipe": plan.strategy.as_recipe(),
        "_applied_rules": plan.applied_rules,
        "_invalid_rules": plan.invalid_rules,
    }


def _openai_key_bound(session: Session, ctx: TenantContext) -> bool:
    """Whether the Workspace's ``llm_chat`` key (OpenAI) is bound for a VLM check."""
    from app.generation.credential_binding import resolve_generation_credential

    return bool(resolve_generation_credential(session, ctx, "openai").get("active"))


def _qc_feedback_of(selectors: dict | None) -> list[dict]:
    """The QC feedback a regeneration draft carries (Issue #169), normalised."""
    raw = (selectors or {}).get("qc_feedback")
    feedback: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        hint = str(entry.get("hint") or "").strip()
        if not hint:
            continue
        feedback.append({
            "hint": hint[:300],
            "reason": str(entry.get("reason") or "")[:500],
            "rule_id": str(entry.get("rule_id") or "")[:64],
            "source_asset_version_id": entry.get("source_asset_version_id"),
            "qc_attempt_id": entry.get("qc_attempt_id"),
        })
    return feedback[:6]


def _resolve_loser_signatures(
    session: Session, ctx: TenantContext, product_id: str, experiment_context: str
) -> set[str]:
    """Stable dimension signatures excluded from this preview (Issue #49
    Task 8): every non-empty ``RunItemRecord.stable_dimension_signature``
    produced for AssetVersions in ``product_id`` whose *newest* such
    AssetVersion (``created_at DESC, id DESC`` -- the same "newest wins"
    rule ``app.generation.regeneration.create_review_summary_regeneration``
    applies within a signature group) carries a confirmed Loser conclusion
    in ``experiment_context``.

    Scoped to ``product_id`` rather than the whole Workspace: a fresh
    combination's dimension signature can only plausibly replace an
    AssetVersion already produced for the same Product.
    """
    rows = session.execute(
        select(
            RunItemRecord.stable_dimension_signature,
            AssetVersionRecord.id,
            AssetVersionRecord.created_at,
        )
        .select_from(RunItemRecord)
        .join(
            RunItemAssetVersionBindingRecord,
            RunItemAssetVersionBindingRecord.run_item_id == RunItemRecord.id,
        )
        .join(
            AssetVersionRecord,
            AssetVersionRecord.id == RunItemAssetVersionBindingRecord.asset_version_id,
        )
        .join(AssetRecord, AssetRecord.id == AssetVersionRecord.asset_id)
        .where(
            RunItemRecord.workspace_id == ctx.workspace_id,
            RunItemAssetVersionBindingRecord.workspace_id == ctx.workspace_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
            AssetRecord.workspace_id == ctx.workspace_id,
            AssetRecord.product_id == product_id,
            RunItemAssetVersionBindingRecord.kind == "produced",
            RunItemRecord.stable_dimension_signature != "",
        )
    ).all()

    newest: dict[str, tuple[str, object]] = {}
    for signature, version_id, created_at in rows:
        current = newest.get(signature)
        if current is None or (created_at, version_id) > (current[1], current[0]):
            newest[signature] = (version_id, created_at)

    losers: set[str] = set()
    for signature, (version_id, _created_at) in newest.items():
        verdict = project_contextual_conclusion_for_context(
            session, ctx, version_id, experiment_context
        )
        if verdict == "loser":
            losers.add(signature)
    return losers


def _materialize_dimension_template_concepts(
    session: Session,
    ctx: TenantContext,
    selectors: dict,
) -> dict:
    """Resolve selected templates into stable, governed combination inputs.

    The UI selects Dimension Template *entries*.  Preview is the freeze point
    that resolves each entry to its exact effective version.  Stable template,
    dimension, and value IDs become the concept identity; labels and prompt
    fragments remain presentation/content only and are never used to guess a
    key.  Locking each entry serializes deterministic key creation for
    concurrent previews.

    Each resolved version's published ``constraints`` (``requires`` /
    ``excludes`` / ``max_occurrence``) are frozen next to the dimensions as
    ``dimension_template_constraints`` so the planner applies exactly the
    constraints that version carried (Issue #157).
    """

    template_ids = selectors.get("dimension_template_ids") or []
    if not template_ids:
        return selectors
    if not isinstance(template_ids, list) or any(
        not isinstance(value, str) or not value for value in template_ids
    ):
        raise PreviewError(
            "dimension_template_selection_invalid",
            "selectors.dimension_template_ids must contain non-empty entry IDs",
        )
    if selectors.get("dimensions"):
        raise PreviewError(
            "dimension_sources_conflict",
            "choose dimension_template_ids or explicit dimensions, not both",
        )

    dimensions: dict[str, list[dict]] = {}
    resolved_versions: list[str] = []
    template_constraints: list[dict] = []
    for entry_id in template_ids:
        entry = session.scalar(
            select(LibraryEntryRecord)
            .where(
                LibraryEntryRecord.id == entry_id,
                LibraryEntryRecord.workspace_id == ctx.workspace_id,
                LibraryEntryRecord.entry_type == "dimension_template",
                LibraryEntryRecord.lifecycle == "active",
            )
            .with_for_update()
        )
        if entry is None or not entry.current_version_id:
            raise PreviewError(
                "dimension_template_not_effective",
                f"Dimension Template {entry_id!r} has no effective version",
            )
        version = session.scalar(
            select(LibraryVersionRecord).where(
                LibraryVersionRecord.id == entry.current_version_id,
                LibraryVersionRecord.workspace_id == ctx.workspace_id,
                LibraryVersionRecord.entry_id == entry.id,
                LibraryVersionRecord.entry_type == "dimension_template",
                LibraryVersionRecord.state == "effective",
                LibraryVersionRecord.eligibility == "eligible",
            )
        )
        if version is None:
            raise PreviewError(
                "dimension_template_not_effective",
                f"Dimension Template {entry_id!r} has no eligible effective version",
            )
        content = version.content_json
        if not isinstance(content, dict):
            raise PreviewError(
                "dimension_template_invalid",
                f"Dimension TemplateVersion {version.id!r} content is not an object",
            )
        raw_mappings = content.get("concept_key_mappings") or []
        if not isinstance(raw_mappings, list):
            raise PreviewError(
                "dimension_template_invalid",
                f"Dimension TemplateVersion {version.id!r} has invalid concept mappings",
            )
        concept_mappings: dict[tuple[str, str], str] = {}
        for raw_mapping in raw_mappings:
            if not isinstance(raw_mapping, dict):
                raise PreviewError(
                    "dimension_template_invalid",
                    f"Dimension TemplateVersion {version.id!r} has an invalid concept mapping",
                )
            mapping_key = (
                str(raw_mapping.get("dimension_id") or "").strip(),
                str(raw_mapping.get("value_id") or "").strip(),
            )
            mapped_concept_id = str(
                raw_mapping.get("concept_key_id") or ""
            ).strip()
            if not all((*mapping_key, mapped_concept_id)) or mapping_key in concept_mappings:
                raise PreviewError(
                    "dimension_template_invalid",
                    f"Dimension TemplateVersion {version.id!r} has an incomplete or duplicate concept mapping",
                )
            concept_mappings[mapping_key] = mapped_concept_id

        resolved_versions.append(version.id)
        raw_constraints = content.get("constraints") or []
        if not isinstance(raw_constraints, list) or any(
            not isinstance(constraint, dict) for constraint in raw_constraints
        ):
            raise PreviewError(
                "dimension_template_invalid",
                f"Dimension TemplateVersion {version.id!r} has invalid constraints",
            )
        template_constraints.append({
            "template_entry_id": entry.id,
            "template_version_id": version.id,
            "combination_cardinality": content.get(
                "combination_cardinality", "one_per_dimension"
            ),
            "constraints": [dict(constraint) for constraint in raw_constraints],
        })
        raw_dimensions = content.get("dimensions")
        if not isinstance(raw_dimensions, list) or not raw_dimensions:
            raise PreviewError(
                "dimension_template_invalid",
                f"Dimension TemplateVersion {version.id!r} has no dimensions",
            )
        seen_values: set[tuple[str, str]] = set()
        for raw_dimension in raw_dimensions:
            if not isinstance(raw_dimension, dict):
                raise PreviewError(
                    "dimension_template_invalid",
                    f"Dimension TemplateVersion {version.id!r} contains an invalid dimension",
                )
            dimension_id = str(raw_dimension.get("id") or "").strip()
            raw_values = raw_dimension.get("values")
            if not dimension_id or not isinstance(raw_values, list) or not raw_values:
                raise PreviewError(
                    "dimension_template_invalid",
                    f"Dimension TemplateVersion {version.id!r} has an incomplete dimension",
                )
            dimension_key = f"{entry.id}:{dimension_id}"
            if dimension_key in dimensions:
                raise PreviewError(
                    "duplicate_dimension_identity",
                    f"Duplicate dimension identity {dimension_key!r}",
                )
            projected_values: list[dict] = []
            for raw_value in raw_values:
                if not isinstance(raw_value, dict):
                    raise PreviewError(
                        "dimension_template_invalid",
                        f"Dimension {dimension_key!r} contains an invalid value",
                    )
                value_id = str(raw_value.get("id") or "").strip()
                if not value_id:
                    raise PreviewError(
                        "dimension_template_invalid",
                        f"Dimension {dimension_key!r} contains a value without stable ID",
                    )
                value_identity = (dimension_id, value_id)
                seen_values.add(value_identity)
                concept_key = ensure_template_concept_key(
                    session,
                    ctx,
                    template_entry_id=entry.id,
                    dimension_id=dimension_id,
                    value_id=value_id,
                    mapped_concept_id=concept_mappings.get(value_identity),
                    description=(
                        "Generated from immutable Dimension Template IDs "
                        f"({version.id})"
                    ),
                )
                projected_values.append({
                    "value": value_id,
                    "concept_key_id": concept_key.id,
                    "template_entry_id": entry.id,
                    "template_version_id": version.id,
                    "dimension_id": dimension_id,
                    "value_id": value_id,
                    "positive_fragments": list(raw_value.get("positive_fragments") or []),
                    "negative_fragments": list(raw_value.get("negative_fragments") or []),
                })
            dimensions[dimension_key] = projected_values
        unknown_mapping_refs = sorted(set(concept_mappings) - seen_values)
        if unknown_mapping_refs:
            raise PreviewError(
                "dimension_template_invalid",
                f"Dimension TemplateVersion {version.id!r} maps unknown dimension values",
            )

    return {
        **selectors,
        "dimension_template_version_ids": resolved_versions,
        "dimension_template_constraints": template_constraints,
        "dimensions": dimensions,
    }


def template_concept_key_id(workspace_id: str, template_entry_id: str, dimension_id: str, value_id: str) -> str:
    """The derived ``dck_`` key of one Dimension Template value (no mapping).

    A pure function of the Workspace and the template's stable entry,
    dimension and value IDs -- never of a label -- so a generated
    combination and an externally made creative tagged with the same value
    (Issue #168) carry the same concept identity.
    """
    identity = {
        "workspace_id": workspace_id,
        "template_entry_id": template_entry_id,
        "dimension_id": dimension_id,
        "value_id": value_id,
    }
    return "dck_" + _canonical_hash(identity)[:48]


def ensure_template_concept_key(
    session: Session,
    ctx: TenantContext,
    *,
    template_entry_id: str,
    dimension_id: str,
    value_id: str,
    mapped_concept_id: str | None = None,
    description: str = "",
) -> DimensionConceptKeyRecord:
    """The governed concept key of one Dimension Template value, created once.

    ``mapped_concept_id`` is the template version's explicit
    ``concept_key_mappings`` entry (it must already exist); otherwise the
    key is :func:`template_concept_key_id` and is created on first use with
    the name ``<entry>:<dimension>:<value>``. Callers that may race on the
    same template (preview, external-creative confirmation) lock the
    template's Library entry row first.
    """
    concept_key_id = mapped_concept_id or template_concept_key_id(
        ctx.workspace_id, template_entry_id, dimension_id, value_id,
    )
    expected_name = f"{template_entry_id}:{dimension_id}:{value_id}"
    concept_key = session.get(DimensionConceptKeyRecord, concept_key_id)
    if concept_key is None and mapped_concept_id:
        raise PreviewError(
            "concept_key_not_governed",
            f"Mapped concept key {concept_key_id!r} does not exist",
        )
    if concept_key is None:
        concept_key = DimensionConceptKeyRecord(
            id=concept_key_id,
            workspace_id=ctx.workspace_id,
            scope="workspace",
            name=expected_name,
            description=description or "Generated from immutable Dimension Template IDs",
            created_by=ctx.initiator_id or ctx.executor,
        )
        session.add(concept_key)
        session.flush()
    elif (
        not (concept_key.scope == "platform" and concept_key.workspace_id is None)
        and not (
            concept_key.scope == "workspace"
            and concept_key.workspace_id == ctx.workspace_id
        )
    ):
        raise PreviewError(
            "concept_key_identity_conflict",
            f"Governed concept key {concept_key_id!r} conflicts with template identity",
        )
    elif not mapped_concept_id and concept_key.name != expected_name:
        raise PreviewError(
            "concept_key_identity_conflict",
            f"Governed concept key {concept_key_id!r} conflicts with template identity",
        )
    return concept_key


def _validate_governed_concepts(
    session: Session,
    ctx: TenantContext,
    combinations: list[dict],
) -> None:
    concept_ids = sorted({
        str(concept.get("concept_key_id") or "")
        for combo in combinations
        for concept in (combo.get("concepts") or [])
        if isinstance(concept, dict)
    })
    if not concept_ids:
        return
    rows = session.scalars(
        select(DimensionConceptKeyRecord).where(
            DimensionConceptKeyRecord.id.in_(concept_ids),
        )
    ).all()
    governed = {
        row.id
        for row in rows
        if (
            (row.scope == "platform" and row.workspace_id is None)
            or (row.scope == "workspace" and row.workspace_id == ctx.workspace_id)
        )
    }
    missing = [concept_id for concept_id in concept_ids if concept_id not in governed]
    if missing:
        raise PreviewError(
            "concept_key_not_governed",
            f"Unknown or out-of-workspace concept keys: {', '.join(missing)}",
        )


def _money(amount: Decimal) -> str:
    """Render a quoted amount without float noise (``"1.272"``, ``"0"``)."""
    text = format(amount.normalize(), "f")
    return "0" if text in ("0", "-0") else text


def _assemble_prompt(selectors: dict) -> str | None:
    """Assemble a frozen prompt from selectors when a template is available."""
    template = selectors.get("prompt_template")
    if not template:
        return None
    variables = selectors.get("prompt_variables", {})
    try:
        return template.format(**variables)
    except (KeyError, IndexError):
        return template


def _recipe_core(draft, resolution) -> dict:
    """The recipe skeleton; ``create_preview`` overwrites ``combination_strategy``
    and ``exclusions`` with the planner's normalised forms (Issue #157) so a
    legacy-shaped Draft and its rewritten twin freeze the same recipe."""
    return {
        "provider": draft.provider,
        "model": draft.model,
        "execution_mode": resolution.effective_mode,
        "combination_strategy": draft.combination_strategy_json,
        "format": draft.format_json,
        "exclusions": draft.exclusions_json,
        "adapter_key": resolution.adapter_key,
        "adapter_version": resolution.adapter_version,
        "capabilities": resolution.capabilities,
        "cost_policy": resolution.cost_policy,
        "scenario_evidence": resolution.scenario_evidence,
        "truth_dependency": _truth_dependency(draft.selectors_json or {}),
    }


def _truth_dependency(selectors: dict) -> dict:
    """Freeze the Generation-time commercial-truth dependency decision.

    Absence is an explicit policy result: this recipe contains no selected
    truth package.  Callers that do inject commercial truth must supply the
    exact immutable package and policy evidence; incomplete declarations fail
    during Preview rather than producing an unevaluable AssetVersion.
    """
    raw = selectors.get("truth_dependency")
    if raw is None:
        evidence = {
            "determination": "no_truth_package_selected",
            "policy": "generation-truth-dependency-v1",
        }
        return {
            "expected": False,
            "policy_version": "generation-truth-dependency-v1",
            "truth_package_id": None,
            "evidence": evidence,
        }
    if not isinstance(raw, dict) or not isinstance(raw.get("expected"), bool):
        raise PreviewError(
            "truth_dependency_invalid",
            "selectors.truth_dependency must explicitly declare expected",
        )
    policy_version = str(raw.get("policy_version") or "").strip()
    evidence = raw.get("evidence")
    package_id = str(raw.get("truth_package_id") or "").strip() or None
    if not policy_version or not isinstance(evidence, dict):
        raise PreviewError(
            "truth_dependency_invalid",
            "truth dependency requires policy_version and evidence",
        )
    if raw["expected"] and not package_id:
        raise PreviewError(
            "truth_dependency_invalid",
            "truth-dependent Generation requires truth_package_id",
        )
    if not raw["expected"] and package_id is not None:
        raise PreviewError(
            "truth_dependency_invalid",
            "truth_package_id is incompatible with expected=false",
        )
    return {
        "expected": raw["expected"],
        "policy_version": policy_version,
        "truth_package_id": package_id,
        "evidence": evidence,
    }


def _as_aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on ``DateTime(timezone=True)`` round-trips; every
    other platform module comparing a stored timestamp against ``utcnow()``
    re-attaches UTC the same way (see e.g. ``app.platform.jobs``,
    ``app.launch.service``)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _canonical_hash(value: object) -> str:
    from ..platform.canonical import canonical_hash
    return canonical_hash(value)


def _default_adapter_registry() -> dict:
    """The deployed adapters, as ``get_generation_capabilities`` advertises them.

    One registry for both the capabilities a Draft is built from and the
    admission its preview runs through, so a provider the dialog offered is
    the provider the preview admits.
    """
    from app.generation.capabilities import PLATFORM_ADAPTER_REGISTRY

    return PLATFORM_ADAPTER_REGISTRY
