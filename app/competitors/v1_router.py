"""``/api/v1/competitors`` -- the competitor list, resolve and page confirmation (Issue #269).

Thin HTTP adapter over :mod:`app.competitors.service`. Workspace and actor
come only from the verified identity (``request_tenant_context``); another
Workspace's competitor, product or resolve answers ``404`` exactly like an
unknown one. Members create, resolve and confirm; archiving is Owner-only.
Every command requires an ``Idempotency-Key`` (``x-marketai-idempotent``).

A link input (a Facebook page link with a numeric id or an Ad Library link
with ``view_all_page_id``) confirms the page on create, without a paid call.
A name (with an optional official site) needs a resolve: a separate paid
Job (``competitor.resolve``, spec §4.4) whose maximum cost the quote shows
before the user starts it.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.platform.blob_store_factory import build_blob_store
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import service

router = APIRouter(prefix="/competitors", tags=["competitors"])
#: Signs the short-lived GET of each candidate's archived avatar.
_blob_store = build_blob_store()


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _problem(exc: service.CompetitorProblem) -> HTTPException:
    return HTTPException(
        exc.status,
        {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "params": exc.params},
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


ExecutionMode = Literal["byok", "mock"]


class CompetitorCreateRequest(BaseModel):
    """One input of spec §4.1 in ``input``: a Facebook page link
    (``facebook.com/<alias>``, ``/profile.php?id=<id>``), an Ad Library link
    with ``view_all_page_id``, or a name. ``website`` (a URL or a bare
    domain) is the official site a resolve compares landing pages with.
    ``role=self`` adds the product's own page as a baseline."""

    model_config = {"extra": "forbid"}

    product_id: str = Field(min_length=1, max_length=64)
    input: str = Field(min_length=1, max_length=2048)
    website: str | None = Field(default=None, max_length=2048)
    role: Literal["competitor", "self"] = "competitor"


class CompetitorPageProbe(BaseModel):
    """One ``probe`` call: the first page of a candidate's active ads.
    ``total_active_ads`` is the provider's own count; ``landing_domains``
    are the registrable domains the sampled ads link to."""

    status: Literal["succeeded", "failed", "uncertain"]
    country: str
    total_active_ads: int | None = None
    sampled_ads: int = 0
    landing_domains: list[str] = Field(default_factory=list)
    error_code: str | None = None


class CompetitorCandidateAvatar(BaseModel):
    """A short-lived signed GET of the resolve's archived copy of a page's
    avatar (its 512px derivative), read like a board thumbnail."""

    url: str
    expires_at: str
    mime_type: str
    width: int | None = None
    height: int | None = None


class CompetitorCandidate(BaseModel):
    """A page a name search returned, scored by spec §4.2.

    ``rank`` is the order after scoring and the domain probe (0 first);
    ``provider_rank`` is the provider's order. ``name_match`` compares the
    normalized names (case, spaces and punctuation ignored); ``alias_match``
    compares with the alias of a pasted ``facebook.com/<alias>`` link.
    ``domain_hit`` is null when no official site was given or the page was
    not probed. ``avatar`` is the copy the resolve downloaded (free, from
    Meta's CDN only), never the provider's signed link; it is served only
    while the resolve is running or reusable and never on a confirmation.
    It is null when the page has no avatar or the download failed: the
    card shows a placeholder."""

    page_id: str
    page_name: str | None = None
    page_alias: str | None = None
    category: str | None = None
    likes: int | None = None
    ig_username: str | None = None
    ig_followers: int | None = None
    verification: str | None = None
    page_is_deleted: bool | None = None
    avatar: CompetitorCandidateAvatar | None = None
    provider_rank: int
    rank: int
    name_match: Literal["exact", "partial", "none"]
    alias_match: bool = False
    category_class: Literal["commercial", "other", "excluded"]
    excluded_reason: Literal["deleted", "category"] | None = None
    probe: CompetitorPageProbe | None = None
    domain_hit: bool | None = None


class CompetitorQuoteLine(BaseModel):
    operation: Literal["page_search", "probe"]
    calls: int
    credits: int
    usd: str


