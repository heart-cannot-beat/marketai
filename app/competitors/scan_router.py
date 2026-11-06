"""``/api/v1/competitor-scans`` -- paid competitor ad scans (Issue #270, spec §5).

Thin HTTP adapter over :mod:`app.competitors.scan_service`. Workspace and
actor come only from the verified identity (``request_tenant_context``);
another Workspace's scan, product or competitor answers ``404`` exactly like
an unknown one. Members preview, start and resume scans; reconciling an
``uncertain`` page by hand is Owner-only. Every command requires an
``Idempotency-Key`` (``x-marketai-idempotent``); the preview is read-only.

A scan is quoted on two lines before it is started (spec §5.1): the
provider's credits and USD (pages per competitor x competitors + keyword
pages + the media-refresh quota) and the LLM maximum (one ``llm_chat``
reservation per numbered call name: ``filter:i``, ``tag:i``, ``cluster:1``,
``discover:i``). Starting it reserves both under one Operation; without a
``market_data`` key *and* an AI key only Controlled Mock is offered, for the
whole scan.

Each observation carries the relevance revision in effect (Issue #271): the
scan's AI or rule judgment until a member flips it. A flip appends a
revision (Owner and Member alike, audited with the actor); the history of
an observation's revisions is readable, and observations filter by it.

Tags, clusters and snapshots (Issue #272): each relevant observation's AI
tag suggestion, which any member confirms or changes as a new revision; the
scan's buying-claim clusters; snapshots -- the pure, free freeze of the
revisions in effect, unique per hash (refreezing unchanged revisions
returns the existing one) -- and the summary read from one. Reclustering is
a paid command of its own: quoted first, then reserved and run as its own
Job, which appends a clusters revision and freezes once. Whether drafting
cites the latest snapshot is a product switch any member sets
(``/products/{product_id}/competitor-settings``).

Discovery (Issue #276, spec §8): a scan with keywords recommends the
advertisers its keywords found outside the product's list -- grouped by
page after the §7.1 rules, each judged by one ``discover:i`` call
(``relevant`` / ``adjacent`` are recommended). Any member accepts one (it
becomes a competitor confirmed by the page ID from its ads: no resolve, no
resolve charge) or rejects it (later scans leave it out); each decision is
an append-only row, audited with the actor.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.platform.blob_store_factory import build_blob_store
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import discovery_service, scan_service
from .service import CompetitorProblem
from .v1_router import Competitor

router = APIRouter(prefix="/competitor-scans", tags=["competitor-scans"])
products_router = APIRouter(prefix="/products", tags=["competitor-scans"])
_blob_store = build_blob_store()


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _problem(exc: CompetitorProblem) -> HTTPException:
    return HTTPException(
        exc.status,
        {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "params": exc.params},
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


ExecutionMode = Literal["byok", "mock"]
ScanStatus = Literal["queued", "running", "completed", "partial", "failed", "uncertain"]
ScanStep = Literal["fetch", "media", "filter", "tag", "cluster", "freeze"]
CompetitorId = Annotated[str, Field(min_length=1, max_length=64)]
Keyword = Annotated[str, Field(min_length=1, max_length=100)]
Relevance = Literal["relevant", "adjacent", "unrelated"]
AssessmentSource = Literal["ai", "rule", "user"]
RelevanceRule = Literal["personal_account", "wholesale_site", "dealer"]


class CompetitorScanRequest(BaseModel):
    """What to scan (spec §5.1). Every competitor must belong to the product,
    be active and have a confirmed page; ``role=self`` competitors are
    scanned as the baseline. ``keywords`` feed competitor discovery (spec
    §8), ``keyword_pages`` pages each. ``max_media_refreshes`` is the quota
    of paid ``ad_refresh`` calls for media links that expired.
    ``max_tagged_ads`` and ``max_discover_advertisers`` cap the LLM steps.
    ``execution_mode`` defaults to the quote's default (``byok`` when both
    keys are ready, else ``mock``). The AI output language is the
    initiator's interface language, frozen into ``params.language``."""

    model_config = {"extra": "forbid"}

    product_id: str = Field(min_length=1, max_length=64)
    competitor_ids: list[CompetitorId] = Field(min_length=1, max_length=20)
    keywords: list[Keyword] = Field(default_factory=list, max_length=5)
    country: str = Field(default="US", min_length=2, max_length=2)
    max_pages_per_competitor: int = Field(default=3, ge=1, le=5)
    keyword_pages: int = Field(default=2, ge=1, le=5)
    max_media_refreshes: int = Field(default=10, ge=0, le=50)
    max_tagged_ads: int = Field(default=60, ge=0, le=200)
    max_discover_advertisers: int = Field(default=30, ge=0, le=100)
    execution_mode: ExecutionMode | None = None


class CompetitorScanQuoteLine(BaseModel):
    """One provider operation: ``calls`` x its credits x the credit price."""

    operation: Literal["page", "keyword_page", "ad_refresh"]
    calls: int
    credits: int
    usd: str


class CompetitorScanMarketDataQuote(BaseModel):
    """Line (a): the provider's maximum. ``max_usd`` is real money (``"0"``
    under Mock, whose ``simulated_usd`` is an estimate, null without a price
    table)."""

    provider: str
    max_credits: int
    max_usd: str
    simulated_usd: str | None = None
    pricing_version: str | None = None
    lines: list[CompetitorScanQuoteLine] = Field(default_factory=list)


class CompetitorScanLlmQuoteLine(BaseModel):
    """One LLM step: ``calls`` numbered call names, each reserved at
    ``quote_llm_chat``'s maximum for its token ceilings."""

    step: Literal["filter", "tag", "cluster", "discover"]
    calls: int
    max_input_tokens: int
    max_output_tokens: int
    usd: str
    simulated_usd: str | None = None


