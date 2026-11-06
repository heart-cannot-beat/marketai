"""Versioned, validated planning documents, never an untyped tag matrix."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError
from .reporting import MediaCurrency, AttributionWindow, MEDIA_CURRENCIES, ATTRIBUTION_WINDOWS, MIN_ATTRIBUTION_LAG
from .expression_catalog import valid_mechanism
from .executions import LEGACY as LEGACY_EXECUTIONS

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Fact(Document):
    id: Identifier
    statement: Text
    source_quote: Text
    confirmation: Literal["unconfirmed", "confirmed"] = "unconfirmed"


class Inference(Document):
    id: Identifier
    statement: Text
    rationale: Text
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=30)


class Understanding(Document):
    product_name: Annotated[str, Field(min_length=1, max_length=160)]
    category_need: Text
    product_choice_reason: Text
    audience: Text
    situations: list[Text] = Field(min_length=1, max_length=20)
    objections: list[Text] = Field(default_factory=list, max_length=20)
    facts: list[Fact] = Field(default_factory=list, max_length=30)
    inferences: list[Inference] = Field(min_length=1, max_length=30)
    unknowns: list[Text] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def references(self):
        ids = [f.id for f in self.facts] + [i.id for i in self.inferences]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError("marketing_fact_ids_duplicate", "事实与推测的标识不能重复")
        facts = {f.id for f in self.facts}
        if any(set(i.fact_ids) - facts for i in self.inferences):
            raise PydanticCustomError("marketing_inference_fact_unknown", "推测引用了不存在的事实")
        return self


class EvidenceBasis(Document):
    """Confirmed-understanding facts, or an explicit gap; inferences are not evidence."""
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    gap: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def stated(self):
        if not self.fact_ids and not self.gap:
            raise PydanticCustomError("evidence_basis_required", "请引用支持该表达的事实，或说明证据缺口")
        return self


class Expression(Document):
    id: Identifier
    name: Text
    description: Text
    fit_reason: Text
    required_evidence: list[Text] = Field(default_factory=list, max_length=20)
    how_to_tell: Text
    visual_mechanism: Annotated[str, Field(min_length=1, max_length=120)]
    evidence_basis: EvidenceBasis

    @field_validator("visual_mechanism")
    @classmethod
    def catalog_or_other(cls, value: str):
        if not valid_mechanism(value):
            raise PydanticCustomError("visual_mechanism_invalid", "画面机制请使用表达知识库标签，或以 other: 开头说明新的画面机制")
        return value


class FrozenExpression(Expression):
    """Plans drafted before expression distinctness lack the three elements."""
    how_to_tell: str | None = None
    visual_mechanism: str | None = None
    evidence_basis: EvidenceBasis | None = None

    @field_validator("visual_mechanism")
    @classmethod
    def catalog_or_other(cls, value):
        return value  # historical labels stay readable as the catalog evolves


class Angle(Document):
    id: Identifier
    proposition: Text
    rationale: Text
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    inference_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    expressions: list[Expression] = Field(min_length=1, max_length=8)
    # The cited competitor snapshot's claim clusters making this buying claim: AI inference (spec §7.2).
    competitor_overlap: list[Identifier] = Field(default_factory=list, max_length=20)


class FrozenAngle(Angle):
    expressions: list[FrozenExpression] = Field(min_length=1, max_length=8)


COMPETITOR_KINDS = ("competitor_crowded", "competitor_gap", "competitor_copy_reuse")


class ReviewObservation(Document):
    """A competitor observation a system finding points at: what others publicly run, never evidence (ADR 0014)."""
    ad_id: Identifier
    cluster_id: Identifier | None = None
    advertiser_name: str = Field(default="", max_length=300)
    library_url: str = Field(default="", max_length=2000)
    days_running: int | None = None


class ReviewItem(Document):
    """A pre-generation finding. System items are recomputed deterministically."""
    id: Identifier
    kind: Literal["angle_overlap", "expression_duplicate", "evidence_gap", "budget_insufficient", "image_brief_stale",
                  "competitor_crowded", "competitor_gap", "competitor_copy_reuse"]
    severity: Literal["blocking", "advisory"]
    target_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    finding: Text
    suggestion: Text
    source: Literal["ai", "system"] = "ai"
    resolved: bool = False
    # Competitor findings only: the observations in the cited snapshot they point at.
    observations: list[ReviewObservation] = Field(default_factory=list, max_length=10)


Option = Annotated[str, Field(min_length=1, max_length=300)]


class GuidingQuestion(Document):
    id: Identifier
    question: Text
    options: list[Option] = Field(min_length=2, max_length=6)
    multiple: bool = False
    why_needed: Text
    selected: list[Option] = Field(default_factory=list, max_length=6)
    custom_answer: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def answer_from_options(self):
        if set(self.selected) - set(self.options) or (not self.multiple and len(self.selected) > 1):
            raise PydanticCustomError("guiding_answer_invalid", "请从选项中作答；单选问题只能选择一项")
        return self

    @property
    def answered(self):
        return bool(self.selected or self.custom_answer)


class ImageBrief(Document):
    """English instructions for the image model, separate from strategy prose."""
    proposition: Text
    expression: Text
    hold_constant: list[Text] = Field(min_length=1, max_length=20)
    checks: list[Text] = Field(min_length=1, max_length=12)


class CreativeCombination(Document):
    id: Identifier
    angle_id: Identifier
    expression_id: Identifier
    elements: dict[str, Annotated[str, Field(max_length=1000)]] = Field(default_factory=dict, max_length=20)
    headline: Text
    body_copy: Text
    visual_brief: Text
    image_brief: ImageBrief | None = None
    checks: list[Text] = Field(min_length=1, max_length=12)
    # Overrides the plan's executions_per_combination for this combination only.
    executions: int | None = Field(default=None, ge=1, le=4)


class TestGroup(Document):
    id: Identifier
    name: Text
    combination_ids: list[Identifier] = Field(min_length=1, max_length=30)
    rationale: Text
    # The group's own visual style, written only when style_variation=tested.
    style: str = Field(default="", max_length=300)


HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
BrandRule = Annotated[str, Field(min_length=1, max_length=120)]
FontName = Annotated[str, Field(min_length=1, max_length=60)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BrandFonts(Document):
    primary: FontName
    secondary: str = Field(default="", max_length=60)


class ReadabilityRules(Document):
    min_text_height_ratio: float = Field(default=0.03, ge=0.01, le=0.2)
    min_contrast_ratio: float = Field(default=4.5, ge=3, le=21)
    safe_area_ratio: float = Field(default=0.05, ge=0, le=0.2)


class BrandLogoInput(Document):
    """A product reference item the product settings marked role=logo (#226)."""
    item_id: Identifier
    placement: Literal["top_left", "top_right", "top_center", "bottom_left", "bottom_right", "bottom_center"] = "top_left"


class BrandLogo(BrandLogoInput):
    reference_version_id: Identifier
    reference_object_id: Identifier
    content_sha256: Sha256


class BrandKitInput(Document):
    logo: BrandLogoInput | None = None
    fonts: BrandFonts
    palette: list[HexColor] = Field(min_length=1, max_length=5)
    readability_rules: ReadabilityRules = Field(default_factory=ReadabilityRules)
    forbidden: list[BrandRule] = Field(default_factory=list, max_length=8)

    @field_validator("palette")
    @classmethod
    def distinct_colors(cls, value: list[str]):
        value = [color.upper() for color in value]
        if len(set(value)) != len(value):
            raise PydanticCustomError("brand_palette_duplicate", "品牌配色不能重复")
        return value


class BrandKitContent(BrandKitInput):
    """The immutable stored version: the logo is resolved to its exact reference identity."""
    logo: BrandLogo | None = None


class BrandKitRef(Document):
    """An exact, immutable brand baseline version that a plan and its recipes freeze."""
    id: Identifier
    product_id: Identifier
    version: int = Field(ge=1)
    content_hash: Sha256


class BrandKitVersion(Document):
    id: str
    product_id: str
    version: int
    content_hash: str
    content: BrandKitContent
    created_by: str | None = None
    created_at: str
    logo_available: bool


class ProductBrandKit(Document):
    product_id: str
    revision: int
    current: BrandKitVersion | None = None


class BrandKitSummary(Document):
    product_id: str
    product_name: str
    current: BrandKitVersion


class BrandKitList(Document):
    items: list[BrandKitSummary]


class DecisionRules(Document):
    business_metric: Literal["cpa", "roas", "ctr"]
    threshold: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=6)
    business_goal: Text
    evidence_requirements: Text
    evaluation_schedule: Text
    stop_rule: Text
    max_small_rounds: int = Field(ge=1, le=20)