class CompetitorResolveQuote(BaseModel):
    """The maximum a resolve can cost: ``page_search`` x 1 + ``probe`` x 3
    (fewer probes are made when fewer candidates qualify; the rest is
    released). ``max_usd`` is what the Workspace pays: ``"0"`` under
    Controlled Mock, whose ``simulated_usd`` is an estimate only (null when
    the deployment has no price table). ``available_modes`` lists ``byok``
    only when a ``market_data`` key is bound and prices are configured;
    otherwise ``byok_unavailable_code`` says why, with the values its copy
    interpolates in ``byok_unavailable_params`` (the missing ``operations``
    of ``market_data_price_operation_missing``)."""

    competitor_id: str
    execution_mode: ExecutionMode
    available_modes: list[ExecutionMode]
    byok_unavailable_code: str | None = None
    byok_unavailable_params: dict[str, Any] = Field(default_factory=dict)
    provider: str
    query: str
    website_domain: str | None = None
    country: str
    max_credits: int
    max_usd: str
    simulated_usd: str | None = None
    currency: Literal["USD"] = "USD"
    pricing_version: str | None = None
    lines: list[CompetitorQuoteLine] = Field(default_factory=list)


class CompetitorResolveRequest(BaseModel):
    """Start a resolve. ``execution_mode`` defaults to the quote's default
    (``byok`` when available, else ``mock``); ``country`` (ISO 3166-1
    alpha-2) is the market the probes read active ads in."""

    model_config = {"extra": "forbid"}

    execution_mode: ExecutionMode | None = None
    country: str = Field(default="US", min_length=2, max_length=2)


class CompetitorResolveCost(BaseModel):
    """What the resolve's calls cost: settled from each call's
    ``credits_charged``. ``usd`` is real money (``"0"`` under Mock)."""

    calls: int
    credits_charged: int
    usd: str
    simulated_usd: str | None = None


class CompetitorResolveFailure(BaseModel):
    code: str
    retryable: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class CompetitorResolution(BaseModel):
    """A resolve: ``queued`` -> ``running`` -> ``completed``; ``failed``
    (the search failed, or a 402/401/403 stopped the probes -- candidates
    already found stay listed) or ``uncertain`` (a call's outcome is
    unknown; nothing is resent). ``waits`` counts how often it queued
    behind another market-data job on the same key (spec §5.1). A finished
    resolve is ``reusable`` for confirmation until ``expires_at`` (72 hours).
    ``auto_confirmed`` is true when it confirmed the page itself (exact name
    and domain hit, or the alias of a pasted page link)."""

    competitor_id: str
    resolve_job_id: str
    operation_id: str
    status: Literal["queued", "running", "completed", "failed", "uncertain"]
    execution_mode: ExecutionMode
    provider: str
    query: str
    website_domain: str | None = None
    country: str
    quote: CompetitorResolveQuote
    cost: CompetitorResolveCost | None = None
    candidates: list[CompetitorCandidate] = Field(default_factory=list)
    auto_confirmed: bool = False
    confirmation_revision: int | None = None
    waits: int = 0
    failure: CompetitorResolveFailure | None = None
    reusable: bool = False
    expires_at: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    replayed: bool = False


class CompetitorResolutionSummary(BaseModel):
    resolve_job_id: str
    operation_id: str
    status: Literal["queued", "running", "completed", "failed", "uncertain"]
    execution_mode: ExecutionMode
    auto_confirmed: bool = False
    reusable: bool = False
    expires_at: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


class CompetitorConfirmation(BaseModel):
    """The latest confirmed page: an append-only revision. ``source`` is
    ``link`` (a pasted link with a page id), ``auto`` (the resolve matched
    by exact name and domain, or by the pasted alias) or ``user``.
    ``candidate`` is the resolve's card for that page as it was confirmed
    (without the avatar)."""

    revision: int
    platform: Literal["meta"] = "meta"
    page_id: str
    page_alias: str | None = None
    page_name: str | None = None
    source: Literal["link", "auto", "user"]
    link_kind: Literal["facebook_page", "ad_library"] | None = None
    resolve_job_id: str | None = None
    domain_hit: bool | None = None
    candidate: CompetitorCandidate | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None


class Competitor(BaseModel):
    """A competitor of a product (or ``role=self``, the product's own page).
    ``input_kind`` is how it was added; ``can_resolve`` says whether a
    name search is possible (a name, or the alias of a pasted page link).

    Two read-only projections from the product's scans, neither a call nor
    a change to the confirmation: ``observed_page_name`` is the advertiser
    name of the confirmed page's newest observation (a link confirms a page
    without its name; null until a scan sees the page), and
    ``last_scanned_at`` is when the newest finished scan (``completed``,
    ``partial``, ``failed`` or ``uncertain``) that included this competitor
    finished (null when none has)."""

    competitor_id: str
    product_id: str
    role: Literal["competitor", "self"]
    status: Literal["active", "archived"]
    name: str | None = None
    website_domain: str | None = None
    input_kind: Literal["name", "facebook_page", "ad_library"]
    input_page_alias: str | None = None
    confirmation_status: Literal["confirmed", "unconfirmed"]
    confirmation: CompetitorConfirmation | None = None
    observed_page_name: str | None = None
    last_scanned_at: str | None = None
    latest_resolution: CompetitorResolutionSummary | None = None
    can_resolve: bool = False
    created_by: str | None = None
    created_at: str | None = None
    archived_by: str | None = None
    archived_at: str | None = None
    replayed: bool = False


