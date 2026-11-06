"""Typed response contracts for the Localization v1 HTTP surface."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


#: ``LocalizationBatchRecord.preparation_stage`` (``ck_lb_preparation_stage``).
#: The interface words each stage (``localization:preparation.state-<stage>``,
#: ``web/__tests__/locales/state-codes.spec.ts``).
PreparationStage = Literal["analysis", "truth", "claim_review", "copy", "complete"]


class LocalizationAction(BaseModel):
    action: str
    allowed: bool
    reason_code: str | None = None


class LocalizationSourceContextProblem(BaseModel):
    code: Literal[
        "missing_source_context",
        "invalid_source_context",
        "conflicting_source_context",
    ]
    missing_fields: list[
        Literal["market", "placement", "locale", "country", "currency"]
    ] = Field(default_factory=list)
    invalid_fields: list[
        Literal["market", "placement", "locale", "country", "currency"]
    ] = Field(default_factory=list)
    conflicting_fields: list[
        Literal["market", "placement", "locale", "country", "currency"]
    ] = Field(default_factory=list)


class LocalizationReusableInventory(BaseModel):
    id: str
    analysis_recipe_hash: str
    version_number: int
    claim_types: list[str]


class EligibleLocalizationSource(BaseModel):
    evidence_origin: str = "legacy_experiment"
    asset_version_id: str
    decision_id: str
    experiment_id: str
    experiment_version_id: str
    experiment_version_candidate_id: str
    source_generation_mode: str
    experiment_evidence_mode: str
    product_version_id: str
    offer_version_id: str
    source_market: str
    placement: str
    source_market_snapshot: dict[str, str] | None = None
    source_context_problem: LocalizationSourceContextProblem | None = None
    reusable_inventories: list[LocalizationReusableInventory] = Field(
        default_factory=list
    )
    asset: dict[str, Any] = Field(default_factory=dict)
    product: dict[str, Any] = Field(default_factory=dict)
    offer: dict[str, Any] = Field(default_factory=dict)


class LocalizationMarket(BaseModel):
    market: str
    locale: str
    country: str
    currency: str
    market_policy_version_id: str
    requires_local_copy: bool
    base_tone: str


class LocalizationPolicyFact(BaseModel):
    fact_type: str
    on_failure: Literal["block", "degrade", "skip"]


class LocalizationConfigurationVersion(BaseModel):
    """A published, non-secret LibraryVersion usable by Localization.

    The workspace projection exposes identity, display metadata and the
    published policy's non-secret fact requirements. Adapter credentials
    remain behind the domain/KMS boundary.
    """

    id: str
    entry_id: str
    entry_type: str
    display_name: str
    fact_policies: list[LocalizationPolicyFact] = Field(default_factory=list)


class LocalizationWorkspace(BaseModel):
    role: str
    eligible_sources: list[EligibleLocalizationSource] = Field(default_factory=list)
    markets: list[LocalizationMarket] = Field(default_factory=list)
    truth_resolution_policies: list[LocalizationConfigurationVersion] = Field(
        default_factory=list
    )
    data_sources: list[LocalizationConfigurationVersion] = Field(default_factory=list)


class LocalizationBatchSummary(BaseModel):
    id: str
    phase: str
    preparation_stage: PreparationStage | None = None
    outcome: str
    truth_freshness: str | None = None
    truth_status: str | None = None
    truth_valid_until: str | None = None
    product_version_id: str
    offer_version_id: str
    provider_execution_mode: str
    evidence_tier: str | None = None
    source_count: int
    market_count: int
    item_count: int
    item_state_counts: dict[str, int] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str
    allowed_actions: list[LocalizationAction] = Field(default_factory=list)


class LocalizationBatchPage(BaseModel):
    items: list[LocalizationBatchSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class LocalizationBatchSource(BaseModel):
    id: str
    asset_version_id: str
    decision_id: str
    experiment_id: str
    experiment_version_id: str
    source_generation_mode: str
    experiment_evidence_mode: str
    source_market_snapshot: dict[str, str] = Field(default_factory=dict)
    analysis_recipe_hash: str | None = None
    inventory_id: str | None = None
    reusable_inventories: list[LocalizationReusableInventory] = Field(
        default_factory=list
    )


class LocalizationTruthFact(BaseModel):
    id: str
    fact_type: str
    scope_key: str
    value: Any
    external_object_ref: str
    accepted_source_attempt_id: str
    requested_context: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str
    mock_scenario: str | None = None
    mock_version: str | None = None
    observed_at: str
    valid_until: str
    source_valid_until: str | None = None


class LocalizationTruthSourceAttempt(BaseModel):
    id: str
    fact_type: str
    adapter_kind: str
    data_source_version_id: str
    outcome: str
    failure_code: str | None = None
    skip_code: str | None = None


class LocalizationTruthMarket(BaseModel):
    market: str
    market_policy_version_id: str
    readiness: str
    readiness_detail: dict[str, Any] = Field(default_factory=dict)
    locale: str | None = None
    country: str | None = None
    currency: str | None = None
    truth_package_market_id: str | None = None
    facts: list[LocalizationTruthFact] = Field(default_factory=list)
    source_attempts: list[LocalizationTruthSourceAttempt] = Field(default_factory=list)


class LocalizedCopySummary(BaseModel):
    id: str
    source_asset_version_id: str
    target_market: str
    state: str
    copy_origin: str
    content: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str


class LocalizationBatchItemSummary(BaseModel):
    id: str
    state: str
    target_market: str
    source_asset_version_id: str
    attempt_count: int
    reason_code: str | None = None
    reason_detail: str | None = None
    recoverable: bool | None = None
    next_action: str | None = None
    revision: int
    allowed_actions: list[LocalizationAction] = Field(default_factory=list)


class LocalizationCommandSummary(BaseModel):
    id: str
    command_kind: str
    item_id: str | None = None
    operation_id: str | None = None
    actor_id: str
    created_at: str


class LocalizationSourceClaim(BaseModel):
    claim_index: int
    claim_type: str
    candidate_fact_types: list[str] = Field(default_factory=list)
    market_sensitive: bool = False
    scope_key: str
    ocr_text: str | None = None
    confidence: float | None = None
    risk: str | None = None
    conflicting: bool = False
    region: dict[str, Any] | None = None


class LocalizationSourceInventory(BaseModel):
    id: str
    asset_version_id: str
    analysis_recipe_hash: str
    version_number: int
    provider: str
    model: str
    analysis_execution_mode: str
    evidence_hash: str
    claims: list[LocalizationSourceClaim] = Field(default_factory=list)


class LocalizationClaimDecisionSetSummary(BaseModel):
    id: str
    inventory_id: str
    source_asset_version_id: str
    target_market: str
    decision_count: int


class LocalizationBatchDetail(LocalizationBatchSummary):
    source_market_snapshot: dict[str, Any] = Field(default_factory=dict)
    truth_package_id: str | None = None
    truth_data_mode: str
    max_attempts: int
    frozen_recipe: dict[str, Any] | None = None
    frozen_at: str | None = None
    sources: list[LocalizationBatchSource] = Field(default_factory=list)
    markets: list[LocalizationTruthMarket] = Field(default_factory=list)
    copies: list[LocalizedCopySummary] = Field(default_factory=list)
    items: list[LocalizationBatchItemSummary] = Field(default_factory=list)
    commands: list[LocalizationCommandSummary] = Field(default_factory=list)
    inventories: list[LocalizationSourceInventory] = Field(default_factory=list)
    claim_decision_sets: list[LocalizationClaimDecisionSetSummary] = Field(
        default_factory=list
    )


class LocalizationAttemptOutput(BaseModel):
    id: str
    content_sha256: str
    byte_size: int
    mime_type: str
    isolated: bool


class LocalizedCopyDetail(BaseModel):
    id: str
    state: str
    origin: str
    content: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str


class LocalizationOutputPublication(BaseModel):
    id: str
    publication_kind: str
    asset_version_id: str | None = None
    created_at: str


class LocalizationQcCheck(BaseModel):
    id: str
    check_kind: str
    check_slot_key: str
    severity: str
    result: str
    expected: Any
    observed: Any
    rule_version: str


class LocalizationMaximumCost(BaseModel):
    amount: str
    currency: str
    state: str


class LocalizationProviderCall(BaseModel):
    step: str
    dispatch_state: str
    chargeability: str
    provider: str
    model: str | None = None
    error_code: str | None = None
    maximum_cost: LocalizationMaximumCost | None = None


class LocalizationAttemptSummary(BaseModel):
    id: str
    attempt_number: int
    state: str
    provider: str
    model: str
    created_at: str
    output: LocalizationAttemptOutput | None = None
    publication: LocalizationOutputPublication | None = None
    qc: list[LocalizationQcCheck] = Field(default_factory=list)
    provider_calls: list[LocalizationProviderCall] = Field(default_factory=list)


class LocalizationCostTotal(BaseModel):
    amount: str
    currency: str


class LocalizationCostLedgerEntry(BaseModel):
    entry_type: str
    direction: str
    amount: str
    currency: str
    provider: str
    model: str | None = None
    created_at: str


class LocalizationItemCost(BaseModel):
    totals: list[LocalizationCostTotal] = Field(default_factory=list)
    ledger: list[LocalizationCostLedgerEntry] = Field(default_factory=list)


class LocalizationAssetBinding(BaseModel):
    id: str
    kind: str
    bound_asset_version_id: str
    review_state: str
    truth_freshness: str
    ready_for_experiment: bool


class LocalizationAssetLineage(BaseModel):
    source_asset_version_id: str
    parent_asset_version_id: str | None = None
    decision_id: str
    offer_version_id: str
    truth_package_id: str
    copy_version_id: str | None = None
    localized_asset_version_id: str | None = None
    attempt_id: str | None = None
    output_id: str | None = None
    publication_id: str | None = None


class LocalizationItemDetail(BaseModel):
    id: str
    batch_id: str
    state: str
    target_market: str
    source_asset_version_id: str
    decision_id: str
    truth_package_id: str
    truth_package_market_id: str
    market_policy_version_id: str
    inventory_id: str
    claim_decision_set_id: str
    copy_version_id: str | None = None
    attempt_count: int
    max_attempts: int
    reason_code: str | None = None
    reason_detail: str | None = None
    recoverable: bool | None = None
    next_action: str | None = None
    revision: int
    localized_copy: LocalizedCopyDetail | None = None
    attempts: list[LocalizationAttemptSummary] = Field(default_factory=list)
    cost: LocalizationItemCost = Field(default_factory=LocalizationItemCost)
    binding: LocalizationAssetBinding | None = None
    lineage: LocalizationAssetLineage
    allowed_actions: list[LocalizationAction] = Field(default_factory=list)


class BatchDraftResult(BaseModel):
    batch_id: str
    phase: str
    reused: bool


class StartPreparationResult(BaseModel):
    batch_id: str
    truth_package_id: str
    truth_status: str
    preparation_stage: PreparationStage
    operation_id: str | None = None
    missing_analysis: list[str] = Field(default_factory=list)
    replayed: bool


class SubmitClaimDecisionsResult(BaseModel):
    batch_id: str
    decision_set_id: str
    copy_version_id: str | None = None
    copy_origin: str
    operation_id: str | None = None
    replayed: bool


class ApproveCopyResult(BaseModel):
    batch_id: str
    copy_version_id: str
    approval_id: str


class ConfirmResult(BaseModel):
    batch_id: str
    item_ids: list[str] = Field(default_factory=list)
    operation_id: str | None = None
    replayed: bool


class RetryItemResult(BaseModel):
    item_id: str
    attempt_number: int
    operation_id: str | None = None
    replayed: bool


class ResumeItemResult(BaseModel):
    item_id: str
    state: str
    replayed: bool


class AuthorizeIsolatedQCResult(BaseModel):
    item_id: str
    attempt_id: str
    operation_id: str | None = None
    replayed: bool
    #: Only the frozen external steps that had not already passed. Empty
    #: means the Attempt's QC plan is complete and the caller can adopt.
    authorized_steps: list[str] = Field(default_factory=list)


class AdoptIsolatedOutputResult(BaseModel):
    item_id: str
    attempt_id: str
    replayed: bool
    #: The published localized child and the evidence pointing at it. The
    #: browser navigates by resource id, never by a URL the BFF would have
    #: to rewrite.
    asset_version_id: str = ""
    publication_id: str = ""
    binding_id: str = ""


class CancelResult(BaseModel):
    batch_id: str
    cancelled_item_ids: list[str] = Field(default_factory=list)
    already_terminal: bool


class BatchOutcome(BaseModel):
    batch_id: str
    phase: str
    outcome: str
    revision: int
    item_count: int
    item_state_counts: dict[str, int] = Field(default_factory=dict)
