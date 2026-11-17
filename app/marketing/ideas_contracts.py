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
    # Whether the image request sent a product photo; the ideas page marks one made without (#196).
    used_product_photo: bool
    created_at: str


class PendingAd(Document):
    combination_id: str
    state: Literal["queued", "running", "failed"]


class IdeasCallSummary(PlanningCallSummary):
    """A drafting call of ads-first ideas: the product understanding, then the ideas (#190).

    The plan-first ``PlanningCallSummary`` keeps its own kinds; an ads-first project never has a plan call."""
    kind: Literal["understanding", "ideas"]


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


class IdeasProjectSummary(Document):
    id: str
    revision: int
    state: str
    product_id: str
    product_name: str
    ad_count: int
    kept_count: int
    updated_at: str


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