class CompetitorScanLlmQuote(BaseModel):
    """Line (b): the LLM maximum, reserved per call name and released for
    every call name the scan does not use."""

    provider: str
    model: str
    calls: int
    max_usd: str
    simulated_usd: str | None = None
    price_rule_version: str
    lines: list[CompetitorScanLlmQuoteLine] = Field(default_factory=list)


class CompetitorScanCredential(BaseModel):
    """A key a real scan needs: ``market_data`` (ScrapeCreators) or
    ``generation`` (the AI key ``llm_chat`` uses). ``code`` says why it is
    not ready."""

    binding_scope: Literal["market_data", "generation"]
    provider: str
    ready: bool
    code: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class CompetitorScanQuote(BaseModel):
    """The two-line maximum of a scan (``kind=scan``) or of what is left of
    one (``kind=resume``: remaining pages, the unused refresh quota and the
    LLM steps without an answer). ``available_modes`` lists ``byok`` only
    when both keys are ready and prices are configured; otherwise
    ``byok_unavailable_code`` (and its ``byok_unavailable_params``) says
    why. ``max_usd`` is the sum of both lines."""

    kind: Literal["scan", "resume"]
    scan_id: str | None = None
    product_id: str
    execution_mode: ExecutionMode
    available_modes: list[ExecutionMode]
    byok_unavailable_code: str | None = None
    byok_unavailable_params: dict[str, Any] = Field(default_factory=dict)
    credentials: list[CompetitorScanCredential] = Field(default_factory=list)
    country: str
    language: Literal["en", "zh-CN"]
    competitor_count: int
    keyword_count: int
    market_data: CompetitorScanMarketDataQuote
    llm: CompetitorScanLlmQuote
    max_usd: str
    simulated_usd: str | None = None
    currency: Literal["USD"] = "USD"


class CompetitorScanParams(BaseModel):
    """The scan's frozen parameters; first release: Meta, active ads only."""

    competitor_ids: list[str]
    keywords: list[str] = Field(default_factory=list)
    country: str
    platform: Literal["meta"] = "meta"
    ad_status: Literal["active"] = "active"
    max_pages_per_competitor: int
    keyword_pages: int
    max_media_refreshes: int
    max_tagged_ads: int
    max_discover_advertisers: int
    language: Literal["en", "zh-CN"]


class CompetitorScanFailure(BaseModel):
    code: str
    retryable: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class CompetitorScanTarget(BaseModel):
    """One fetch path: a competitor's confirmed page (``page:<competitor_id>``)
    or a keyword (``keyword:<keyword>``). ``pages_fetched`` counts pages
    that succeeded; ``exhausted`` means the provider had no further page.
    A failed target is fetched again by ``resume`` from its stored cursor.
    ``page_name`` is the confirmation's, frozen at admission (none for a
    link); ``observed_page_name`` is read-only, the advertiser name of the
    page's newest observation in the product's scans (null until one)."""

    origin: str
    kind: Literal["page", "keyword"]
    competitor_id: str | None = None
    role: Literal["competitor", "self"] | None = None
    page_id: str | None = None
    page_name: str | None = None
    observed_page_name: str | None = None
    confirmation_revision: int | None = None
    keyword: str | None = None
    status: Literal["pending", "fetching", "completed", "failed", "uncertain"]
    pages_planned: int
    pages_fetched: int
    ads_seen: int
    exhausted: bool = False
    failure: CompetitorScanFailure | None = None


class CompetitorScanStepState(BaseModel):
    """One Worker step (spec §5.2). A ``partial`` or ``failed`` filter or tag
    step says why in ``reason``: ``calls_failed``, ``ordinals_exhausted``
    (more work than reserved call names) or the ``llm_chat`` code that
    stopped the run (e.g. ``credential_rejected``; tag and cluster then
    record the same code without calling). ``skipped`` reasons:
    ``no_vocabulary`` (tag: the product has no dimension vocabulary),
    ``vocabulary_not_frozen`` (tag: a scan admitted before Issue #272),
    ``no_relevant_observations`` (tag, cluster), ``no_observations`` and
    ``no_judgments`` (freeze); ``not_implemented`` on scans of releases
    before Issue #272. A ``completed`` cluster step with ``reason=answered``
    read an earlier run's answer. Discovery (Issue #276) reports its step
    on the scan's discoveries, not here."""

    step: ScanStep
    status: Literal["pending", "running", "completed", "partial", "failed", "skipped"]
    reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    attempts: int = 0


class CompetitorScanMarketDataCost(BaseModel):
    """Settled from each call's ``credits_charged`` (and reconciled pages)."""

    calls: int
    credits_charged: int
    usd: str
    simulated_usd: str | None = None


class CompetitorScanLlmCost(BaseModel):
    calls: int
    usd: str
    simulated_usd: str | None = None


class CompetitorScanCost(BaseModel):
    """What the scan cost so far: both lines and their sum (real money)."""

    market_data: CompetitorScanMarketDataCost
    llm: CompetitorScanLlmCost
    usd: str
    simulated_usd: str | None = None


class CompetitorScanResume(BaseModel):
    """A resume: same scan, a new Job and Operation, possibly a newer key."""

    operation_id: str
    job_id: str
    credential_version_id: str | None = None
    max_credits: int
    max_usd: str
    requested_by: str | None = None
    requested_at: str | None = None


class CompetitorScanBalanceCheck(BaseModel):
    """The automatic reconciliation (spec §5.1): the free credit-balance
    compared with the last successful call before the page, less the charges
    known since. ``difference`` 0 settles "not charged", exactly 1 "charged";
    anything else (``reason``) is left to the Owner."""

    outcome: Literal["not_charged", "charged", "manual_required"]
    reason: Literal[
        "difference", "cached_baseline", "no_baseline", "credential_changed",
        "unknown_charge_in_window", "balance_unavailable",
    ] | None = None
    difference: int | None = None
    baseline_credits_remaining: int | None = None
    known_charges: int | None = None
    balance: int | None = None
    checked_at: str | None = None