class TestPlan(Document):
    stage: Literal["screen_angles", "match_expressions", "confirm_combination", "optimize_elements"]
    question: Text
    comparison_kind: Literal["whole_combinations", "specific_factor", "business_retest"]
    changed: list[Text] = Field(min_length=1, max_length=20)
    allowed_changes: list[Text] = Field(default_factory=list, max_length=20)
    held_constant: list[Text] = Field(min_length=1, max_length=20)
    limitations: list[Text] = Field(min_length=1, max_length=20)
    limited_expression_coverage: bool = False
    conclusion_scope: Literal["asset", "combination", "specific_factor"]
    audience: Text
    market: Text
    ad_language: Annotated[str, Field(min_length=2, max_length=35, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")]
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    language_confirmation_required: bool = False
    placement: Text
    offer: Text
    landing_page: Text
    baseline: Text
    currency: MediaCurrency = "USD"
    media_budget: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=2)
    small_round_budget: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=2)
    duration_days: int = Field(ge=1, le=90)
    allocation: Text
    rules: DecisionRules
    # Images generated per combination; several executions of one expression check execution stability.
    executions_per_combination: int = Field(default=2, ge=1, le=4)
    angles: list[Angle] = Field(min_length=1, max_length=8)
    combinations: list[CreativeCombination] = Field(min_length=1, max_length=30)
    groups: list[TestGroup] = Field(min_length=1, max_length=12)
    plan_review: list[ReviewItem] = Field(max_length=20)
    guiding_questions: list[GuidingQuestion] = Field(max_length=3)
    # Whether visual style is a held condition or an actively tested change (ADR 0013).
    style_variation: Literal["held", "tested"] = "held"
    style_notes: str = Field(default="", max_length=500)
    brand_kit: BrandKitRef | None = None
    # The competitor snapshot the draft read (spec §7.2), set by the server: same product, same market.
    competitor_snapshot_id: Identifier | None = None

    @field_validator("small_round_budget")
    @classmethod
    def budget_within_media_budget(cls, value: Decimal, info: ValidationInfo):
        if "media_budget" in info.data and value > info.data["media_budget"]:
            raise PydanticCustomError("small_round_budget_exceeds_media_budget", "小轮预算不能超过大轮预算")
        return value

    @model_validator(mode="after")
    def relationships(self):
        if self.comparison_kind != "business_retest" and len(self.groups) < 2:
            raise PydanticCustomError("marketing_groups_too_few", "比较至少需要两个测试组")
        if self.comparison_kind != "specific_factor" and self.conclusion_scope == "specific_factor":
            raise PydanticCustomError("marketing_conclusion_scope_invalid", "完整组合比较不能承诺单因素结论")
        if self.comparison_kind == "specific_factor" and len(self.changed) != 1:
            raise PydanticCustomError("marketing_changed_factor_required", "特定因素比较必须明确一个主动改变的因素，其余变化单列为配套变化")
        if self.rules.business_metric == "ctr" and self.rules.threshold > 1:
            raise PydanticCustomError("marketing_ctr_threshold_invalid", "CTR 阈值必须使用 0 到 1 之间的比例")
        ids = [a.id for a in self.angles] + [c.id for c in self.combinations] + [g.id for g in self.groups]
        expressions = {}
        for angle in self.angles:
            if len({e.id for e in angle.expressions}) != len(angle.expressions):
                raise PydanticCustomError("marketing_expression_duplicate", "同一角度不能重复列出同一种表达")
            for expression in angle.expressions:
                shared = expression.model_dump(exclude={"fit_reason"})
                if expression.id in expressions and expressions[expression.id] != shared:
                    raise PydanticCustomError("marketing_shared_expression_mismatch", "共享表达的名称、讲法、画面机制和证据依据必须一致，适配理由按角度分别说明")
                expressions[expression.id] = shared
        if len(set(ids)) != len(ids) or set(ids) & expressions.keys():
            raise PydanticCustomError("marketing_plan_ids_duplicate", "方案对象标识必须唯一")
        checks = [r.id for r in self.plan_review] + [q.id for q in self.guiding_questions]
        if len(set(checks)) != len(checks):
            raise PydanticCustomError("marketing_check_ids_duplicate", "检查项与引导问题的标识不能重复")
        # Editing can remove a reviewed object; the finding stays visible without a dangling reference.
        known = set(ids) | expressions.keys()
        for review in self.plan_review:
            review.target_ids = [target for target in review.target_ids if target in known]
        angles = {a.id: a for a in self.angles}
        for c in self.combinations:
            angle = angles.get(c.angle_id)
            if not angle or c.expression_id not in {e.id for e in angle.expressions}:
                raise PydanticCustomError("marketing_combination_invalid", "创意组合的角度与表达关系无效")
        members = [c for g in self.groups for c in g.combination_ids]
        if len(members) != len(set(members)) or set(members) != {c.id for c in self.combinations}:
            raise PydanticCustomError("marketing_group_membership_invalid", "每个组合必须明确归属一个测试组，不能遗漏或重复")
        if self.stage == "screen_angles":
            for angle in self.angles:
                selected = {c.expression_id for c in self.combinations if c.angle_id == angle.id}
                if len(selected) < 2 and not self.limited_expression_coverage:
                    raise PydanticCustomError("marketing_expression_coverage_required", "筛选角度需多个表达；预算仅支持单一表达时必须说明局限")
        return self


