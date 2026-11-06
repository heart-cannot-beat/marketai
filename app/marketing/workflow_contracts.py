"""Frozen delivery protocols and exact-version marketing review contracts."""
from datetime import date
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError
from .contracts import FrozenTestPlan, Document, Text, Understanding, CreativeCombination
from .ai_qc_contracts import AICheckFinding, RenderedText
from app.generation.image_tools import ImageToolSpec
from app.generation.response_models import AssetThumbnailLink
from .reporting import MediaCurrency, AttributionWindow, MIN_ATTRIBUTION_LAG


class EvaluationProtocol(Document):
    version: Literal["marketing-eval-v1"] = "marketing-eval-v1"
    min_days: int = Field(default=7, ge=3, le=90)
    min_impressions: int = Field(default=1000, ge=100, le=100000000)
    min_clicks: int = Field(default=20, ge=1, le=10000000)
    min_purchases: int = Field(default=20, ge=5, le=1000000)
    min_spend: Decimal = Field(default=Decimal("20"), ge=1, le=10000000, allow_inf_nan=False)
    alpha: Decimal = Field(default=Decimal("0.05"), ge=Decimal("0.001"), le=Decimal("0.1"), allow_inf_nan=False)
    attribution_lag_days: int = Field(default=8, ge=2, le=30)
    allocation: Literal["equal_budget", "observational"] = "equal_budget"
    max_spend_ratio: Decimal = Field(default=Decimal("2"), ge=1, le=4, allow_inf_nan=False)
    # Predeclared final analysis only; later data corrections append revisions.
    checkpoint: Literal["end_of_window"] = "end_of_window"


class FreezeRound(Document):
    start_date: date
    end_date: date
    account_timezone: str = Field(default="UTC", max_length=64)
    execution_mode: Literal["byok", "mock"]
    protocol: EvaluationProtocol
    attribution_window: AttributionWindow = '7d_click_1d_view'

    @model_validator(mode="after")
    def valid_period(self):
        if self.protocol.attribution_lag_days < MIN_ATTRIBUTION_LAG[self.attribution_window]:
            raise PydanticCustomError("marketing_attribution_lag_too_short", "归因等待期必须覆盖所选归因窗口及至少一天的回传缓冲")
        try:
            ZoneInfo(self.account_timezone)
        except (KeyError, ValueError) as exc:
            raise PydanticCustomError("marketing_timezone_invalid", "无效的广告账户时区") from exc
        if self.end_date < self.start_date or (self.end_date - self.start_date).days >= 90:
            raise PydanticCustomError("marketing_date_range_invalid", "投放日期范围无效")
        if (self.end_date - self.start_date).days + 1 < self.protocol.min_days:
            raise PydanticCustomError("marketing_duration_too_short", "投放周期少于预定最低完整天数")
        return self


class ReviewCreative(Document):
    # The single-image result (or a pre-split call that answered every check) and the paired result this review
    # confirms; a kind without its result is a human override that needs override_note to approve.
    ai_qc_id: str | None = Field(default=None, max_length=64)
    paired_ai_qc_id: str | None = Field(default=None, max_length=64)
    override_note: str = Field(default="", max_length=2000)
    asset_version_id: str = Field(min_length=1, max_length=64)
    quality: Literal["approved", "needs_changes", "rejected"]
    checks: dict[str, Literal["pass", "fail", "unknown"]] = Field(max_length=30)
    note: Text
    compared_asset_version_ids: list[str] = Field(default_factory=list, max_length=20)


class CreateHandoff(Document):
    asset_version_ids: list[str] = Field(min_length=1, max_length=60)


class BindDelivery(Document):
    entry_id: str = Field(min_length=1, max_length=64)
    ad_account_id: str = Field(min_length=1, max_length=64, pattern=r"^(act_)?[0-9]+$")
    ad_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+$")
    conditions_match: bool
    deviations: list[Text] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def deviations_explained(self):
        if not self.conditions_match and not self.deviations:
            raise PydanticCustomError("marketing_deviation_note_required", "请说明实际投放与方案的差异")
        return self