class CompetitorScanReconciliation(BaseModel):
    """A page whose outcome was lost after the dispatch fence. Its cost is
    settled by the automatic check or by the Owner; the page itself is
    fetched again as a new attempt."""

    attempt_id: str
    call_name: str
    origin: str
    page: int
    credential_version_id: str | None = None
    status: Literal["pending", "charged", "not_charged"]
    source: Literal["auto", "owner"] | None = None
    balance_check: CompetitorScanBalanceCheck | None = None
    decided_by: str | None = None
    decided_at: str | None = None


class CompetitorScanMedia(BaseModel):
    """Media archiving: one entry per card image or video poster."""

    planned: int = 0
    available: int = 0
    unavailable: int = 0
    pending: int = 0
    refreshes_used: int = 0
    refresh_quota: int = 0


class CompetitorScanRecluster(BaseModel):
    """A member's paid recluster: its own Operation and Job, ``cluster:1``
    reserved at ``max_usd``; when it completes, the new clusters revision
    and the snapshot it froze."""

    operation_id: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    max_usd: str
    simulated_usd: str | None = None
    cluster_revision: int | None = None
    snapshot_id: str | None = None
    failure: dict[str, Any] | None = None
    requested_by: str | None = None
    requested_at: str | None = None
    finished_at: str | None = None


class CompetitorScan(BaseModel):
    """A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some
    pages succeeded, some failed or ran out of credits) | ``failed`` (no page
    succeeded) | ``uncertain`` (a page's outcome is unknown and not yet
    reconciled). ``stage`` is the last completed step. ``can_resume`` when
    pages are left, a page awaits reconciliation, or a judgment step did not
    finish (a ``completed`` scan whose AI key was refused mid-way, say).
    ``waits`` counts how often it queued behind another market-data job on
    the same key (spec §5.1). ``snapshot_id`` is the snapshot of the
    revisions in effect as of the last freeze."""

    scan_id: str
    product_id: str
    operation_id: str
    job_id: str
    status: ScanStatus
    stage: ScanStep | None = None
    execution_mode: ExecutionMode
    provider: str
    platform: Literal["meta"] = "meta"
    country: str
    params: CompetitorScanParams
    targets: list[CompetitorScanTarget] = Field(default_factory=list)
    steps: list[CompetitorScanStepState] = Field(default_factory=list)
    quote: CompetitorScanQuote
    cost: CompetitorScanCost | None = None
    initial_credential_version_id: str | None = None
    resumes: list[CompetitorScanResume] = Field(default_factory=list)
    reconciliation: list[CompetitorScanReconciliation] = Field(default_factory=list)
    media: CompetitorScanMedia = Field(default_factory=CompetitorScanMedia)
    observation_count: int = 0
    waits: int = 0
    failure: CompetitorScanFailure | None = None
    can_resume: bool = False
    reclusters: list[CompetitorScanRecluster] = Field(default_factory=list)
    snapshot_id: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    replayed: bool = False


class CompetitorScanSummary(BaseModel):
    scan_id: str
    product_id: str
    operation_id: str
    job_id: str
    status: ScanStatus
    stage: ScanStep | None = None
    execution_mode: ExecutionMode
    country: str
    competitor_ids: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    observation_count: int = 0
    created_by: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


class CompetitorScanList(BaseModel):
    items: list[CompetitorScanSummary] = Field(default_factory=list)


class CompetitorScanReconcileRequest(BaseModel):
    """The Owner's verdict on one pending page: did the provider charge it?"""

    model_config = {"extra": "forbid"}

    attempt_id: str = Field(min_length=1, max_length=64)
    outcome: Literal["charged", "not_charged"]


class CompetitorAdMedia(BaseModel):
    """One archived image or video poster (video files are never archived).
    ``source_url`` is the provider link without its signature; ``expires_at``
    is when the signed link stopped working (``oe=``)."""

    kind: Literal["image", "video_poster"]
    card_index: int
    source_field: str
    source_url: str | None = None
    expires_at: str | None = None
    status: Literal["pending", "available", "unavailable"]
    reason: str | None = None
    sha256: str | None = None
    byte_size: int | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    fetched_at: str | None = None
    refreshed: bool = False


class CompetitorAdCard(BaseModel):
    """Card 0 is the snapshot's own creative; ``snapshot.cards[i]`` is card ``i + 1``."""

    index: int
    title: str | None = None
    body: str | None = None
    cta_text: str | None = None
    cta_type: str | None = None
    link_url: str | None = None
    link_description: str | None = None
    image: CompetitorAdMedia | None = None
    video_poster: CompetitorAdMedia | None = None


class CompetitorAdOrigin(BaseModel):
    """A path the observation was found by: a competitor's page or a keyword."""

    origin: str
    kind: Literal["page", "keyword"]
    competitor_id: str | None = None
    keyword: str | None = None
    attempt_id: str | None = None
    observed_at: str | None = None


class CompetitorAdThumbnail(BaseModel):
    """A short-lived signed GET of the 512px derivative (not downloadable as a reference)."""

    url: str
    expires_at: str
    mime_type: str
    width: int | None = None
    height: int | None = None


class CompetitorAdRelevance(BaseModel):
    """The relevance revision in effect (spec §7.1), AI inference until a
    member flips it (ADR 0014). ``source``: ``ai`` (the scan's filter step;
    ``confidence`` and a one-sentence ``reason`` in the scan's language),
    ``rule`` (a keyword-only observation the §7.1 rules judged ``unrelated``
    without the LLM; ``rule`` names it) or ``user`` (a flip)."""

    relevance: Relevance
    source: AssessmentSource
    revision: int
    confidence: float | None = None
    reason: str | None = None
    rule: RelevanceRule | None = None
    assessed_at: str | None = None


