"""Ads-first ideas (ADR 0018): the ad content half of a test plan, and the ideas endpoints' shapes.

``Ideas`` holds what a ``TestPlan`` says about the ads themselves -- market, languages, buying claims,
ways of telling them and the ad combinations -- and none of the test settings (budget, goal, duration,
decision rules), which are asked for only when the user takes ads to a test (design doc §7).
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from .contracts import (Angle, BrandKitRef, CreativeCombination, Document, GuidingQuestion, Identifier,
                        PlanningCallSummary, Text, Understanding)

AdLanguage = Annotated[str, Field(min_length=2, max_length=35, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")]
IdeasState = Literal["input", "understanding_draft", "ideas_ready"]


class Ideas(Document):
    market: Text
    ad_language: AdLanguage
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    angles: list[Angle] = Field(min_length=2, max_length=8)
    combinations: list[CreativeCombination] = Field(min_length=2, max_length=30)
    style_notes: str = Field(default="", max_length=500)
    brand_kit: BrandKitRef | None = None
    competitor_snapshot_id: Identifier | None = None
    guiding_questions: list[GuidingQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def relationships(self):
        ids = [a.id for a in self.angles] + [c.id for c in self.combinations]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError("marketing_plan_ids_duplicate", "方案对象标识必须唯一")
        expressions = {}
        for angle in self.angles:
            own = [e.id for e in angle.expressions]
            if len(own) != len(set(own)):
                raise PydanticCustomError("marketing_expression_duplicate", "同一角度不能重复列出同一种表达")
            expressions[angle.id] = set(own)
        for combination in self.combinations:
            if combination.expression_id not in expressions.get(combination.angle_id, ()):
                raise PydanticCustomError("marketing_combination_invalid", "创意组合的角度与表达关系无效")
        used = {(c.angle_id, c.expression_id) for c in self.combinations}
        if any((angle_id, e) not in used for angle_id, own in expressions.items() for e in own):
            raise PydanticCustomError("marketing_expression_unused", "每种讲法至少要有一张广告")
        return self


class AdSummary(Document):
    asset_version_id: str
    combination_id: str
    angle_id: str
    expression_id: str
    # Step 1 only ever reports "draft"; S10 derives "exportable" and "needs_redo" (ADR 0018 §3).
    status: Literal["draft", "exportable", "needs_redo"]
    kept: bool
    created_at: str


class PendingAd(Document):
    combination_id: str
    state: Literal["queued", "running", "failed"]


class IdeasCallSummary(PlanningCallSummary):
    """A drafting call of ads-first ideas: the product understanding, then the ideas (#190).

    The plan-first ``PlanningCallSummary`` keeps its own kinds; an ads-first project never has a plan call."""
    kind: Literal["understanding", "ideas"]


FirstBatchState = Literal["reading", "understanding", "ideas", "generating", "done", "needs_attention", "failed"]
FirstBatchStep = Literal["reading", "understanding", "ideas", "images"]
#: Why a first batch stopped (#193). ``page_unreadable`` and ``reading_failed`` end it; the others wait for the user.
FirstBatchStopReason = Literal["several_models", "page_unreadable", "reading_failed", "too_few_claims", "over_budget",
                               "ideas_refused", "step_failed", "uncertain"]


class FirstBatchSummary(Document):
    """The product's first batch as the overview reads it: whether it stopped and waits for the user."""
    id: str
    state: FirstBatchState
    needs_attention: bool
    reason: FirstBatchStopReason | None = None


class IdeasProjectDetail(Document):
    id: str
    revision: int
    state: IdeasState
    product_id: str
    market: str
    ad_language: str
    understanding: Understanding | None
    ideas: Ideas | None
    latest_call: IdeasCallSummary | None
    ads: list[AdSummary]
    pending: list[PendingAd]
    # The first batch that opened these ideas, if one did (#193).
    first_batch: FirstBatchSummary | None = None


class IdeasProjectSummary(Document):
    id: str
    revision: int
    state: str
    product_id: str
    product_name: str
    ad_count: int
    kept_count: int
    updated_at: str
    first_batch: FirstBatchSummary | None = None


class IdeasProjectPage(Document):
    items: list[IdeasProjectSummary]
    next_cursor: str | None = None


class CreateIdeas(Document):
    product_id: str = Field(min_length=1, max_length=64)
    source_text: str = Field(min_length=1, max_length=16000)
    market: str = Field(min_length=1, max_length=80)
    ad_language: AdLanguage


class SetKept(Document):
    kept: bool


class PrepareIdeasGeneration(Document):
    """Image settings for the ads of these ideas: one generation draft per combination (#191).

    The product's verified photos are the appearance references; choosing photos comes later (S11)."""
    provider: Literal["gemini", "openai", "seedream", "mock"]
    model: str = Field(min_length=1, max_length=200)
    aspect_ratio: Literal["1:1", "4:5", "9:16", "16:9"] = "4:5"
    image_size: Literal["1K", "2K"] = "1K"
    # Images per way of telling; the first batch makes one (step-1 plan, Global Constraints).
    variants: int = Field(default=1, ge=1, le=4)


class IdeasGenerationDraft(Document):
    combination_id: str
    draft_id: str


class IdeasGenerationDrafts(Document):
    """The drafts prepared for these ideas at ``revision``; preview and submit each with the generation endpoints."""
    project_id: str
    revision: int
    drafts: list[IdeasGenerationDraft]


class StartIdeasPlanning(Document):
    """Draft the product understanding, or the ideas from it; no fact needs checking first (ADR 0018)."""
    kind: Literal["understanding", "ideas"]
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    execution_mode: Literal["byok", "mock"]
    quote_hash: str = Field(default="", max_length=64)


class FirstBatchQuoteStep(Document):
    step: Literal["reading", "understanding", "ideas", "ideas_retry", "images"]
    quantity: int
    unit_maximum: str
    maximum_cost: str


class FirstBatchQuote(Document):
    """The start button's maximum (#193): every step's maximum plus one more drafting of the ideas.

    Step 1 runs on one OpenAI key; without one the quote is unavailable with ``openai_key_required``."""
    available: bool
    unavailable_reason: str | None = None
    execution_mode: Literal["byok", "mock"]
    maximum_cost: str
    currency: Literal["USD"]
    quote_hash: str
    image_provider: str
    image_model: str
    steps: list[FirstBatchQuoteStep]


class StartFirstBatch(Document):
    """Paste a link, images or text and start: read, draft and make the first ads under one confirmed maximum."""
    url: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, max_length=20000)
    upload_intent_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=12)
    market: str = Field(min_length=1, max_length=80)
    ad_language: AdLanguage
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    execution_mode: Literal["byok", "mock"] = "byok"
    # The quote the user confirmed by clicking the start button.
    quote_hash: str = Field(min_length=1, max_length=64)


