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


class IdeasProjectDetail(Document):
    id: str
    revision: int
    state: IdeasState
    product_id: str
    market: str
    ad_language: str
    understanding: Understanding | None
    ideas: Ideas | None
    latest_call: PlanningCallSummary | None
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