class CompetitorAdTagValue(BaseModel):
    factor_id: str
    value_id: str | None = None
    confidence: float | None = None


class CompetitorAdTags(BaseModel):
    """The tags revision in effect: the scan's AI suggestion
    (``ai_suggestion``, ``source=ai``) until a member confirms or changes it
    (``source=user``). ``value_id`` null is "none of these"."""

    source: AssessmentSource
    revision: int
    ai_suggestion: bool
    factors: list[CompetitorAdTagValue] = Field(default_factory=list)
    summary: str | None = None
    assessed_at: str | None = None


class CompetitorAd(BaseModel):
    """One observation (spec §5.3): what was seen in the Meta Ad Library,
    never what works (ADR 0014). ``competitor_ids`` are the competitors whose
    page it was fetched from (projected from its ``page:`` origins);
    ``role`` is ``self`` when the ad is the product's own page's: fetched
    from that page, or its advertiser page is the page a ``role=self``
    competitor of this scan confirmed.
    ``field_sources`` names the provider field each value came from.
    ``relevance`` is null until the filter step (or a member) judged it;
    ``tags`` until the tag step (or a member) tagged it."""

    ad_id: str
    scan_id: str
    provider: str
    ad_archive_id: str
    collation_id: str | None = None
    collation_count: int | None = None
    advertiser_page_id: str | None = None
    advertiser_name: str | None = None
    page_profile_uri: str | None = None
    page_categories: list[str] = Field(default_factory=list)
    is_active: bool | None = None
    start_date: str | None = None
    end_date: str | None = None
    days_running: int | None = None
    display_format: str | None = None
    publisher_platforms: list[str] = Field(default_factory=list)
    cards: list[CompetitorAdCard] = Field(default_factory=list)
    landing_domain: str | None = None
    library_url: str | None = None
    observed_at: str
    origins: list[CompetitorAdOrigin] = Field(default_factory=list)
    competitor_ids: list[str] = Field(default_factory=list)
    role: Literal["competitor", "self"] | None = None
    media_status: Literal["none", "pending", "available", "unavailable"]
    thumbnail: CompetitorAdThumbnail | None = None
    field_sources: dict[str, str] = Field(default_factory=dict)
    relevance: CompetitorAdRelevance | None = None
    tags: CompetitorAdTags | None = None


class CompetitorAdPage(BaseModel):
    """One page of observations, longest-running first; ``total`` counts every match."""

    items: list[CompetitorAd] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0


class CompetitorAdRelevanceFlip(BaseModel):
    """A member's verdict on one observation; flipping back is one more revision."""

    model_config = {"extra": "forbid"}

    relevance: Relevance


class CompetitorAdAssessmentPrevious(BaseModel):
    """The revision a flip replaced."""

    revision: int
    relevance: Relevance | None = None
    source: AssessmentSource


class CompetitorAdAssessmentEvidence(BaseModel):
    """Which ``llm_chat`` call answered an ``ai`` revision (its batch is ``call_name``)."""

    job_id: str | None = None
    call_name: str | None = None
    execution_mode: ExecutionMode | None = None
    provider: str | None = None
    model: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None


class CompetitorAdAssessment(BaseModel):
    """One append-only revision of an observation's relevance. ``created_by``
    is the member who flipped it (null for the scan's own judgments);
    ``previous`` is what a flip replaced."""

    assessment_id: str
    ad_id: str
    scan_id: str
    kind: Literal["relevance"]
    revision: int
    source: AssessmentSource
    relevance: Relevance
    confidence: float | None = None
    reason: str | None = None
    language: Literal["en", "zh-CN"] | None = None
    rule: RelevanceRule | None = None
    previous: CompetitorAdAssessmentPrevious | None = None
    evidence: CompetitorAdAssessmentEvidence | None = None
    attempt_id: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    replayed: bool = False


class CompetitorAdAssessmentList(BaseModel):
    """An observation's relevance revisions, oldest first; ``current`` is the one in effect."""

    scan_id: str
    ad_id: str
    kind: Literal["relevance"]
    items: list[CompetitorAdAssessment] = Field(default_factory=list)
    current: CompetitorAdAssessment | None = None


class CompetitorTagInput(BaseModel):
    model_config = {"extra": "forbid"}

    factor_id: str = Field(min_length=1, max_length=200)
    value_id: str | None = Field(default=None, max_length=64)


class CompetitorAdTagsConfirm(BaseModel):
    """A member's tags for one observation, against the scan's frozen
    vocabulary: one value per factor (``value_id`` null or ``none`` = none of
    these); factors left out are "none". Confirming the AI's values as they
    are is a revision too."""

    model_config = {"extra": "forbid"}

    tags: list[CompetitorTagInput] = Field(default_factory=list, max_length=24)


class CompetitorAdTagFactor(BaseModel):
    factor_id: str | None = None
    factor_label: str | None = None
    value_id: str | None = None
    value_label: str | None = None
    confidence: float | None = None
    reason: str | None = None


class CompetitorAdTagImage(BaseModel):
    card_index: int
    derivative: Literal["thumbnail", "original"]


class CompetitorAdTagPrevious(BaseModel):
    revision: int
    source: AssessmentSource


