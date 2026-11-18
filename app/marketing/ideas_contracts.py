"""Ads-first ideas (ADR 0018): the ad content half of a test plan, and the ideas endpoints' shapes.

``Ideas`` holds what a ``TestPlan`` says about the ads themselves -- market, languages, buying claims,
ways of telling them and the ad combinations -- and none of the test settings (budget, goal, duration,
decision rules), which are asked for only when the user takes ads to a test (design doc §7).
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from app.generation.response_models import DownloadGrant
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


class RenderedCopyChange(Document):
    expected: str
    rendered: str


class RenderedCopyDifference(Document):
    """A headline or body text the image renders differently from the ad's copy (empty ``rendered``: not readable)."""
    field: Literal["headline", "body_copy"]
    expected: str
    rendered: str
    changes: list[RenderedCopyChange]


class IdeasCheckFinding(Document):
    """A check that needs attention: ``fail``, or ``unknown`` when the image alone cannot settle it. Passes are left out."""
    check_id: str
    # What the check asks, as the call was given it (``intent_n`` checks come from the ad's own checklist).
    description: str
    status: Literal["fail", "unknown"]
    confidence: Literal["high", "medium", "low"]
    # Set when the server, not the model, held the result back or failed it (e.g. ``rendered_text_mismatch``,
    # ``claim_evidence_missing``).
    gate_reason: str | None = None
    visual_evidence: str
    rationale: str
    suggested_fix: str
    fact_ids: list[str]
    unsupported_claims: list[str] = Field(default_factory=list)
    rendered_text_diff: list[RenderedCopyDifference] = Field(default_factory=list)


class IdeasAdCheck(Document):
    """The newest automatic check of one ad (#197): a suggestion, never a quality approval.

    It was made against the product understanding as it was when the image was made (``understanding_hash``)
    and the facts the ad cites (``fact_ids``); whether that still holds after the user checks the facts is
    for the ad's status to say."""
    id: str
    state: Literal["queued", "running", "succeeded", "failed", "uncertain"]
    execution_mode: Literal["byok", "mock"]
    # ``controlled_mock`` for the explicit example, ``ai_visual_suggestion`` for a real check; set once succeeded.
    source: Literal["ai_visual_suggestion", "controlled_mock"] | None = None
    suggestion: Literal["needs_changes", "needs_confirmation", "ready_to_confirm"] | None = None
    findings: list[IdeasCheckFinding]
    understanding_hash: str
    fact_ids: list[str]
    language: str
    maximum_cost: str
    error_code: str | None = None
    created_at: str


#: Why an ad is not exportable (#198). The first five mean ``needs_redo``; the others leave it a ``draft``.
#: ``ideas_changed``: the ideas no longer hold this ad's content under its combination. ``facts_changed`` /
#: ``facts_removed``: a cited fact was changed or removed since the image was made. ``photo_unavailable``:
#: a product photo it used may no longer be used. ``image_blocked``: the image's own generation check
#: stopped it. ``facts_unverified``: cited facts the user has not checked. ``photo_unverified``: a photo it
#: used is not checked. ``appearance_unconfirmed``: made without a product photo, and the user has not
#: confirmed the product looks right. ``image_check_in_progress``: the generation check is still running.
#: ``check_missing`` / ``check_in_progress`` / ``check_failed`` / ``check_uncertain``: the automatic check
#: has no result. ``findings_open``: findings the user has not handled. ``check_outdated``: the check was
#: made against facts that have changed since.
AdReasonCode = Literal["ideas_changed", "facts_changed", "facts_removed", "photo_unavailable", "image_blocked",
                       "facts_unverified", "photo_unverified", "appearance_unconfirmed", "image_check_in_progress",
                       "check_missing", "check_in_progress", "check_failed", "check_uncertain", "findings_open",
                       "check_outdated"]


class AdReason(Document):
    code: AdReasonCode
    # The facts concerned (``facts_*``).
    fact_ids: list[str] = Field(default_factory=list)
    # ``IdeasCheckFinding.check_id`` of each finding still to handle (``findings_open``).
    finding_ids: list[str] = Field(default_factory=list)


class AdSummary(Document):
    asset_version_id: str
    combination_id: str
    angle_id: str
    expression_id: str
    # Derived from the facts, the photos and the check as they are now, never stored (ADR 0018 §3, #198).
    status: Literal["draft", "exportable", "needs_redo"]
    # Why the ad is not exportable; empty when it is.
    reasons: list[AdReason] = Field(default_factory=list)
    # The facts the ad stands on, as its image was made: its claim's and way of telling's, and the facts
    # behind an inference the claim cites -- so sometimes more than the card shows (#211).
    cited_fact_ids: list[str] = Field(default_factory=list)
    # Made without a product photo, and the user confirmed the product in the image looks right.
    appearance_confirmed: bool = False
    kept: bool
    # Whether the image request sent a product photo; the ideas page marks one made without (#196).
    used_product_photo: bool
    created_at: str
    # The newest automatic check of this image, if one was started (#197).
    check: IdeasAdCheck | None = None