class PreviewMetrics(Document):
    csv_text: str = Field(min_length=1, max_length=262144)
    filename: str = Field(min_length=1, max_length=160)
    ad_account_id: str = Field(min_length=1, max_length=64, pattern=r"^(act_)?[0-9]+$")
    account_timezone: str = Field(max_length=64)
    currency: MediaCurrency
    attribution_window: AttributionWindow
    source_mode: Literal["customer_file", "controlled_mock"]
    field_mapping: dict[str, str] = Field(default_factory=dict, max_length=10)
    replaces_batch_id: str | None = Field(default=None, max_length=64)


class BatchCommand(Document):
    batch_id: str = Field(min_length=1, max_length=64)


class CloseRound(Document):
    reason: Literal["completed", "asset_only", "no_qualified", "insufficient_evidence", "user_stopped"]
    note: Text


class CopyChange(Document):
    """Words of the confirmed copy and what the image rendered in their place; either side may be empty."""
    expected: str
    rendered: str


class RenderedTextDifference(Document):
    """One copy field whose verbatim transcription differs from the confirmed text, or was not transcribed (`rendered` empty)."""
    field: Literal['headline', 'body_copy']
    expected: str
    rendered: str
    changes: list[CopyChange] = Field(default_factory=list)


class EffectiveFinding(AICheckFinding):
    model_status: Literal['pass', 'fail', 'unknown']
    gate_reason: str | None = None
    unsupported_claims: list[str] = Field(default_factory=list)
    # include_qc_evidence only: copy_consistent against the transcription (#294).
    rendered_text_diff: list[RenderedTextDifference] = Field(default_factory=list)


class ProviderReceipt(Document):
    usage: dict[str, int]
    request_id: str


class ImageEvidence(Document):
    asset_version_id: str
    mime: str
    sha256: str
    width: int
    height: int
    policy: str


class EffectiveReport(Document):
    summary: str
    findings: list[EffectiveFinding]
    source: Literal['ai_visual_suggestion', 'controlled_mock']
    normalization_version: str
    quality_suggestion: Literal['needs_changes', 'needs_confirmation', 'ready_to_confirm']
    human_approved: Literal[False]
    provider_receipt: ProviderReceipt | None = None
    image_evidence: list[ImageEvidence] = Field(default_factory=list)
    # include_qc_evidence only: single-image v3 calls transcribe the copy the image renders (#294).
    rendered_text: RenderedText | None = None


class AIQCErrorDetail(Document):
    """Why a returned output was refused (#295): an `error-<reason>` code, the field path, the validator's problem and a
    bounded excerpt with the hash and length of the whole output. Never the request, its headers or a credential."""
    reason: str
    field: str | None = None
    problem: str | None = None
    excerpt: str
    sha256: str
    length: int


class WorkflowAIQCCall(Document):
    id: str
    # include_qc_kinds only; `combined` is a call from before the split that checked everything at once.
    kind: Literal['single', 'paired', 'combined'] | None = None
    # include_languages only: the strategy language the call wrote its findings in (J-43); calls before that are zh-CN.
    language: Literal['zh-CN', 'en-US'] | None = None
    state: str
    provider: str
    model: str
    execution_mode: Literal['byok', 'mock']
    policy_version: str
    input_hash: str
    result: EffectiveReport | None
    error_code: str | None
    # include_qc_evidence only.
    error_detail: AIQCErrorDetail | None = None
    maximum_cost: str
    current: bool
    compared_asset_version_ids: list[str]
    product_reference_version_ids: list[str] = Field(default_factory=list)


class CheckOverride(Document):
    ai: Literal['pass', 'fail', 'unknown']
    human: Literal['pass', 'fail', 'unknown']