class CompetitorAdTagAssessment(BaseModel):
    """One append-only tags revision: the AI's suggestion (``ai_suggestion``;
    per factor a value, confidence and reason in the scan's language, a
    one-sentence ``summary`` and the card images it saw) or a member's
    confirmation (``created_by``, ``previous``)."""

    assessment_id: str
    ad_id: str
    scan_id: str
    kind: Literal["tags"]
    revision: int
    source: AssessmentSource
    ai_suggestion: bool
    factors: list[CompetitorAdTagFactor] = Field(default_factory=list)
    summary: str | None = None
    language: Literal["en", "zh-CN"] | None = None
    vocabulary_hash: str | None = None
    images: list[CompetitorAdTagImage] | None = None
    previous: CompetitorAdTagPrevious | None = None
    evidence: CompetitorAdAssessmentEvidence | None = None
    attempt_id: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    replayed: bool = False


class CompetitorTagVocabularyValue(BaseModel):
    value_id: str
    label: str


class CompetitorTagVocabularyFactor(BaseModel):
    """``source`` is ``test_plan``, ``dimension_template`` or
    ``expression_catalog`` (the expression mechanism catalog, one factor
    every product has; Issue #329)."""

    factor_id: str
    label: str
    source: str | None = None
    values: list[CompetitorTagVocabularyValue] = Field(default_factory=list)


class CompetitorTagVocabulary(BaseModel):
    """The product's dimension vocabulary as the scan froze it."""

    factors: list[CompetitorTagVocabularyFactor] = Field(default_factory=list)


class CompetitorAdTagList(BaseModel):
    """An observation's tags revisions, oldest first; ``current`` is the one
    in effect. ``vocabulary`` is null until the tag step froze one."""

    scan_id: str
    ad_id: str
    kind: Literal["tags"]
    vocabulary: CompetitorTagVocabulary | None = None
    items: list[CompetitorAdTagAssessment] = Field(default_factory=list)
    current: CompetitorAdTagAssessment | None = None


class CompetitorSnapshotCounts(BaseModel):
    observations: int = 0
    relevant: int = 0
    adjacent: int = 0
    unrelated: int = 0
    unassessed: int = 0
    self: int = 0


class CompetitorScanSnapshotMeta(BaseModel):
    """A snapshot: the scan's relevant observations and the revisions in
    effect, frozen with a hash over the whole content (spec §7.1).
    ``current`` is the scan's ``snapshot_id``."""

    snapshot_id: str
    scan_id: str
    product_id: str
    country: str
    execution_mode: ExecutionMode
    hash: str
    cluster_revision: int | None = None
    current: bool = False
    counts: CompetitorSnapshotCounts = Field(default_factory=CompetitorSnapshotCounts)
    job_id: str | None = None
    created_by: str | None = None
    created_at: str | None = None


class CompetitorScanSnapshot(CompetitorScanSnapshotMeta):
    """A snapshot with its frozen ``content`` (``version``
    ``competitor-snapshot/v1``: ``relevant_ad_ids``, ``judgments``,
    ``cluster_revision``, ``claim_clusters``, ``unclustered_ad_ids``,
    ``dimension_counts``, ``long_running_ad_ids``, ``adjacent_ad_ids``,
    ``self_ad_ids``, ``ads``, ``params``). A refreeze answers ``created``
    false when the hash was already frozen."""

    content: dict[str, Any] = Field(default_factory=dict)
    created: bool = False
    replayed: bool = False


class CompetitorScanSnapshotList(BaseModel):
    """A scan's snapshots, newest first."""

    scan_id: str
    current_snapshot_id: str | None = None
    items: list[CompetitorScanSnapshotMeta] = Field(default_factory=list)


class CompetitorSnapshotAdTags(BaseModel):
    source: AssessmentSource
    ai_suggestion: bool
    values: dict[str, str | None] = Field(default_factory=dict)


class CompetitorSnapshotAd(BaseModel):
    """An observation as the summary lists it. ``ai_inferred`` is false for
    a member's or a rule's relevance (``source`` ``user`` / ``rule``)."""

    ad_id: str
    ad_archive_id: str | None = None
    advertiser_page_id: str | None = None
    advertiser_name: str | None = None
    role: Literal["competitor", "self"] | None = None
    days_running: int | None = None
    display_format: str | None = None
    title: str | None = None
    library_url: str | None = None
    relevance: Relevance | None = None
    relevance_source: AssessmentSource | None = None
    ai_inferred: bool = False
    tags: CompetitorSnapshotAdTags | None = None


class CompetitorClusterAdvertiser(BaseModel):
    advertiser_page_id: str | None = None
    advertiser_name: str | None = None
    role: Literal["competitor", "self"]


class CompetitorClaimCluster(BaseModel):
    """One buying claim (AI inference, ``ai_inference``): its members as of
    the snapshot (observations no longer relevant left it), the advertisers
    (``role=self`` marked; ``advertiser_count`` leaves them out) and the
    longest days running."""

    cluster_id: str
    claim: str
    ad_ids: list[str] = Field(default_factory=list)
    observation_count: int = 0
    advertisers: list[CompetitorClusterAdvertiser] = Field(default_factory=list)
    advertiser_count: int = 0
    self_included: bool = False
    max_days_running: int | None = None
    library_urls: list[str] = Field(default_factory=list)
    ai_inference: bool = True
    ads: list[CompetitorSnapshotAd] = Field(default_factory=list)


class CompetitorDimensionValueCount(BaseModel):
    value_id: str
    label: str
    confirmed: int = 0
    suggested: int = 0


class CompetitorDimensionNone(BaseModel):
    """Relevant observations with no value of this factor: ``confirmed``
    none, ``suggested`` none, or no tags at all (``missing``)."""

    confirmed: int = 0
    suggested: int = 0
    missing: int = 0


class CompetitorDimensionCount(BaseModel):
    """Per value, relevant observations a member confirmed it on
    (``confirmed``) and ones that only carry it as an AI suggestion."""

    factor_id: str
    label: str
    values: list[CompetitorDimensionValueCount] = Field(default_factory=list)
    none: CompetitorDimensionNone = Field(default_factory=CompetitorDimensionNone)