class PendingAd(Document):
    combination_id: str
    state: Literal["queued", "running", "failed"]


class IdeasCallSummary(PlanningCallSummary):
    """A drafting call of ads-first ideas: the product understanding, then the ideas (#190).

    The plan-first ``PlanningCallSummary`` keeps its own kinds; an ads-first project never has a plan call."""
    kind: Literal["understanding", "ideas"]


#: ``checking``: every image is made (they are on the ideas page) and each is being checked (#197).
FirstBatchState = Literal["reading", "understanding", "ideas", "generating", "checking", "done", "needs_attention", "failed"]
FirstBatchStep = Literal["reading", "understanding", "ideas", "images", "checks"]
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


class IdeasChecksSkipped(Document):
    """An ad that cannot be checked as it was made, and why (e.g. ``product_reference_changed``)."""
    asset_version_id: str
    code: str


class IdeasChecksQuote(Document):
    """What checking these ideas' unchecked ads costs at most (#197): one single-image check per image.

    Only images made in ``execution_mode`` are counted: a Mock image gets the Mock check, a real one a real
    check. An image with a check queued, running or uncertain, or with a finished one, is not counted again."""
    available: bool
    unavailable_reason: str | None = None
    execution_mode: Literal["byok", "mock"]
    provider: str
    model: str
    quantity: int
    maximum_cost: str
    currency: Literal["USD"] = "USD"
    quote_hash: str
    asset_version_ids: list[str]
    skipped: list[IdeasChecksSkipped]


class StartIdeasChecks(Document):
    """Check the ads the quote counted, confirming its maximum."""
    execution_mode: Literal["byok", "mock"]
    quote_hash: str = Field(min_length=64, max_length=64)


class FirstBatchQuoteStep(Document):
    step: Literal["reading", "understanding", "ideas", "ideas_retry", "images", "checks"]
    quantity: int
    unit_maximum: str
    maximum_cost: str


class FirstBatchQuote(Document):
    """The start button's maximum (#193): every step's maximum, the images' checks (#197) included, plus one
    more drafting of the ideas.

    Step 1 runs on one OpenAI key; without one the quote is unavailable with ``openai_key_required``. When
    that key is all that is missing, ``maximum_cost`` and the steps still say what starting will cost once
    it is saved (#216); with anything else in the way too, ``maximum_cost`` is ``"0"``."""
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
    # ``step_failed`` or ``uncertain`` while making or checking images, ``over_budget`` while checking: the ads concerned.
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


class EditIdeasUnderstanding(Document):
    """Change what the AI read about the product (#198): facts, guesses and unknowns, as one document.

    ``confirmation`` in the document is ignored: a fact is checked only by ``verify_marketing_ideas``. A
    fact that reads as before keeps its checking; a changed or new fact is unchecked. Leaving a fact out
    removes it. ``source_addition`` adds material for a new quote to the product's source."""
    understanding: Understanding
    source_addition: str | None = Field(default=None, max_length=8000)


class AcceptedFindings(Document):
    """Findings of one ad's check the user accepts as they are ("this is fine")."""
    asset_version_id: str = Field(min_length=1, max_length=64)
    # ``IdeasAdCheck.id``: the check the user looked at. A newer check has to be looked at again.
    check_id: str = Field(min_length=1, max_length=64)
    # ``IdeasCheckFinding.check_id`` of each accepted finding.
    finding_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=40)


class VerifyIdeas(Document):
    """One "check before download" (design doc §5.3): what the user confirms, and nothing they did not see."""
    # Facts the user read and confirms.
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    # Ads made without a product photo: the user confirms the product in the image looks right.
    appearance_confirmed: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=60)
    accepted_findings: list[AcceptedFindings] = Field(default_factory=list, max_length=60)


class DownloadIdeas(Document):
    """The ads to download: every one must be exportable, and only these are handed out."""
    asset_version_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.asset_version_ids)) != len(self.asset_version_ids):
            raise PydanticCustomError("marketing_download_ads_duplicate", "同一张广告只能列一次")
        return self


class IdeasAdDownload(Document):
    asset_version_id: str
    combination_id: str
    # Carries the ad's printable code, so an ad name in a later report can be matched to this image.
    filename: str
    intent_id: str
    expires_at: str
    download_grant: DownloadGrant


class IdeasDownload(Document):
    """One download of ads-first ads: a short-lived signed link per requested ad, in the order asked."""
    id: str
    project_id: str
    downloads: list[IdeasAdDownload]
