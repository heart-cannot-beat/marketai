"""Ads-first ideas (ADR 0018): the ad content half of a test plan, and the ideas endpoints' shapes.

``Ideas`` holds what a ``TestPlan`` says about the ads themselves -- market, languages, buying claims,
ways of telling them and the ad combinations -- and none of the test settings (budget, goal, duration,
decision rules), which are asked for only when the user takes ads to a test (design doc §7).
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from app.generation.image_tools import ImageToolSpec
from app.generation.response_models import DownloadGrant
from .contracts import (Angle, BrandKitRef, CreativeCombination, Document, Expression, GuidingQuestion, Identifier,
                        PlanningCallSummary, Text, Understanding)
from .image_editing import ImageCrop

AdLanguage = Annotated[str, Field(min_length=2, max_length=35, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")]
#: A product photo of ads-first ideas (#199): the SHA-256 of the derivative an image request sends.
PhotoId = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
IdeasState = Literal["input", "understanding_draft", "ideas_ready"]


class UnsupportedClaim(Document):
    """Something the AI read about the product that no source quote supports (design doc §5.2, #224): a
    certification, a measurement, a performance or price claim, a comparison. Listed so the user sees it,
    never used: no claim, way of telling or ad copy may say it (``claims.unsourced_uses``)."""
    id: Identifier
    text: Text


class IdeasUnderstanding(Understanding):
    """The product understanding of ads-first ideas: the plan-first ``Understanding`` and the claims the AI
    read without a source (#224). The plan-first document is unchanged; this list is optional, so an
    understanding drafted before it reads as having none."""
    unsupported_claims: list[UnsupportedClaim] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unsupported_ids(self):
        ids = [f.id for f in self.facts] + [i.id for i in self.inferences] + [u.id for u in self.unsupported_claims]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError("marketing_fact_ids_duplicate", "事实、推测与没有出处的说法的标识不能重复")
        return self



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
    # facts_supported of a v2 check (#265): what the image shows besides its copy, verbatim; the ad's facts do not
    # stand behind these words (``gate_reason`` ``extra_text`` or ``extra_text_unsupported``).
    extra_text: list[str] = Field(default_factory=list)


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


#: How an ad's image came to be (#200): made for the ideas, a correction of an ad, an ad processed by a
#: supplier image tool (upscale, background removal, erasing), a crop or rotation of an ad, or an image the
#: user uploaded and placed under the ad.
AdOrigin = Literal["generated", "corrected", "image_tool", "image_edit", "uploaded"]


class AdSummary(Document):
    asset_version_id: str
    combination_id: str
    angle_id: str
    expression_id: str
    origin: AdOrigin = "generated"
    # For a new version of an ad (``corrected``, ``image_tool``, ``image_edit``): the version it was made
    # from, which stays an ad of its own so the two can be compared and the old one kept instead.
    source_asset_version_id: str | None = None
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
    # The photos the image request sent, as ``IdeasPhoto.photo_id`` (#199): the main photo first.
    photo_ids: list[str] = Field(default_factory=list)
    created_at: str
    # The newest automatic check of this image, if one was started (#197).
    check: IdeasAdCheck | None = None


class PendingAd(Document):
    combination_id: str
    state: Literal["queued", "running", "failed"]


class IdeasCallAddition(Document):
    """What an ``ideas`` call adds instead of drafting the whole ideas (#200)."""
    kind: Literal["claim", "expression"]
    # The claim a way of telling is added to.
    angle_id: str | None = None


class IdeasCallSummary(PlanningCallSummary):
    """A drafting call of ads-first ideas: the product understanding, then the ideas (#190).

    The plan-first ``PlanningCallSummary`` keeps its own kinds; an ads-first project never has a plan call."""
    kind: Literal["understanding", "ideas"]
    # Set when the call adds one claim or one way of telling to drafted ideas (#200).
    addition: IdeasCallAddition | None = None


class IdeasImageEdit(Document):
    """A crop or rotation of one ad (#200): a local Worker job, free of charge. ``unchanged`` when the result
    was the source itself; ``result_asset_version_id`` is then that existing version."""
    id: str
    operation_id: str
    source_asset_version_id: str
    result_asset_version_id: str | None = None
    state: Literal["queued", "succeeded", "unchanged", "failed"]
    error_code: str | None = None


#: ``checking``: every image is made (they are on the ideas page) and each is being checked (#197).
FirstBatchState = Literal["reading", "understanding", "ideas", "generating", "checking", "done", "needs_attention", "failed"]
FirstBatchStep = Literal["reading", "understanding", "ideas", "images", "checks"]
#: Why a first batch stopped (#193). ``page_unreadable`` and ``reading_failed`` end it; the others wait for the user.
#: ``step_failed`` is a step that failed (the provider or an admission refused it, ``code`` says why); ``fault``
#: is an error of ours while moving between steps that retrying did not get past (#269), with no ``code``.
FirstBatchStopReason = Literal["several_models", "page_unreadable", "reading_failed", "too_few_claims", "over_budget",
                               "ideas_refused", "step_failed", "fault", "uncertain"]


class FirstBatchSummary(Document):
    """The product's first batch as the overview reads it: whether it stopped and waits for the user."""
    id: str
    state: FirstBatchState
    needs_attention: bool
    reason: FirstBatchStopReason | None = None


class IdeasPhoto(Document):
    """A photo that may show the product's look (#199; design doc §5.2): one the product's reading found, or
    one the user added to the product (``add_marketing_ideas_photo``, #233).

    Ads may be drafted with it before anyone checks it; an ad made with it is exportable only once the user
    checks it (``verify_marketing_ideas`` ``photo_ids``), and needs making again once the user rejects it.
    Show it with ``create_marketing_ideas_photo_download_intent(photo_id)``; a reading's photo also with
    ``create_product_intake_image_download_intent(intake_id, image_id)``."""
    # The content hash of the photo's controlled derivative, the bytes an image request sends.
    photo_id: str
    # ``unavailable``: the user checked it, but the creative-library reference it became can no longer be
    # used (withdrawn, archived, its rights expired). Like ``rejected``, nothing new is made with it and
    # every ad made with it needs making again. The newest decision on the product's photo counts, in
    # whichever of the product's ideas it was made.
    status: Literal["unverified", "verified", "rejected", "unavailable"]
    # What the reading (or the user, adding it) took the photo for: the main photo, another angle, or a close-up.
    role: Literal["main", "angle", "detail"]
    # From the product page (or an image link the user pasted), from an image the user gave the reading, or
    # added to the product by the user afterwards (``added``, #233).
    source: Literal["product_page", "upload", "added"]
    source_url: str | None = None
    # The reading that found it; none for a photo the user added.
    intake_id: str | None = None
    image_id: str | None = None
    width: int | None = None
    height: int | None = None
    # The creative-library reference a checked photo became.
    reference_version_id: str | None = None
    # The ads whose image request sent this photo.
    asset_version_ids: list[str] = Field(default_factory=list)
    # Why a checked photo can no longer be used (#233): ``expired`` -- the rights the user declared ran out, and
    # checking it again (``photo_ids``) is the new declaration that renews it; ``withdrawn`` -- withdrawn,
    # archived or blocked in the library, which a check never undoes; ``file_gone`` -- its file is gone.
    unavailable_reason: Literal["expired", "withdrawn", "file_gone"] | None = None
    # The last day of the rights the user declared for a checked photo (ISO date), checked or since unavailable.
    valid_to: str | None = None


class IdeasLogo(Document):
    """A logo the user added to the product (``add_marketing_ideas_photo`` with ``role = "logo"``, #233).

    It is never sent as a photo of the product's look. Checking it (``verify_marketing_ideas`` ``photo_ids``)
    declares that it is the brand's logo and the user may use it in ads; it then becomes part of the
    product's published version as a logo, which the product's brand baseline can choose. Rejecting it
    takes it out again. Show it with ``create_marketing_ideas_photo_download_intent(photo_id)``."""
    photo_id: str
    status: Literal["unverified", "verified", "rejected", "unavailable"]
    width: int | None = None
    height: int | None = None
    # The creative-library reference a checked logo became.
    reference_version_id: str | None = None
    # Why a checked logo can no longer be used (#233): ``expired`` -- the rights the user declared ran out, and
    # checking it again (``photo_ids``) is the new declaration that renews it; ``withdrawn`` -- withdrawn,
    # archived or blocked in the library, which a check never undoes; ``file_gone`` -- its file is gone.
    unavailable_reason: Literal["expired", "withdrawn", "file_gone"] | None = None
    # The last day of the rights the user declared for a checked logo (ISO date), checked or since unavailable.
    valid_to: str | None = None


class IdeasProjectDetail(Document):
    id: str
    revision: int
    state: IdeasState
    product_id: str
    market: str
    ad_language: str
    understanding: IdeasUnderstanding | None
    # The audience the understanding states now is the one the user confirmed (#224). A confirmed audience
    # is what the product's published version names as its audience; changing its words undoes this. Always
    # set in responses; optional in the schema so clients built before it read the detail unchanged.
    audience_confirmed: bool | None = None
    ideas: Ideas | None
    latest_call: IdeasCallSummary | None
    ads: list[AdSummary]
    pending: list[PendingAd]
    # The first batch that opened these ideas, if one did (#193).
    first_batch: FirstBatchSummary | None = None
    # Every crop or rotation of these ideas' ads, oldest first (#200); the new versions are in ``ads``.
    image_edits: list[IdeasImageEdit] = Field(default_factory=list)
    # The photos the product's reading found, in the page's order, each with its checking (#199), then the ones
    # the user added to the product (#233).
    photos: list[IdeasPhoto] = Field(default_factory=list)
    # The logos the user added to the product, in the order they were added, each with its checking (#233).
    logos: list[IdeasLogo] = Field(default_factory=list)


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


class IdeasGuidingAnswer(Document):
    """The user's answer to a guiding question, in their words: a wish, never a product fact."""
    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=1000)


class IdeasAddition(Document):
    """Add one claim, or one way of telling an existing claim, to drafted ideas (#200, design doc §5.3).

    A claim comes with up to two ways of telling it, one ad each; a way of telling comes with one ad and a
    picture mechanism its claim does not use yet. Nothing drafted before changes, and no ad made so far."""
    kind: Literal["claim", "expression"]
    # ``expression``: the claim (``Angle.id``) to add a way of telling to.
    angle_id: Identifier | None = None
    # The user's own claim or way of telling, in their words ("add my own claim").
    note: str = Field(default="", max_length=1000)
    guiding_answers: list[IdeasGuidingAnswer] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def target(self):
        if (self.kind == "expression") != (self.angle_id is not None):
            raise PydanticCustomError("ideas_addition_target", "追加讲法要指明主张；追加主张不指明主张")
        return self


class StartIdeasPlanning(Document):
    """Draft the product understanding, or the ideas from it; no fact needs checking first (ADR 0018).

    With ``addition`` (``kind = "ideas"`` only, #200) the call adds one claim or one way of telling to the
    drafted ideas instead of drafting them again; it costs one planning call, quoted like any other."""
    kind: Literal["understanding", "ideas"]
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    execution_mode: Literal["byok", "mock"]
    quote_hash: str = Field(default="", max_length=64)
    addition: IdeasAddition | None = None


class ClaimAddition(Document):
    """What a drafting call adds as a new claim (#200): the claim and one ad per way of telling it."""
    angle: Angle
    combinations: list[CreativeCombination] = Field(min_length=1, max_length=2)


class ExpressionAddition(Document):
    """What a drafting call adds to an existing claim (#200): one way of telling it and its ad."""
    expression: Expression
    combination: CreativeCombination


class PrepareIdeasFeedback(Document):
    """Change one ad by making it again (#200): a correction from the user's note and its check's findings,
    or -- with ``image_tool`` -- a supplier image tool (upscale, remove the background, erase an area).

    Prepares a generation draft only; preview it for the maximum cost and submit it to make the image."""
    asset_version_id: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=1200)
    provider: Literal["gemini", "openai", "seedream", "mock"]
    model: str = Field(min_length=1, max_length=200)
    image_tool: ImageToolSpec | None = None


class IdeasFeedbackDraft(Document):
    """A prepared correction or image tool: preview and submit ``draft_id`` with the generation endpoints."""
    id: str
    project_id: str
    draft_id: str
    source_asset_version_id: str
    combination_id: str
    kind: Literal["correction", "image_tool"]
    # The findings of the source's check the correction asks to fix.
    finding_ids: list[str]


class CreateIdeasImageEdit(Document):
    """Crop then rotate one ad clockwise (#200): a new version, made locally and free of charge."""
    asset_version_id: str = Field(min_length=1, max_length=64)
    crop: ImageCrop | None = None
    rotation: Literal[0, 90, 180, 270] = 0


class PlaceIdeasUpload(Document):
    """Place an uploaded image of the product (``create_external_creatives``, rights declared) under one ad."""
    external_creative_id: str = Field(min_length=1, max_length=64)
    combination_id: Identifier


class AddIdeasPhoto(Document):
    """Add a photo to the product of these ideas (#233, design doc §5.2): an image the user uploaded that passed
    the reference upload gate (``create_library_reference_object``), and what it shows. No rights are declared
    here: checking the photo declares them, as for a photo the reading found."""
    reference_object_id: str = Field(min_length=1, max_length=64)
    # The whole product (the main photo), another angle of it, a close-up -- or the brand's logo, which never
    # stands in for the product's look and is listed in ``logos`` (#233).
    role: Literal["main", "angle", "detail", "logo"] = "detail"


class IdeasPhotoDownloadGrant(Document):
    """A short-lived signed link to a photo's released derivative, the bytes an image request sends (#233)."""
    photo_id: str
    download_url: str
    expires_at: str
    mime_type: str
    byte_size: int
    width: int
    height: int


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
    # ``uncertain`` while making images: the generation runs to reconcile, one per ad concerned (#204). The
    # existing reconciliation of a generation run settles them; planning and check calls have none.
    run_ids: list[str] | None = None
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


class FirstBatchListItem(Document):
    """A first batch that has no product yet and has not ended (#204): still reading, or stopped before the
    product could be made (several models on the page). The overview and the start page list them, so a
    user who left the reading page finds the way back."""
    id: str
    state: FirstBatchState
    needs_attention: bool
    reason: FirstBatchStopReason | None = None
    # The link the user pasted; none when they pasted text or images.
    source_url: str | None = None
    created_at: str
    updated_at: str


class FirstBatchPage(Document):
    items: list[FirstBatchListItem]


class ResumeFirstBatch(Document):
    """Continue a stopped first batch, confirming the maximum its ``resume`` offers."""
    quote_hash: str = Field(min_length=1, max_length=64)
    selected_variant: str | None = Field(default=None, min_length=1, max_length=200)
    additional_text: str | None = Field(default=None, min_length=1, max_length=16000)


class EditIdeasUnderstanding(Document):
    """Change what the AI read about the product (#198): facts, guesses and unknowns, as one document.

    ``confirmation`` in the document is ignored: a fact is checked only by ``verify_marketing_ideas``. A
    fact that reads as before keeps its checking; a changed or new fact is unchecked. Leaving a fact out
    removes it. ``source_addition`` adds material for a new quote to the product's source.

    ``unsupported_claims`` (#224) can only lose items: the server keeps each listed claim the document still
    names by id, as the AI wrote it, and ignores any other. "Add a source" removes one and adds the fact it
    becomes (unchecked); "don't need it" removes one."""
    understanding: IdeasUnderstanding
    source_addition: str | None = Field(default=None, max_length=8000)


class EditIdeasClaim(Document):
    """Change one claim in the user's own words (#224, design doc §5.7): what it says, why it is worth
    trying and the facts it stands on, as a whole. Free of charge; nothing is drafted by AI.

    Every fact named must be a fact of the understanding now: each has a source, and neither a removed fact
    nor an unsourced claim can be named. The words may state no number, price or certification the named
    facts do not, and no listed unsourced claim. Each way of telling the claim keeps only the facts the claim
    still names. The claim's ads, their copy and their image instructions stay as they are; the images made
    before need making again (``ideas_changed``), and a new image is made on the claim as it is now."""
    proposition: Text
    rationale: Text
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=30)