class CompetitorSnapshotConclusion(BaseModel):
    """The AI's one-sentence conclusion of the cited clusters revision (AI inference)."""

    text: str
    ai_inference: bool = True
    cluster_revision: int
    language: Literal["en", "zh-CN"] | None = None


class CompetitorRelevanceSources(BaseModel):
    """Who judged the relevant observations; ``non_ai`` = ``rule`` + ``user``."""

    ai: int = 0
    rule: int = 0
    user: int = 0
    ai_inferred: int = 0
    non_ai: int = 0


class CompetitorScanSnapshotSummary(CompetitorScanSnapshotMeta):
    """The summary of one snapshot (spec §9): claim clusters, the relevant
    observations no cluster holds (``unclustered``: flipped to relevant
    since the last clustering), adjacent, long-running (``long_running_days``
    or more) and own-page observations, dimension counts and the AI's
    conclusion. ``pending_changes`` says a refreeze would make a new
    snapshot; ``can_recluster`` that a recluster can be quoted now."""

    mock: bool = False
    language: Literal["en", "zh-CN"] | None = None
    pending_changes: bool = False
    can_recluster: bool = False
    relevance_sources: CompetitorRelevanceSources = Field(default_factory=CompetitorRelevanceSources)
    clusters: list[CompetitorClaimCluster] = Field(default_factory=list)
    unclustered: list[CompetitorSnapshotAd] = Field(default_factory=list)
    adjacent: list[CompetitorSnapshotAd] = Field(default_factory=list)
    long_running: list[CompetitorSnapshotAd] = Field(default_factory=list)
    self_observations: list[CompetitorSnapshotAd] = Field(default_factory=list)
    long_running_days: int = 60
    dimension_counts: list[CompetitorDimensionCount] = Field(default_factory=list)
    conclusion: CompetitorSnapshotConclusion | None = None


class CompetitorScanReclusterQuote(BaseModel):
    """What a recluster can cost at most: ``cluster:1`` at
    ``quote_llm_chat``'s maximum, in the scan's own execution mode."""

    kind: Literal["recluster"]
    scan_id: str
    product_id: str
    execution_mode: ExecutionMode
    relevant_count: int
    llm: CompetitorScanLlmQuote
    max_usd: str
    simulated_usd: str | None = None
    currency: Literal["USD"] = "USD"


class ProductCompetitorSettings(BaseModel):
    """Whether drafting cites the latest competitor snapshot of the plan's
    market (spec §7.2; on unless a member turned it off)."""

    product_id: str
    competitor_snapshot_in_drafts: bool
    updated_at: str | None = None
    replayed: bool = False


class ProductCompetitorSettingsUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    competitor_snapshot_in_drafts: bool


# -- Discovery (Issue #276) --------------------------------------------------


class CompetitorDiscoveryDecision(BaseModel):
    """A member's decision on one recommended advertiser: append-only, per
    product. ``competitor_id`` is the competitor an ``accept`` created. The
    newest decision counts: a ``reject`` always, an ``accept`` only while
    that competitor still has the page on the product's list."""

    decision_id: str
    scan_id: str
    advertiser_page_id: str
    decision: Literal["accept", "reject"]
    reason: str | None = None
    competitor_id: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None


class CompetitorDiscovery(BaseModel):
    """One recommended advertiser: found only by the scan's keywords, past
    the §7.1 rules (never a personal account; no avatar is ever shown) and
    judged ``relevant`` or ``adjacent`` by its ``discover:i`` call -- AI
    inference with a one-sentence ``reason`` in the scan's ``language``.
    ``observation_count`` counts its keyword-only observations in this scan.
    ``state``: ``open`` (never decided, or the latest decision is an
    ``accept`` whose competitor no longer has the page on the product's
    list -- archived or re-confirmed elsewhere -- so it is decided again);
    ``accepted`` (the latest decision, its competitor still on the list);
    ``rejected``; ``competitor`` (already on the list, added another way).
    ``decision`` is the product's latest decision, in effect or not;
    ``competitor_id`` is the listed competitor that has the page, if any.
    ``library_url`` opens its page in the Meta Ad Library."""

    advertiser_page_id: str
    advertiser_name: str | None = None
    categories: list[str] = Field(default_factory=list)
    observation_count: int
    landing_domains: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    relevance: Literal["relevant", "adjacent"]
    reason: str | None = None
    language: str | None = None
    library_url: str
    call_name: str
    state: Literal["open", "accepted", "rejected", "competitor"]
    decision: CompetitorDiscoveryDecision | None = None
    competitor_id: str | None = None


class CompetitorDiscoveryList(BaseModel):
    """A scan's recommendations, most observations first. ``status`` and
    ``reason`` are the discover step's, which runs last on a scan with
    keywords (``skipped`` + ``no_keywords`` without any, ``no_candidates``
    when they found no advertiser outside the list; ``pending`` until it
    runs; ``partial`` or ``failed`` with ``calls_failed``,
    ``ordinals_exhausted`` or the ``llm_chat`` code that stopped it, which a
    resume finishes). ``advertisers`` is
    the frozen table (at most ``max_discover_advertisers``; ``over_cap``
    more were left out), ``judged`` how many have an answer, ``unrelated``
    how many were judged unrelated and are not listed."""

    scan_id: str
    product_id: str
    execution_mode: ExecutionMode
    language: str | None = None
    status: Literal["pending", "running", "completed", "partial", "failed", "skipped"]
    reason: str | None = None
    advertisers: int = 0
    judged: int = 0
    recommended: int = 0
    unrelated: int = 0
    over_cap: int = 0
    items: list[CompetitorDiscovery] = Field(default_factory=list)