class CreativeQuality(ReviewCreative):
    source: Literal['human_semantic_review', 'ai_assisted_human_review']
    required_checks: dict[str, str]
    recipe_hash: str
    plan_hash: str
    ai_input_hash: str | None = None
    ai_policy_version: str | None = None
    normalization_version: str | None = None
    effective_findings: list[EffectiveFinding] = Field(default_factory=list)
    overrides: dict[str, CheckOverride] = Field(default_factory=dict)
    # include_qc_kinds only.
    paired_ai_input_hash: str | None = None
    paired_ai_policy_version: str | None = None
    qc_kinds: dict[Literal['single', 'paired'], Literal['ai', 'human']] | None = None


class WorkflowAsset(Document):
    parent_version_id: str | None = None
    asset_id: str | None = None
    created_at: str | None = None
    edit_operation: Literal['generated', 'crop_rotate', 'upscale', 'remove_background', 'erase', 'modify', 'reuse'] | None = None
    asset_version_id: str
    combination_id: str
    headline: str
    body_copy: str
    required_checks: dict[str, str]
    quality: CreativeQuality | None
    ready: bool
    mime_type: str
    ai_qc: WorkflowAIQCCall | None
    # include_qc_kinds only: the newest paired-check attempt (a pre-split call answers both kinds).
    paired_ai_qc: WorkflowAIQCCall | None = None


class CreativeProvenance(Document):
    image_tool: ImageToolSpec | None = None
    edit_id: str | None = None
    crop: dict[str, float] | None = None
    rotation: int | None = None
    algorithm: str | None = None
    kind: Literal['generated_for_plan', 'existing_creative', 'qc_feedback_revision', 'image_edit']
    asset_version_id: str | None = None
    asset_id: str | None = None
    product_id: str | None = None
    origin_kind: str | None = None
    execution_mode: str | None = None
    content_sha256: str | None = None
    original_prompt: str | None = None
    original_provider: str | None = None
    original_model: str | None = None
    plan_hash: str | None = None
    combination: CreativeCombination | None = None
    source_asset_version_id: str | None = None
    source_sha256: str | None = None
    qc_review_id: str | None = None
    ai_qc_id: str | None = None
    feedback_id: str | None = None
    feedback_hash: str | None = None


class ProductPhotoReference(Document):
    reference_version_id: str
    content_sha256: str
    content_hash: str
    source: str
    attestation: str


class ReferenceDelivery(Document):
    reference_delivery_policy: str | None = None
    reference_input_sha256: list[str] = Field(default_factory=list)


class ProductPhotoLineage(Document):
    policy: Literal['marketing-product-photos-v1'] | None = None
    reference_actor_id: str | None = None
    sent_to_provider: bool | None = None
    delivery: ReferenceDelivery | None = None
    references: list[ProductPhotoReference] = Field(default_factory=list)


class HandoffEntry(Document):
    entry_id: str
    asset_version_id: str
    group_id: str
    group_name: str
    combination_id: str
    angle_id: str
    expression_id: str
    proposition: str
    expression_name: str
    # Stable ASCII name for Meta, the same in every language (#255).
    ad_name: str
    # Readable name in the package language; absent on packages exported before #255.
    display_name: str | None = None
    headline: str
    body_copy: str
    content_sha256: str
    mime_type: str
    recipe_hash: str
    product_references: ProductPhotoLineage | None = None
    provenance: CreativeProvenance | None = None
    provider: str | None = None
    model: str | None = None


class WorkflowHandoff(Document):
    quality_current: bool = True
    id: str
    state: str
    error_code: str | None
    manifest_hash: str
    # The interface language the package was written in; absent before #255 (English).
    language: Literal["en", "zh-CN"] | None = None
    entries: list[HandoffEntry]


class DeliveryBinding(BindDelivery):
    id: str


class MetricsMetadata(Document):
    filename: str
    ad_account_id: str
    account_timezone: str
    currency: MediaCurrency
    attribution_window: AttributionWindow
    source_mode: Literal['customer_file', 'controlled_mock']
    field_mapping: dict[str, str]
    resolved_mapping: dict[str, str] = Field(default_factory=dict)