class EditIdeasCopy(Document):
    """Change one ad's headline and body copy in the user's own words (#228, design doc §5.3), as a whole.
    Free of charge; nothing is drafted by AI.

    The copy belongs to one place of the ideas -- a claim told one way (a combination) -- and the image model
    renders it into every picture made there, so an image never follows a change: the images made there before
    need making again (``ideas_changed``) and a new one is made with ``prepare_marketing_ideas_generation``.
    An uploaded image keeps its own words and is left as it is. Both texts use the ads' language; they may
    state no number, price or certification the facts the place cites do not, and no listed unsourced claim.
    The claim, the way of telling, the facts they name and the English image brief stay as they are."""
    headline: Text
    body_copy: Text


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
    # ``IdeasPhoto.photo_id`` of photos the user confirms are this product's real photos and may be used in
    # ads (#199): they become creative-library references and part of the product's published version. An
    # ``IdeasLogo.photo_id`` here confirms the brand's logo, which the user may use in ads (#233).
    photo_ids: list[PhotoId] = Field(default_factory=list, max_length=20)
    # Photos the user says are not this product, or may not be used: ads made with one need making again. A
    # logo here is not the brand's, or may not be used.
    rejected_photo_ids: list[PhotoId] = Field(default_factory=list, max_length=20)
    # The user read the audience the understanding states now and says it is right (#224). Its words are
    # recorded; the product's published version names it as the audience until those words change. Absent
    # or false: the audience is not part of this verification.
    audience_confirmed: bool | None = None

    @model_validator(mode="after")
    def _one_decision_per_photo(self):
        if set(self.photo_ids) & set(self.rejected_photo_ids):
            raise PydanticCustomError("photo_decision_conflict", "同一张照片不能既核对又否定")
        return self


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