class CompetitorDiscoveryDecisionRequest(BaseModel):
    """``accept`` adds the advertiser as a competitor confirmed by the page
    ID from its ads (no resolve, no charge); ``reject`` keeps it out of the
    product's later scans. ``reason`` is optional."""

    model_config = {"extra": "forbid"}

    decision: Literal["accept", "reject"]
    reason: str | None = Field(default=None, max_length=500)


class CompetitorDiscoveryDecisionResult(BaseModel):
    """The recommendation after the decision, the decision in effect, and the
    competitor an accept created (null for a reject)."""

    discovery: CompetitorDiscovery
    decision: CompetitorDiscoveryDecision
    competitor: Competitor | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(lambda: fn(*args, **kwargs))
    except CompetitorProblem as exc:
        raise _problem(exc) from exc


def _params(body: CompetitorScanRequest) -> dict:
    return body.model_dump(exclude={"execution_mode"})


@router.post("/preview", operation_id="preview_competitor_scan", response_model=CompetitorScanQuote)
async def preview_competitor_scan(
    body: CompetitorScanRequest,
    context: TenantContext = Depends(request_tenant_context),
):
    """Both lines of the maximum a scan can cost, and the keys it needs; reserves nothing."""
    return await _call(scan_service.preview_scan, context, _params(body), execution_mode=body.execution_mode)