class FrozenTestGroup(TestGroup):
    style: str | None = None


class FrozenTestPlan(TestPlan):
    """Historical snapshots may predate language, expression and style fields; never backfill evidence."""
    ad_language: str | None = None
    executions_per_combination: int = Field(default=LEGACY_EXECUTIONS, ge=1, le=4)  # plans before #225 generated one image
    angles: list[FrozenAngle] = Field(min_length=1, max_length=8)
    groups: list[FrozenTestGroup] = Field(min_length=1, max_length=12)
    plan_review: list[ReviewItem] = Field(default_factory=list, max_length=20)
    guiding_questions: list[GuidingQuestion] = Field(default_factory=list, max_length=3)
    style_variation: Literal["held", "tested"] | None = None
    style_notes: str | None = None


class PlanningCallSummary(Document):
    id: str
    operation_id: str
    kind: Literal["understanding", "plan"]
    state: str
    execution_mode: Literal["byok", "mock"]
    error_code: str | None = None
    maximum_cost: str = '0'


class ProductReferenceSelection(Document):
    reference_version_ids: list[Identifier] = Field(default_factory=list, max_length=3)
    source: str = Field(default="", max_length=1200)
    confirms_real_product: bool = False


class MarketingProjectDetail(Document):
    id: str
    revision: int
    state: Literal["input", "understanding_draft", "understanding_confirmed", "plan_draft", "plan_confirmed"]
    source_text: str
    product_id: str | None
    understanding: Understanding | None
    plan: FrozenTestPlan | None
    language_support: Literal["v1"] | None = None
    plan_review_support: Literal["v1"] | None = None
    brand_style_support: Literal["v1"] | None = None
    # The exact brand baseline version the plan references, never the product's current one.
    brand_kit: BrandKitVersion | None = None
    competitor_review_support: Literal["v1"] | None = None
    # A newer snapshot of the same product and market with another hash exists (spec §7.2).
    competitor_snapshot_stale: bool | None = None
    # The plan's market names no country, so drafting cites no snapshot.
    competitor_market_unmatched: bool | None = None
    evidence_status: Literal["unvalidated"] = "unvalidated"
    latest_call: PlanningCallSummary | None
    generation_draft_ids: list[str]
    product_references: ProductReferenceSelection = Field(default_factory=ProductReferenceSelection)


class MarketingProjectPage(Document):
    items: list["MarketingProjectSummary"]
    next_cursor: str | None = None


class MarketingProjectSummary(Document):
    id: str
    revision: int
    state: str
    product_name: str


class PlannerCapability(Document):
    provider: str
    model: str
    available: bool
    unavailable_reason: str | None
    maximum_cost: str
    currency: Literal["USD"] = "USD"
    quote_hash: str


class PlannerCapabilities(Document):
    paid: PlannerCapability
    mock_available: Literal[True] = True
    media_currencies: list[MediaCurrency] = Field(default_factory=lambda: MEDIA_CURRENCIES.copy())
    attribution_windows: list[AttributionWindow] = Field(default_factory=lambda: ATTRIBUTION_WINDOWS.copy())
    attribution_min_lag_days: dict[str, int] = Field(default_factory=lambda: MIN_ATTRIBUTION_LAG.copy())