class FirstBatchAttention(Document):
    reason: FirstBatchStopReason
    step: FirstBatchStep
    code: str | None = None
    # ``several_models``: the models the page lists, one to choose.
    variants: list[str] = Field(default_factory=list)
    # ``step_failed`` or ``uncertain`` while making images: the ads concerned.
    combination_ids: list[str] = Field(default_factory=list)
    # ``over_budget``: what the reserved total would have become.
    needed: str | None = None
    call_id: str | None = None
    # Whether this stop is continued on this batch (``resume``); otherwise start again.
    resumable: bool


class FirstBatchResume(Document):
    """What continuing from a stop needs and costs at most; continuing confirms it like the start button."""
    maximum_cost: str
    additional_cost: str
    currency: Literal["USD"]
    quote_hash: str
    requires: list[Literal["selected_variant", "additional_text"]]


class FirstBatchDetail(Document):
    id: str
    operation_id: str
    state: FirstBatchState
    needs_attention: bool
    attention: FirstBatchAttention | None
    resume: FirstBatchResume | None
    intake_id: str
    product_id: str | None
    project_id: str | None
    market: str
    ad_language: str
    strategy_language: Literal["zh-CN", "en-US"]
    execution_mode: Literal["byok", "mock"]
    selected_variant: str | None
    confirmed_max_cost: str
    reserved_total: str
    currency: Literal["USD"]
    image_provider: str
    image_model: str
    # Ads the drafted ideas call for: 6, or 4 when the material supports only 2 claims; ``None`` before.
    ads_planned: int | None
    created_at: str
    updated_at: str


class ResumeFirstBatch(Document):
    """Continue a stopped first batch, confirming the maximum its ``resume`` offers."""
    quote_hash: str = Field(min_length=1, max_length=64)
    selected_variant: str | None = Field(default=None, min_length=1, max_length=200)
    additional_text: str | None = Field(default=None, min_length=1, max_length=16000)