@router.post(
    "",
    operation_id="create_competitor_scan",
    response_model=CompetitorScan,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_competitor_scan(
    body: CompetitorScanRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Reserve both lines under one Operation and queue the scan Job; the
    same ``Idempotency-Key`` replays the same scan (never deduplicated by
    date). Poll the Operation or the scan itself."""
    return await _call(
        scan_service.create_scan, context, _params(body),
        execution_mode=body.execution_mode, idempotency_key=idempotency_key,
    )


@router.get("", operation_id="list_competitor_scans", response_model=CompetitorScanList)
async def list_competitor_scans(
    product_id: str | None = Query(default=None, min_length=1, max_length=64),
    status: ScanStatus | None = Query(default=None),
    context: TenantContext = Depends(request_tenant_context),
):
    """The Workspace's scans, newest first (at most 50)."""
    return await _call(scan_service.list_scans, context, product_id=product_id, status=status)


@router.get("/{scan_id}", operation_id="get_competitor_scan", response_model=CompetitorScan)
async def get_competitor_scan(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Status and stage, per-target progress, step evidence, both costs and key versions."""
    return await _call(scan_service.get_scan, context, scan_id)


@router.get(
    "/{scan_id}/discoveries",
    operation_id="list_competitor_scan_discoveries",
    response_model=CompetitorDiscoveryList,
)
async def list_competitor_scan_discoveries(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """The advertisers the scan's keywords found and its discover step
    recommends (``relevant`` or ``adjacent``), with each one's state."""
    return await _call(discovery_service.list_discoveries, context, scan_id)


@router.post(
    "/{scan_id}/discoveries/{advertiser_page_id}/decision",
    operation_id="decide_competitor_discovery",
    response_model=CompetitorDiscoveryDecisionResult,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def decide_competitor_discovery(
    scan_id: str,
    advertiser_page_id: str,
    body: CompetitorDiscoveryDecisionRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Accept (Owner or Member: a new competitor confirmed by the ads' page
    ID, no resolve, no charge) or reject one recommendation; appended and
    audited with the actor. Deciding what is already in effect answers it."""
    return await _call(
        discovery_service.decide, context, scan_id, advertiser_page_id,
        decision=body.decision, reason=body.reason, idempotency_key=idempotency_key,
    )


@router.get(
    "/{scan_id}/resume-quote",
    operation_id="quote_competitor_scan_resume",
    response_model=CompetitorScanQuote,
)
async def quote_competitor_scan_resume(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """What resuming would reserve: the remaining pages and LLM steps only."""
    return await _call(scan_service.quote_resume, context, scan_id)


@router.post(
    "/{scan_id}/resume",
    operation_id="resume_competitor_scan",
    response_model=CompetitorScan,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def resume_competitor_scan(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Continue a ``partial``, ``failed`` or ``uncertain`` scan under the same
    ID from its stored cursors: a new Job reserves only what is left and
    first reconciles any ``uncertain`` page by the free credit balance."""
    return await _call(scan_service.resume_scan, context, scan_id, idempotency_key=idempotency_key)


@router.post(
    "/{scan_id}/reconcile",
    operation_id="reconcile_competitor_scan",
    response_model=CompetitorScan,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def reconcile_competitor_scan(
    scan_id: str,
    body: CompetitorScanReconcileRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Owner only: mark a pending page "charged" or "not charged"; its cost
    is settled accordingly and the page is fetched again on resume."""
    return await _call(
        scan_service.reconcile_scan, context, scan_id,
        attempt_id=body.attempt_id, outcome=body.outcome, idempotency_key=idempotency_key,
    )


@router.get("/{scan_id}/ads", operation_id="list_competitor_scan_ads", response_model=CompetitorAdPage)
async def list_competitor_scan_ads(
    scan_id: str,
    competitor_id: str | None = Query(default=None, min_length=1, max_length=64),
    role: Literal["competitor", "self"] | None = Query(default=None),
    origin_kind: Literal["page", "keyword"] | None = Query(default=None),
    min_days_running: int | None = Query(default=None, ge=0, le=3650),
    display_format: str | None = Query(default=None, min_length=1, max_length=32),
    relevance: Literal["relevant", "adjacent", "unrelated", "unassessed"] | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    """The scan's observations with their origins and relevance. Filters: a
    competitor (its own page included), ``role`` (``self`` = the product's
    own page), ``origin_kind``, days running, display format and the
    relevance in effect (``unassessed``: not judged yet)."""
    page, refs = await _call(
        scan_service.list_ads, context, scan_id,
        competitor_id=competitor_id, role=role, origin_kind=origin_kind,
        min_days_running=min_days_running, display_format=display_format, cursor=cursor, limit=limit,
        relevance=relevance,
    )
    await scan_service.sign_thumbnails(_blob_store, page["items"], refs)
    return page


@router.post(
    "/{scan_id}/ads/{ad_id}/relevance",
    operation_id="flip_competitor_ad_relevance",
    response_model=CompetitorAdAssessment,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def flip_competitor_ad_relevance(
    scan_id: str,
    ad_id: str,
    body: CompetitorAdRelevanceFlip,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Set one observation's relevance (Owner or Member): a new revision,
    audited with the actor; earlier revisions stay in its history."""
    return await _call(
        scan_service.flip_relevance, context, scan_id, ad_id,
        relevance=body.relevance, idempotency_key=idempotency_key,
    )


@router.get(
    "/{scan_id}/ads/{ad_id}/assessments",
    operation_id="list_competitor_ad_assessments",
    response_model=CompetitorAdAssessmentList,
)
async def list_competitor_ad_assessments(
    scan_id: str,
    ad_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Every relevance revision of one observation, oldest first."""
    return await _call(scan_service.list_assessments, context, scan_id, ad_id)


@router.post(
    "/{scan_id}/ads/{ad_id}/tags",
    operation_id="confirm_competitor_ad_tags",
    response_model=CompetitorAdTagAssessment,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def confirm_competitor_ad_tags(
    scan_id: str,
    ad_id: str,
    body: CompetitorAdTagsConfirm,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Confirm or change one observation's tags (Owner or Member): a new
    revision, audited with the actor; the AI's suggestion stays in history."""
    return await _call(
        scan_service.confirm_tags, context, scan_id, ad_id,
        values=[item.model_dump() for item in body.tags], idempotency_key=idempotency_key,
    )


@router.get(
    "/{scan_id}/ads/{ad_id}/tags",
    operation_id="list_competitor_ad_tags",
    response_model=CompetitorAdTagList,
)
async def list_competitor_ad_tags(
    scan_id: str,
    ad_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Every tags revision of one observation, oldest first, with the frozen vocabulary."""
    return await _call(scan_service.list_tags, context, scan_id, ad_id)


@router.post(
    "/{scan_id}/snapshots",
    operation_id="refreeze_competitor_scan",
    response_model=CompetitorScanSnapshot,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def refreeze_competitor_scan(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Freeze the revisions in effect now (free, no LLM, any member).
    Unchanged revisions answer the existing snapshot (``created`` false)."""
    return await _call(scan_service.refreeze_scan, context, scan_id, idempotency_key=idempotency_key)


@router.get(
    "/{scan_id}/snapshots",
    operation_id="list_competitor_scan_snapshots",
    response_model=CompetitorScanSnapshotList,
)
async def list_competitor_scan_snapshots(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """The scan's snapshots, newest first; ``current`` marks the scan's own."""
    return await _call(scan_service.list_snapshots, context, scan_id)


@router.get(
    "/{scan_id}/snapshots/{snapshot_id}",
    operation_id="get_competitor_scan_snapshot",
    response_model=CompetitorScanSnapshot,
)
async def get_competitor_scan_snapshot(
    scan_id: str,
    snapshot_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """One snapshot with its frozen content (what a plan cites)."""
    return await _call(scan_service.get_snapshot, context, scan_id, snapshot_id)


@router.get(
    "/{scan_id}/summary",
    operation_id="get_competitor_scan_summary",
    response_model=CompetitorScanSnapshotSummary,
)
async def get_competitor_scan_summary(
    scan_id: str,
    snapshot_id: str | None = Query(default=None, min_length=1, max_length=64),
    context: TenantContext = Depends(request_tenant_context),
):
    """The summary of the scan's current snapshot (or ``snapshot_id``):
    clusters, unclustered, adjacent, long-running and own-page observations,
    dimension counts and the AI's one-sentence conclusion."""
    return await _call(scan_service.get_summary, context, scan_id, snapshot_id=snapshot_id)


@router.get(
    "/{scan_id}/recluster-quote",
    operation_id="quote_competitor_scan_recluster",
    response_model=CompetitorScanReclusterQuote,
)
async def quote_competitor_scan_recluster(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """What reclustering can cost at most; reserves nothing."""
    return await _call(scan_service.quote_recluster, context, scan_id)


@router.post(
    "/{scan_id}/recluster",
    operation_id="recluster_competitor_scan",
    response_model=CompetitorScan,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def recluster_competitor_scan(
    scan_id: str,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Paid (Owner or Member, Spend Guardrail applies): reserve ``cluster:1``
    for a Job of its own, which appends a new clusters revision and freezes
    once. Poll the Operation or the scan's ``reclusters``."""
    return await _call(scan_service.recluster_scan, context, scan_id, idempotency_key=idempotency_key)


@products_router.get(
    "/{product_id}/competitor-settings",
    operation_id="get_product_competitor_settings",
    response_model=ProductCompetitorSettings,
)
async def get_product_competitor_settings(
    product_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Whether drafting cites the latest competitor snapshot."""
    return await _call(scan_service.get_product_settings, context, product_id)


@products_router.put(
    "/{product_id}/competitor-settings",
    operation_id="update_product_competitor_settings",
    response_model=ProductCompetitorSettings,
    openapi_extra={"x-marketai-idempotent": True},
)
async def update_product_competitor_settings(
    product_id: str,
    body: ProductCompetitorSettingsUpdate,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Switch it (Owner or Member), audited with the actor."""
    return await _call(
        scan_service.update_product_settings, context, product_id,
        competitor_snapshot_in_drafts=body.competitor_snapshot_in_drafts, idempotency_key=idempotency_key,
    )