class MetricsIssue(Document):
    """One preflight finding; the browser words it from ``code`` (#255)."""
    row: int
    field: str
    code: str
    # Values for the wording, as declared in ``metrics.ISSUE_PARAMS``; absent on issues stored before #255.
    params: dict[str, str | int | list[str]] | None = None


class WorkflowBatch(Document):
    id: str
    state: str
    raw_hash: str
    metadata: MetricsMetadata
    row_count: int
    issues: list[MetricsIssue]
    replaces_batch_id: str | None


class DeliveryConditions(Document):
    audience: str
    market: str
    placement: str
    offer: str
    landing_page: str


class MetricTotals(Document):
    days: int
    impressions: int
    clicks: int
    spend: str
    purchases: str
    purchase_value: str


class EvaluationResult(Document):
    purchases_reported: bool = False
    id: str
    kind: Literal['group', 'asset']
    label: str
    entry_ids: list[str]
    entry_id: str | None
    metric: Literal['cpa', 'roas', 'ctr']
    estimate: str | None
    interval_low: str | None
    interval_high: str | None
    reason_codes: list[str]
    totals: MetricTotals
    business_status: Literal['passes', 'does_not_pass', 'uncertain', 'insufficient']
    conclusion_scope: Literal['asset', 'tested_group_of_combinations']


class ConfirmedCombination(Document):
    combination_id: str
    combination_key: str
    current_asset_version_id: str
    supporting_evaluations: list[str]
    scope: Literal['repeatable_under_recorded_conditions']


class WorkflowEvaluation(Document):
    current: bool = True
    id: str
    input_hash: str
    algorithm_version: Literal['marketing-eval-v1']
    metric: Literal['cpa', 'roas', 'ctr']
    threshold: str
    currency: MediaCurrency
    attribution_window: AttributionWindow
    account_timezone: str
    evidence_status: Literal['simulated', 'user_supplied_delivery', 'not_supplied']
    results: list[EvaluationResult]
    relative_status: Literal['not_compared', 'insufficient', 'no_clear_difference', 'separated_under_observed_conditions']
    relative_group_ids: list[str]
    qualified_asset_version_ids: list[str]
    confirmed_combinations: list[ConfirmedCombination]
    all_assets_failed: bool
    round_spend: str
    small_round_budget: str
    big_round_spend: str
    big_round_budget: str
    mature: bool
    next_action: str
    limitations: list[str]
    factor_conclusion: Literal['conditional_association_only', 'not_established']
    conditions: DeliveryConditions
    budget_exhausted: bool
    round_limit_reached: bool


class ExplanationFocus(Document):
    evidence_id: str
    interpretation: Literal['retain_asset', 'not_success', 'investigate', 'compare_scope', 'repeat_execution', 'close', 'explore_expression']


class WorkflowExplanationReport(Document):
    focuses: list[ExplanationFocus]


class ExplanationParagraph(Document):
    evidence_id: str
    object_id: str
    observation: str
    explanation: str


class ModalityTokenUsage(Document):
    modality: str
    tokenCount: int


class TextProviderReceipt(Document):
    request_id: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int | dict[str, int] | list[ModalityTokenUsage]] | None = None


class ExplanationData(Document):
    provider_receipt: TextProviderReceipt | None = None
    report: WorkflowExplanationReport
    paragraphs: list[ExplanationParagraph]
    evaluation_id: str
    input_hash: str
    result_hash: str
    question: str
    conditions: DeliveryConditions
    limitations: list[str]
    evidence_status: Literal['simulated', 'user_supplied_delivery', 'not_supplied']
    next_action: str
    confirmed_combinations: list[ConfirmedCombination]
    policy: Literal['marketing-explanation-v1']


