"""Pydantic response models for Generation v1 routes (Issue #27 Task 3).

Mirrors ``app.library.response_models``: these are ``response_model``
declarations FastAPI uses both to generate the OpenAPI schema consumed by
``scripts/generate_openapi.py`` and to validate/serialize the plain dicts
returned by ``app.generation.v1_router``, ``app.generation.run_queries`` and
the pre-existing Generation domain modules (``draft``, ``preview``,
``submission``, ``export``, ``review``, ``regeneration``).

Field shapes mirror what those modules actually produce -- not an idealized
future contract -- so ``response_model`` validation never silently drops or
500s on real output. Several fields (``allowed_actions``, cost, provider
attempt evidence) are intentionally thin placeholders: the brief for this
task explicitly scopes ``run_queries``/``run_commands``/``recovery_preview``/
``capabilities`` as minimal projections, filled in iteratively as the UI
sub-projects that consume these endpoints land.

Issue #52 enriches ``GenerationPreviewDetail`` with UI-facing projections:
``combination_count``, ``call_count``, ``maximum_cost``, ``final_prompt``,
``items``, ``capability_decisions``, and ``exclusion_summary``. Issue #49
Task 8 adds Loser exclusion's ``skipped_signature_count``,
``skipped_call_count``, ``estimated_cost_saving``, and each
``PreviewItemSummary``'s ``stable_dimension_signature``.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from app.generation.image_tools import ImageToolSpec
from pydantic import BaseModel, Field, model_validator

from app.platform.action_eligibility import ActionEligibility


class Page(BaseModel):
    """Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``."""

    items: list[Any]
    next_cursor: str | None = None


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------


class GenerationDraftSummary(BaseModel):
    """Row of ``GET /generation/drafts`` (run_queries.list_drafts)."""

    id: str
    product_id: str
    provider: str
    model: str
    execution_mode: str
    state: str
    revision: int
    updated_at: str | None = None


class GenerationDraftDetail(GenerationDraftSummary):
    """``GET /generation/drafts/{id}`` and the create/update/regenerate result."""

    selectors: dict[str, Any] = Field(default_factory=dict)
    combination_strategy: dict[str, Any] = Field(default_factory=dict)
    format_config: dict[str, Any] = Field(default_factory=dict)
    exclusions: dict[str, Any] = Field(default_factory=dict)
    regeneration_intent: str = "new_generation"
    source_asset_version_id: str | None = None
    source_recipe_identity: str | None = None
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Previews
# --------------------------------------------------------------------------


class MaximumCost(BaseModel):
    """Cost ceiling for a preview, expressed as a Decimal string with currency."""

    amount: str
    currency: str


class PreviewItemSummary(BaseModel):
    """Per-combination detail inside a resolved preview.

    ``stable_dimension_signature`` (Issue #49 Task 8) is the combination's
    dimension-only fingerprint -- the identity Loser exclusion matches
    against a signature's newest produced AssetVersion.
    """

    ordinal: int
    dimension_tags: dict[str, str] = Field(default_factory=dict)
    reference_asset_ids: list[str] = Field(default_factory=list)
    excluded: bool = False
    exclusion_reason: str | None = None
    stable_dimension_signature: str = ""


class CapabilityDecision(BaseModel):
    """Provider capability resolution result."""

    capability: str
    supported: bool
    detail: str | None = None


class ExclusionSummary(BaseModel):
    """Summary of excluded combinations in a preview."""

    excluded_count: int
    reasons: list[str] = Field(default_factory=list)


class StrategyApplied(BaseModel):
    """The combination strategy the planner actually applied (Issue #157).

    ``kind`` is ``full`` or ``coverage``; ``cap`` is set for ``coverage``.
    ``deprecated_shape`` is ``True`` when the Draft still carries the legacy
    ``{mode, sample_cap}`` shape the server mapped onto ``kind``/``cap``.
    ``full_count`` is the Cartesian product, ``candidate_count`` what was
    left after template constraints and exclusion rules, ``selected_count``
    what sampling kept (equals ``combination_count`` unless Loser exclusion
    removed more).
    """

    kind: str
    cap: int | None = None
    deprecated_shape: bool = False
    full_count: int = 0
    candidate_count: int = 0
    selected_count: int = 0


class GenerationPreviewDetail(BaseModel):
    """``POST /generation/drafts/{id}/previews`` and ``GET /generation/previews/{id}``."""

    purpose: Literal["initial_generation"] = "initial_generation"
    id: str
    preview_hash: str
    recipe_input_hash: str
    execution_mode: str
    valid_until: str | None = None
    draft_id: str
    draft_revision: int
    invalidation_reason: str | None = None
    combination_count: int = 0
    call_count: int = 0
    maximum_cost: MaximumCost | None = None
    final_prompt: str | None = None
    items: list[PreviewItemSummary] = Field(default_factory=list)
    capability_decisions: list[CapabilityDecision] = Field(default_factory=list)
    exclusion_summary: ExclusionSummary | None = None
    skipped_signature_count: int = 0
    skipped_call_count: int = 0
    estimated_cost_saving: MaximumCost | None = None
    strategy_applied: StrategyApplied | None = None
    combinations_dropped_by_constraints: int = 0
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)
    prompt: str = ""
    quantity: int = 1
    selection_maximum_cost: str = "0"
    currency: str = "USD"
    pricing_version: str = ""
    provider: str = ""
    model: str = ""
    image_tool: ImageToolSpec | None = None
    format_config: dict[str, Any] = Field(default_factory=dict)
    submitted_run_id: str | None = None


class OperationRef(BaseModel):
    """``POST /generation/runs`` result: the Operation created by Atomic Submission."""

    operation_id: str
    generation_run_id: str


class RunCommandResult(BaseModel):
    """Result of a Run mutation command (cancel/retry/resume/reconcile)."""

    operation_id: str
    run_id: str


# --------------------------------------------------------------------------
# Runs / RunItems
# --------------------------------------------------------------------------


class GenerationRunCommandSummary(BaseModel):
    id: str
    command_kind: str
    operation_id: str
    created_at: str | None = None


class GenerationRunSummary(BaseModel):
    """Row of ``GET /generation/runs`` (run_queries.list_runs)."""

    id: str
    product_id: str
    provider: str
    model: str
    status: str
    total: int
    done: int
    failed: int
    execution_mode: str
    created_at: str | None = None


class GenerationRunDetail(GenerationRunSummary):
    """``GET /generation/runs/{id}`` (run_queries.get_run_detail)."""

    revision: int
    recipe_snapshot_id: str | None = None
    commands: list[GenerationRunCommandSummary] = Field(default_factory=list)
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


class AssetThumbnailLink(BaseModel):
    """A short-lived signed GET for an AssetVersion's gallery thumbnail
    (Issue #159, ``app.generation.thumbnails``).

    Embedded per item by the two list projections that render tiles
    (``AssetVersionSummary.thumbnail``, ``GenerationRunItemSummary.thumbnail``)
    so a page of tiles costs one list request plus one small GET per tile.
    It is the 512px-long-edge derivative, never the original -- Asset
    Detail, the Lightbox and exports keep the download-intent path for
    originals. ``null`` whenever the version has no derivative (legacy rows
    awaiting backfill, a derivative that could not be produced, a RunItem
    that has produced nothing yet); the browser then falls back to the
    original through the download-intent route.
    """

    url: str
    expires_at: str
    mime_type: str
    width: int | None = None
    height: int | None = None


class GenerationRunItemSummary(BaseModel):
    """Row of ``GET /generation/runs/{id}/items`` (run_queries.list_run_items).

    ``bound_asset_version_id`` is the AssetVersion the item produced (or
    reused) -- ``None`` until the item succeeds -- and ``thumbnail`` that
    version's signed thumbnail link when it has one (Issue #159), so the Run
    page can tile images without a per-item detail or download-intent call.
    """

    id: str
    ordinal: int
    state: str
    combination_fingerprint: str
    variant_index: int
    tags: dict[str, Any] = Field(default_factory=dict)
    bound_asset_version_id: str | None = None
    thumbnail: AssetThumbnailLink | None = None
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


class AttemptCost(BaseModel):
    """Cost sub-object inside AttemptSummary: Decimal string + currency."""

    amount: str
    currency: str


class AttemptSummary(BaseModel):
    """Latest provider attempt state for a RunItem."""

    dispatch_certainty: str
    outcome: str
    provider: str
    model: str | None = None
    version: str
    scenario: str | None = None
    cost: AttemptCost | None = None


class QcCheckSummary(BaseModel):
    """Individual QC check result (``verdict`` since Issue #169)."""

    name: str
    passed: bool
    detail: str | None = None
    verdict: str | None = None


class QcResultSummary(BaseModel):
    """Latest QC attempt state and check results for a RunItem."""

    state: str
    checks: list[QcCheckSummary] = Field(default_factory=list)


class CostEvidence(BaseModel):
    """Spend reservation state for a RunItem's provider call."""

    state: str
    amount: str
    currency: str


class GenerationRunItemDetail(GenerationRunItemSummary):
    """``GET /generation/runs/{id}/items/{item_id}`` (run_queries.get_run_item_detail)."""

    combination_identity: dict[str, Any] = Field(default_factory=dict)
    execution_mode: str
    error_summary: str = ""
    #: The stable code that leads ``error_summary`` -- what the interface
    #: renders; the summary itself is a diagnostic (#253).
    error_code: str | None = None
    source_asset_version_id: str | None = None
    source_run_item_id: str | None = None
    intent_kind: str = "new_generation"
    latest_attempt: AttemptSummary | None = None
    qc_result: QcResultSummary | None = None
    # ``bound_asset_version_id`` is inherited from the summary row.
    cost_evidence: CostEvidence | None = None


class GenerationRecoveryPreview(BaseModel):
    """POST /generation/runs/{id}/recovery-previews and
    GET /generation/previews/{id} when purpose is run_recovery."""

    purpose: Literal["run_recovery"] = "run_recovery"
    id: str
    preview_hash: str
    command: str
    targets: list[str] = Field(default_factory=list)
    estimated_additional_cost: MaximumCost
    valid_until: str
    run_id: str = ""
    expected_run_revision: int = 0
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


GenerationResolutionPreview = Annotated[
    GenerationPreviewDetail | GenerationRecoveryPreview,
    Field(discriminator="purpose"),
]


class RecoveryPreviewSelection(BaseModel):
    kind: str = Field(
        min_length=1,
        description="item_ids | all_eligible_failed | all_resumable_paused",
    )
    item_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_selection(self):
        if self.kind == "item_ids" and len(self.item_ids) == 0:
            raise ValueError("item_ids must not be empty when kind is 'item_ids'")
        if self.kind in ("all_eligible_failed", "all_resumable_paused") and self.item_ids:
            raise ValueError(f"item_ids must be empty when kind is '{self.kind}'")
        return self


class RecoveryPreviewInput(BaseModel):
    command: str = Field(min_length=1)
    selection: RecoveryPreviewSelection

    @model_validator(mode="after")
    def _validate_command_selection(self):
        allowed = {
            "retry_failed_items": ("item_ids", "all_eligible_failed"),
            "resume_paused_items": ("item_ids", "all_resumable_paused"),
        }
        kinds = allowed.get(self.command)
        if kinds is None:
            raise ValueError(f"command must be one of {list(allowed)}")
        if self.selection.kind not in kinds:
            raise ValueError(
                f"selection.kind '{self.selection.kind}' not valid for "
                f"command '{self.command}'; expected one of {kinds}"
            )
        return self


class GenerationModelOption(BaseModel):
    image_tools: list[Literal["upscale", "remove_background", "erase"]] = Field(default_factory=list)
    id: str
    image_sizes: list[str]
    maximum_costs: dict[str, str]
    available: bool
    unavailable_reason: str | None = None


class GenerationProviderOption(BaseModel):
    id: str
    label: str
    default_model: str
    execution_mode: str
    credential_ready: bool
    available: bool
    unavailable_reason: str | None = None
    aspect_ratios: list[str]
    models: list[GenerationModelOption]


class GenerationProductOption(BaseModel):
    id: str
    name: str
    version_id: str | None = None


class GenerationModelOption(BaseModel):
    image_tools: list[Literal["upscale", "remove_background", "erase"]] = Field(default_factory=list)
    id: str
    image_sizes: list[str]
    maximum_costs: dict[str, str]
    available: bool
    unavailable_reason: str | None = None


class GenerationProviderOption(BaseModel):
    id: str
    label: str
    default_model: str
    execution_mode: str
    credential_ready: bool
    available: bool
    unavailable_reason: str | None = None
    aspect_ratios: list[str]
    models: list[GenerationModelOption]


class GenerationProductOption(BaseModel):
    id: str
    name: str
    version_id: str | None = None


class GenerationProductOption(BaseModel):
    id: str
    name: str
    version_id: str | None = None


class GenerationCapabilities(BaseModel):
    """Workspace provider catalog and non-secret readiness."""

    providers: list[str] = Field(default_factory=list)
    models: dict[str, Any] = Field(default_factory=dict)
    modes: list[str] = Field(default_factory=list)
    format_limits: dict[str, Any] = Field(default_factory=dict)
    credential_readiness: dict[str, Any] = Field(default_factory=dict)
    #: Governed ``llm_chat`` (Issue #160): modes, ``byok_ready``, models and
    #: their per-token prices (``app.providers.llm_chat.llm_capability``).
    llm: dict[str, Any] = Field(default_factory=dict)
    provider_options: list[GenerationProviderOption] = Field(default_factory=list)
    products: list[GenerationProductOption] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Assets / AssetVersions
# --------------------------------------------------------------------------


class AssetVersionSummary(BaseModel):
    """Row of ``GET /generation/assets`` (``asset_queries.list_asset_versions``,
    Issue #49 Task 3, spec §0.2).

    ``id`` is the AssetVersion id -- the exact-version identity the browser
    reviews/regenerates/downloads against, matching
    ``app.generation.review``'s exact-version anchor.

    ``stable_dimension_signature`` and ``mock_evidence`` are always
    ``None`` for now -- no column backs either of them yet.
    ``contextual_conclusion`` (Task 6, spec §0.7) is populated only when
    the request supplied ``experiment_context``; otherwise it stays
    ``None`` -- it is never a global property of the AssetVersion.
    ``effective_review_value`` applies Task 5's supersession-aware
    head-review rule (``app.generation.review.project_effective_review``,
    spec §0.3); ``latest_qc_state`` is the latest
    ``AssetQCAttemptRecord.state``. ``thumbnail`` (Issue #159) is the
    version's signed thumbnail link, or ``None`` when it has no derivative.
    """

    id: str
    asset_id: str
    product_id: str
    run_id: str
    provider: str
    model: str
    status: str
    aspect_ratio: str
    stable_dimension_signature: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    effective_review_value: str | None = None
    latest_qc_state: str | None = None
    contextual_conclusion: str | None = None
    origin_kind: str
    execution_mode: str | None = None
    mock_evidence: dict[str, Any] | None = None
    byte_size: int
    thumbnail: AssetThumbnailLink | None = None
    created_at: str | None = None


class GalleryFacetValue(BaseModel):
    value: str
    count: int


class AssetGalleryFacets(BaseModel):
    """Server-computed facets over the full matching Workspace dataset, not
    just the currently loaded page (spec §0.2). ``reviews`` (Task 5) is
    computed from the supersession-aware effective-review projection, with
    a "no effective review" bucket surfaced as the value ``"unreviewed"``.
    ``qc_states`` and ``tags`` stay empty until Task 6 lands their backing
    columns."""

    providers: list[GalleryFacetValue] = Field(default_factory=list)
    origins: list[GalleryFacetValue] = Field(default_factory=list)
    modes: list[GalleryFacetValue] = Field(default_factory=list)
    reviews: list[GalleryFacetValue] = Field(default_factory=list)
    aspects: list[GalleryFacetValue] = Field(default_factory=list)
    qc_states: list[GalleryFacetValue] = Field(default_factory=list)
    tags: list[GalleryFacetValue] = Field(default_factory=list)


class AssetGalleryPage(BaseModel):
    """``GET /generation/assets`` (Issue #49 Task 3, spec §0.2). Replaces the
    generic ``Page[Any]`` envelope with a typed page carrying server-computed
    facets and collection-level ``allowed_actions`` (export eligibility for
    the current scope)."""

    items: list[AssetVersionSummary] = Field(default_factory=list)
    next_cursor: str | None = None
    facets: AssetGalleryFacets
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)


class RegenerationDraftBatchItem(BaseModel):
    """One created Draft in a review-summary batch result (Issue #49 Task 7):
    the Draft id, the source AssetVersion chosen for its
    stable-dimension-signature group, that group's signature, and the
    variant count the batch action requested for it (2 for
    ``regenerate_needs_changes``, 3 for ``supplement_approved``)."""

    generation_draft_id: str
    source_asset_version_id: str | None = None
    stable_dimension_signature: str = ""
    requested_variant_count: int | None = None


class RegenerationDraftBatchResult(BaseModel):
    """``POST /generation/regeneration-draft-batches`` result (Issue #49
    Task 7, spec §0.3/§0.4). Browser Task 10's ``ReviewSummary`` consumes
    this."""

    drafts: list[RegenerationDraftBatchItem] = Field(default_factory=list)


class AssetReview(BaseModel):
    """Row of ``AssetVersionDetail.reviews`` and the create-review result."""

    id: str
    asset_version_id: str
    review_value: str | None = None
    reason: str = ""
    supersedes_review_id: str | None = None
    actor_id: str
    created_at: str | None = None
    #: ``human`` for a person's review; ``machine_qc`` for the quality
    #: projection a failed VLM check recorded (Issue #169).
    source: str = "human"


class AssetVersionSeed(BaseModel):
    """Requested vs. effective seed for one AssetVersion's generation call,
    plus the reason the effective seed is unavailable/differs (spec §0.2)."""

    requested: str | None = None
    effective: str | None = None
    unavailable_reason: str | None = None


class AssetQCCheckSummary(BaseModel):
    """One check of a QC attempt (Issue #169): a must-match item, product
    presence, text artifacts or reference readability, with its verdict
    (``pass``/``fail``/``uncertain``/``skip``) and the Chinese reason."""

    rule_id: str
    check_type: str
    severity: str
    outcome: str
    verdict: str
    reason: str | None = None
    fix_hint: str | None = None
    must_match_id: str | None = None
    description: str | None = None


class AssetQCAttemptSummary(BaseModel):
    """Row of ``AssetVersionDetail.qc_attempts`` -- QC attempt history
    (spec §0.2's "QC attempt history + latest_qc_attempt_id"). Since Issue
    #169 it carries the VLM check's summary, reasons and per-check verdicts,
    how many consecutive failures this combination has had, and which human
    quality the failure projected (``needs_changes`` / ``rejected``)."""

    id: str
    state: str
    attempt_sequence: int
    feedback_round: int
    execution_mode: str
    created_at: str | None = None
    summary: str | None = None
    reasons: list[str] = Field(default_factory=list)
    consecutive_failures: int = 0
    projected_review: str | None = None
    model: str | None = None
    controlled_mock: bool = False
    checks: list[AssetQCCheckSummary] = Field(default_factory=list)


class AssetVersionLineageEntry(BaseModel):
    """One ``parent``/``children`` lineage entry (spec §0.2/§0.6): the exact
    AssetVersion id plus the regeneration intent that produced this edge --
    the *child* version's producing ``RunItemRecord.intent_kind``, not a
    property of the parent. ``intent`` is ``None`` when the producing
    RunItem cannot be resolved (e.g. legacy data)."""

    version_id: str
    intent: str | None = None


class AssetVersionDetail(BaseModel):
    """``GET /generation/asset-versions/{id}``
    (``asset_queries.get_asset_version_detail``, Issue #49 Task 4, spec
    §0.2). Extends ``AssetVersionSummary``'s fields with full provenance:
    seed, prompt, append-only ``reviews[]``, QC attempt history, and
    parent/children lineage. Browser Task 8's ``LightboxModal`` consumes
    this.

    Raw object keys and committed storage-version identifiers are
    deliberately never exposed to the browser -- downloads go through a
    signed DownloadIntent, not a raw blob reference.
    ``stable_dimension_signature`` and ``mock_evidence`` stay ``None`` --
    no backing column exists yet. ``contextual_conclusions`` (Task 6, spec
    §0.7) is every non-revoked, fully-anchored
    ``ContextualExperimentConclusion`` for this exact AssetVersion, each
    carrying its own full context. ``review_head_id``/
    ``effective_review_value`` apply Task 5's supersession-aware head-review
    rule (spec §0.3) -- a ``cleared`` head still sets ``review_head_id`` but
    projects ``effective_review_value`` to ``None``.
    """

    id: str
    asset_id: str
    product_id: str
    run_id: str
    provider: str
    model: str
    status: str
    kind: str
    aspect_ratio: str
    stable_dimension_signature: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    mime_type: str
    byte_size: int
    content_sha256: str
    origin_kind: str
    execution_mode: str | None = None
    mock_evidence: dict[str, Any] | None = None
    seed: AssetVersionSeed
    prompt: str
    #: Issue #167: the reference lineage of the producing call -- which
    #: ``reference_asset`` versions it sent (role, viewpoint, SHA-256), the
    #: recipe's reference decision and the confirmed must-match ids. ``None``
    #: for Mock output and AssetVersions produced before it was recorded.
    reference_sources: dict[str, Any] | None = None
    producing_run_item_id: str | None = None
    provider_attempt_id: str | None = None
    reviews: list[AssetReview] = Field(default_factory=list)
    review_head_id: str | None = None
    effective_review_value: str | None = None
    qc_attempts: list[AssetQCAttemptSummary] = Field(default_factory=list)
    latest_qc_attempt_id: str | None = None
    latest_qc_state: str | None = None
    contextual_conclusions: list[Any] = Field(default_factory=list)
    parent: AssetVersionLineageEntry | None = None
    children: list[AssetVersionLineageEntry] = Field(default_factory=list)
    allowed_actions: list[ActionEligibility] = Field(default_factory=list)
    created_at: str | None = None


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


class ExportPackageQueued(BaseModel):
    """``POST /generation/exports`` 202 result (Issue #49 Task 9).

    ``state`` is the authorizing Operation's state (``"queued"`` for a
    fresh call, or whatever state a replayed Operation has already reached)
    -- not the ExportPackage's own ``pending``/``building``/``ready``/
    ``failed`` projection, which ``GET /generation/exports/{id}``
    (``ExportPackageDetail``) surfaces instead.
    """

    package_id: str
    operation_id: str
    state: str


class ExportPackageDetail(BaseModel):
    """``GET /generation/exports/{id}`` result (export.get_export_package).

    Recovery/polling shape (Issue #49 Task 9): the browser's ``useExportFlow``
    polls this alongside the Operation to recover state across a refresh.
    ``state`` here is the ExportPackage's own projection
    (``pending``/``building``/``ready``/``failed``/``expired``), unlike the
    202 response's Operation-state ``state``.
    """

    package_id: str
    operation_id: str
    execution_mode: str
    state: str
    revision: int
    byte_size: int | None = None
    zip_hash: str | None = None


class ExportManifestEntry(BaseModel):
    asset_version_id: str
    asset_id: str
    content_sha256: str | None = None
    byte_size: int | None = None
    mime_type: str | None = None
    origin_kind: str | None = None
    execution_mode: str | None = None
    producing_run_item_id: str | None = None
    provider_attempt_id: str | None = None
    requested_seed: str | None = None
    effective_seed: str | None = None
    seed_unavailable_reason: str | None = None


class ExportManifest(BaseModel):
    """``GET /generation/exports/{id}/manifest`` (export.build_manifest)."""

    package_id: str
    execution_mode: str
    entries: list[ExportManifestEntry] = Field(default_factory=list)
    total_versions: int


class DownloadGrant(BaseModel):
    """Signed capability returned by a download-intent route (spec §7.4).

    The browser never sees an object key or storage credentials -- only a
    short-lived signed ``url`` plus enough shape (``kind``, ``headers``,
    ``content_disposition``) to perform the fetch and save the result.
    """

    url: str
    kind: str = "signed_get"
    headers: dict[str, str] = Field(default_factory=dict)
    content_disposition: str


class DownloadIntentResponse(BaseModel):
    """Shared shape for asset-version/export download-intent results.

    ``download_grant`` is optional because the export-package route
    (``create_generation_export_download_intent``) does not yet issue one
    (Issue #49 Task 9 wires that); the asset-version route always populates
    it.
    """

    intent_id: str
    expires_at: str | None = None
    download_grant: DownloadGrant | None = None