class CompetitorList(BaseModel):
    items: list[Competitor] = Field(default_factory=list)


class CompetitorConfirmRequest(BaseModel):
    """Either a candidate of a reusable resolve (``resolve_job_id`` +
    ``page_id``) or a page link carrying a page id (``page_url``: a
    ``profile.php?id=`` link or an Ad Library ``view_all_page_id`` link).
    Confirming the page that is already confirmed from the same source
    returns that revision instead of appending another."""

    model_config = {"extra": "forbid"}

    resolve_job_id: str | None = Field(default=None, min_length=1, max_length=64)
    page_id: str | None = Field(default=None, min_length=1, max_length=32)
    page_url: str | None = Field(default=None, min_length=1, max_length=2048)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(lambda: fn(*args, **kwargs))
    except service.CompetitorProblem as exc:
        raise _problem(exc) from exc


@router.post(
    "",
    operation_id="create_competitor",
    response_model=Competitor,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_competitor(
    body: CompetitorCreateRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Add a competitor; a link with a page id confirms it at once."""
    return await _call(
        service.create_competitor, context,
        product_id=body.product_id, input_text=body.input, website=body.website,
        role=body.role, idempotency_key=idempotency_key,
    )


@router.get("", operation_id="list_competitors", response_model=CompetitorList)
async def list_competitors(
    product_id: str | None = Query(default=None, min_length=1, max_length=64),
    status: Literal["active", "archived", "all"] = Query(default="active"),
    context: TenantContext = Depends(request_tenant_context),
):
    """The Workspace's competitors (newest first, at most 200)."""
    return await _call(service.list_competitors, context, product_id=product_id, status=status)


@router.get("/{competitor_id}", operation_id="get_competitor", response_model=Competitor)
async def get_competitor(
    competitor_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return await _call(service.get_competitor, context, competitor_id)


@router.get(
    "/{competitor_id}/resolve-quote",
    operation_id="quote_competitor_resolve",
    response_model=CompetitorResolveQuote,
)
async def quote_competitor_resolve(
    competitor_id: str,
    execution_mode: ExecutionMode | None = Query(default=None),
    country: str = Query(default="US", min_length=2, max_length=2),
    context: TenantContext = Depends(request_tenant_context),
):
    """The maximum credits and USD a resolve can cost, before starting it."""
    return await _call(
        service.quote_resolve, context, competitor_id, execution_mode=execution_mode, country=country,
    )


@router.post(
    "/{competitor_id}/resolve",
    operation_id="resolve_competitor",
    response_model=CompetitorResolution,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def resolve_competitor(
    competitor_id: str,
    body: CompetitorResolveRequest = Body(default=None),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Reserve the quoted maximum and queue the resolve Job; poll the
    Operation (``GET /operations/{operation_id}``) or the resolve itself."""
    body = body or CompetitorResolveRequest()
    view, avatars = await _call(
        service.start_resolve, context, competitor_id,
        execution_mode=body.execution_mode, country=body.country, idempotency_key=idempotency_key,
    )
    await service.sign_avatars(_blob_store, view["candidates"], avatars)
    return view


@router.get(
    "/{competitor_id}/resolutions/{resolve_job_id}",
    operation_id="get_competitor_resolution",
    response_model=CompetitorResolution,
)
async def get_competitor_resolution(
    competitor_id: str,
    resolve_job_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Candidates, scores, domain probes and cost of one resolve."""
    view, avatars = await _call(service.get_resolution, context, competitor_id, resolve_job_id)
    await service.sign_avatars(_blob_store, view["candidates"], avatars)
    return view


@router.post(
    "/{competitor_id}/confirm",
    operation_id="confirm_competitor",
    response_model=Competitor,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def confirm_competitor(
    competitor_id: str,
    body: CompetitorConfirmRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Append a confirmation revision binding the competitor to one page."""
    return await _call(
        service.confirm_competitor, context, competitor_id,
        resolve_job_id=body.resolve_job_id, page_id=body.page_id, page_url=body.page_url,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{competitor_id}/archive",
    operation_id="archive_competitor",
    response_model=Competitor,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def archive_competitor(
    competitor_id: str,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Owner only. Archived competitors are not scanned; history stays."""
    del idempotency_key  # archiving an archived competitor answers the same view
    return await _call(service.archive_competitor, context, competitor_id)