class WorkflowExplanation(Document):
    id: str
    state: str
    # include_languages only: the strategy language the explanation was written in (J-43); calls before that are zh-CN.
    language: Literal['zh-CN', 'en-US'] | None = None
    execution_mode: Literal['byok', 'mock']
    provider: str
    model: str
    maximum_cost: str
    error_code: str | None
    current: bool
    data: ExplanationData | None


class FeedbackDraft(Document):
    image_tool: ImageToolSpec | None = None
    id: str
    draft_id: str
    source_asset_version_id: str
    combination_id: str
    request_note: str
    provider: str
    model: str


class RoundClose(CloseRound):
    evaluation_id: str | None
    plan_hash: str
    execution_mode: Literal['byok', 'mock']
    closed_at: str


class RoundSnapshot(Document):
    version: Literal[1]
    plan: FrozenTestPlan
    plan_hash: str
    understanding: Understanding
    protocol: EvaluationProtocol
    currency: MediaCurrency
    attribution_window: AttributionWindow


class RoundSummary(Document):
    id: str
    ordinal: int
    state: Literal['reviewing', 'handoff', 'awaiting_data', 'evaluated', 'closed']
    close: RoundClose | None


class RoundContinuation(Document):
    can_plan: bool
    remaining_budget: str
    remaining_rounds: int
    limit_reached: bool
    completed: bool


class WorkflowImageEdit(Document):
    id: str
    operation_id: str
    source_asset_version_id: str
    result_asset_version_id: str | None
    state: Literal['queued', 'succeeded', 'unchanged', 'failed']
    error_code: str | None


# Without include_qc_kinds the board keeps the J-11 names (`ai_qc` action, no *_running / paired_qc_done stages).
ExecutionStage = Literal['pending_generation', 'generating', 'single_qc_pending', 'single_qc_running', 'single_qc_done',
    'paired_qc_pending', 'paired_qc_running', 'paired_qc_done', 'ready', 'exported', 'bound', 'has_data', 'blocked']
ExecutionGap = Literal['pending_generation', 'single_qc', 'paired_qc', 'review', 'export', 'bind', 'data', 'none']
ExecutionAction = Literal['generate', 'single_qc', 'paired_qc', 'ai_qc', 'review', 'revise', 'export', 'bind', 'import', 'evaluate', 'none']


class BoardAIQC(Document):
    id: str
    state: str
    current: bool
    quality_suggestion: Literal['needs_changes', 'needs_confirmation', 'ready_to_confirm'] | None


class BoardDelivery(Document):
    entry_id: str
    ad_ids: list[str]
    has_data: bool


class BoardProductAppearance(Document):
    """Whether this execution's visible product appearance can be checked against frozen, current verified photos.

    `pending_verification` (外观待核实) keeps product_fidelity unknown. A photo or its verified status is never
    evidence for certification, hidden performance or ad effect."""
    status: Literal['reference_backed', 'pending_verification']
    reason: Literal['no_reference', 'reference_not_current'] | None
    reference_version_ids: list[str]


class BoardAppearanceSummary(Document):
    """Round-level product appearance; `pending_combination_ids` lists combinations with a pending execution."""
    status: Literal['reference_backed', 'partial', 'pending_verification']
    reference_version_ids: list[str]
    pending_combination_ids: list[str]


class BoardExecution(Document):
    """One image of a combination, or an ungenerated placeholder without an AssetVersion ID.

    `thumbnail` (include_board_thumbnails only) is the version's signed 512px derivative for the tile; null for
    placeholders and for versions without one, where the tile falls back to the original through the download intent.
    """
    asset_version_id: str | None
    execution_index: int = Field(ge=1)
    # Revisions keep their execution's index with the next version (①v2); include_execution_plan only.
    execution_version: int | None = Field(default=None, ge=1)
    parent_version_id: str | None
    duplicate_of: str | None
    angle_id: str
    expression_id: str
    combination_id: str
    group_id: str
    stage: ExecutionStage
    blocked_reason: Literal['quality_changes_requested', 'file_qc_failed', 'reference_not_current', 'review_withdrawn'] | None
    quality: Literal['approved', 'needs_changes', 'rejected'] | None
    ai_qc: BoardAIQC | None
    paired_ai_qc: BoardAIQC | None = None  # include_qc_kinds only
    delivery: BoardDelivery | None
    product_appearance: BoardProductAppearance
    compare_with: list[str]
    awaiting_combination_ids: list[str]
    gap: ExecutionGap
    next_action: ExecutionAction
    thumbnail: AssetThumbnailLink | None = None


class BoardCombination(Document):
    combination_id: str
    group_id: str
    group_name: str
    headline: str
    planned_executions: int | None = Field(default=None, ge=1)  # include_execution_plan only
    executions: list[BoardExecution]


class BoardExpression(Document):
    expression_id: str
    name: str
    visual_mechanism: str | None
    details_missing: bool
    shared: bool
    combinations: list[BoardCombination]


class BoardAngle(Document):
    angle_id: str
    proposition: str
    group_ids: list[str]
    expressions: list[BoardExpression]


class BoardTree(Document):
    angles: list[BoardAngle]


class BoardGroup(Document):
    id: str
    name: str
    angle_ids: list[str]
    combination_ids: list[str]


class BetweenGroups(Document):
    kind: Literal['between_groups']
    groups: list[BoardGroup]


class WithinAngle(Document):
    kind: Literal['within_angle']
    angle_id: str
    expression_ids: list[str]


class Retest(Document):
    kind: Literal['retest']
    original_asset_version_ids: list[str]
    new_execution_ids: list[str]


class FactorPair(Document):
    combination_ids: list[str]
    group_ids: list[str]


class FactorComparison(Document):
    kind: Literal['factor']
    changed: list[str]
    pairs: list[FactorPair]
    unpaired_combination_ids: list[str]


class SecondaryObservation(Document):
    kind: Literal['expression_coverage', 'same_angle']
    angle_id: str | None


class MatrixRow(Document):
    expression_id: str
    name: str
    visual_mechanism: str | None
    angle_ids: list[str]
    details_missing: bool
    shared: bool


class MatrixCol(Document):
    angle_id: str
    proposition: str


class BoardMatrix(Document):
    rows: list[MatrixRow]
    cols: list[MatrixCol]
    # cells[row][col]: the combinations arranged there, or null for untested (never a failure).
    cells: list[list[list[str] | None]]


class MarketingBoard(Document):
    version: Literal['marketing-board-v1']
    question: str
    stage: Literal['screen_angles', 'match_expressions', 'confirm_combination', 'optimize_elements']
    comparison_kind: Literal['whole_combinations', 'specific_factor', 'business_retest']
    primary_comparison: BetweenGroups | WithinAngle | Retest | FactorComparison
    secondary_observation: SecondaryObservation | None
    tree: BoardTree
    matrix: BoardMatrix
    product_appearance: BoardAppearanceSummary


class MarketingRound(RoundSummary):
    image_edits: list[WorkflowImageEdit] = Field(default_factory=list)
    continuation: RoundContinuation | None = None
    revision: int
    plan_revision: int
    execution_mode: Literal['byok', 'mock']
    snapshot: RoundSnapshot
    start_date: str
    end_date: str
    timezone: str
    assets: list[WorkflowAsset]
    handoff: WorkflowHandoff | None
    bindings: list[DeliveryBinding]
    batches: list[WorkflowBatch]
    evaluation: WorkflowEvaluation | None
    explanation: WorkflowExplanation | None
    feedback_drafts: list[FeedbackDraft]
    board: MarketingBoard | None = None


class ResourceEnvelope(Document):
    id: str
    data: MarketingRound


class WorkflowEnvelope(Document):
    project_id: str
    rounds: list[RoundSummary]
    current: MarketingRound | None
    next_action: str
    poll_after_ms: int = Field(default=0, ge=0)
