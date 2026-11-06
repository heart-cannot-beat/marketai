"""``/api/v1/external-creatives`` -- images made elsewhere join tests (Issue #168).

Thin HTTP adapter over :mod:`app.external_creatives.service`. Every command
requires an ``Idempotency-Key`` (``x-marketai-idempotent``). Workspace and
actor come only from the verified identity (``request_tenant_context``);
another Workspace's creative, product or upload id answers ``404`` exactly
like an unknown one.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.generation.thumbnails import attach_thumbnail_links, thumbnail_refs_for_versions
from app.platform.blob_store_factory import build_blob_store
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import service

router = APIRouter(prefix="/external-creatives", tags=["external-creatives"])

#: Signs the gallery thumbnail links the views embed (same seam as Assets).
_blob_store = build_blob_store()


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _problem(exc: service.ExternalCreativeProblem) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message, "retryable": exc.retryable})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RightsDeclaration(BaseModel):
    """Who made the images and the uploader's confirmation that the
    Workspace may use them in ads (``permission_confirmed`` must be true)."""

    model_config = {"extra": "forbid"}

    made_by: Literal["self", "designer", "agency", "creator", "ai_tool", "other"] = "self"
    permission_confirmed: bool = False
    note: str | None = Field(default=None, max_length=500)


class ExternalCreativeCreateRequest(BaseModel):
    """Finalized uploads of purpose ``external_creative`` (PNG/JPEG/WebP,
    <= 20 MB each, at most 10) for one product."""

    model_config = {"extra": "forbid"}

    product_id: str = Field(min_length=1, max_length=64)
    upload_intent_ids: list[str] = Field(min_length=1, max_length=10)
    rights: RightsDeclaration


class ThumbnailLink(BaseModel):
    url: str
    expires_at: str
    mime_type: str
    width: int | None = None
    height: int | None = None


class VocabularyValue(BaseModel):
    value_id: str
    label: str


class VocabularyFactor(BaseModel):
    """A factor of the product's vocabulary. ``source`` is ``test_plan``
    (merged across the product's test plans by key),
    ``dimension_template`` or ``expression_catalog`` (the expression
    mechanism catalog, one factor every product has; Issue #329)."""

    factor_id: str
    label: str
    source: str
    values: list[VocabularyValue] = Field(default_factory=list)


class VocabularyDirection(BaseModel):
    plan_id: str
    round: int | None = None
    direction_key: str
    title: str


class Vocabulary(BaseModel):
    factors: list[VocabularyFactor] = Field(default_factory=list)
    directions: list[VocabularyDirection] = Field(default_factory=list)


class SuggestedTag(BaseModel):
    """The model's pick for one factor: ``value_id`` null means "none of these"."""

    factor_id: str
    value_id: str | None = None
    confidence: float = 0
    reason: str = ""


class TagSuggestion(BaseModel):
    factors: list[SuggestedTag] = Field(default_factory=list)
    summary: str = ""
    provider: str | None = None
    model: str | None = None
    execution_mode: str | None = None
    preselect_confidence: float = 0.5


class ConfirmedTag(BaseModel):
    factor_id: str
    factor_label: str
    value_id: str | None = None
    value_label: str | None = None
    suggested_value_id: str | None = None
    suggested_confidence: float | None = None
    source: str


class ConfirmedDirection(BaseModel):
    plan_id: str | None = None
    direction_key: str | None = None
    title: str | None = None


class TagConfirmation(BaseModel):
    """The latest confirmed tags. ``experiment_ready`` says the image has
    concept identity (it can be frozen into an experiment);
    ``attributable`` says learnings can place it in a test round."""

    revision: int
    tags: list[ConfirmedTag] = Field(default_factory=list)
    direction: ConfirmedDirection | None = None
    plan_id: str | None = None
    round: int | None = None
    attributable: bool = False
    experiment_ready: bool = False
    concept_count: int = 0
    stable_dimension_signature: str | None = None
    review_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None


class CreativeFailure(BaseModel):
    code: str
    message: str
    detail: str | None = None
    suggested_action: str


class RightsView(BaseModel):
    made_by: str | None = None
    permission_confirmed: bool | None = None
    note: str | None = None
    declared_by: str | None = None
    declared_at: str | None = None


class ExternalCreativeDetail(BaseModel):
    """``status``: ``queued`` -> ``gating`` -> ``preparing_asset`` ->
    ``tagging`` -> ``ready`` (suggestion available) -> ``confirmed``; or
    ``rejected`` (upload gate), ``failed``, ``tagging_failed`` (the asset
    exists; tag it by hand). ``asset_code`` (``MK-XXXXXX``) is what a
    results sheet matches."""

    external_creative_id: str
    product_id: str
    status: str
    current_step: str | None = None
    execution_mode: str
    filename: str = ""
    mime_type: str | None = None
    byte_size: int = 0
    width: int | None = None
    height: int | None = None
    aspect_ratio: str | None = None
    rights: RightsView
    asset_id: str | None = None
    asset_version_id: str | None = None
    asset_code: str | None = None
    thumbnail: ThumbnailLink | None = None
    failure: CreativeFailure | None = None
    vocabulary: Vocabulary
    suggestion: TagSuggestion | None = None
    confirmation: TagConfirmation | None = None
    operation_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    replayed: bool = False


class ExternalCreativeList(BaseModel):
    items: list[ExternalCreativeDetail] = Field(default_factory=list)


class TagChoice(BaseModel):
    model_config = {"extra": "forbid"}

    factor_id: str = Field(min_length=1, max_length=200)
    value_id: str | None = Field(default=None, max_length=32)


class DirectionChoice(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str = Field(min_length=1, max_length=64)
    direction_key: str = Field(min_length=1, max_length=32)


class ExternalCreativeConfirmRequest(BaseModel):
    """The reviewed tags (one per factor; ``value_id`` null or omitted factor
    = "none of these"), an optional direction, and whether confirming also
    approves the image for tests (default true)."""

    model_config = {"extra": "forbid"}

    tags: list[TagChoice] = Field(default_factory=list, max_length=24)
    direction: DirectionChoice | None = None
    approve: bool = True


async def _with_thumbnails(items: list[dict[str, Any]], context: TenantContext) -> None:
    from app.adapters.persistence.database import tenant_session

    def _refs():
        with tenant_session(context) as session:
            return thumbnail_refs_for_versions(session, context, (item.get("asset_version_id") for item in items))

    refs = await run_in_threadpool(_refs)
    await attach_thumbnail_links(_blob_store, items, refs, version_id_key="asset_version_id")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    operation_id="create_external_creatives",
    response_model=ExternalCreativeList,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_external_creatives(
    body: ExternalCreativeCreateRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Admit uploaded images: gate, store as external AssetVersions, tag."""
    try:
        result = await run_in_threadpool(
            lambda: service.create_external_creatives(
                context,
                product_id=body.product_id,
                upload_intent_ids=body.upload_intent_ids,
                rights=body.rights.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        )
    except service.ExternalCreativeProblem as exc:
        raise _problem(exc) from exc
    await _with_thumbnails(result["items"], context)
    return result


@router.get(
    "",
    operation_id="list_external_creatives",
    response_model=ExternalCreativeList,
)
async def list_external_creatives(
    product_id: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
    context: TenantContext = Depends(request_tenant_context),
):
    """The Workspace's external creatives (newest first, at most 100)."""
    try:
        result = await run_in_threadpool(
            lambda: service.list_external_creatives(context, product_id=product_id, status=status)
        )
    except service.ExternalCreativeProblem as exc:
        raise _problem(exc) from exc
    await _with_thumbnails(result["items"], context)
    return result


@router.get(
    "/{external_creative_id}",
    operation_id="get_external_creative",
    response_model=ExternalCreativeDetail,
)
async def get_external_creative(
    external_creative_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Status, the vocabulary, the AI suggestion and the confirmed tags."""
    try:
        view = await run_in_threadpool(lambda: service.get_external_creative(context, external_creative_id))
    except service.ExternalCreativeProblem as exc:
        raise _problem(exc) from exc
    await _with_thumbnails([view], context)
    return view


@router.post(
    "/{external_creative_id}/confirm",
    operation_id="confirm_external_creative",
    response_model=ExternalCreativeDetail,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
async def confirm_external_creative(
    external_creative_id: str,
    body: ExternalCreativeConfirmRequest = Body(default=None),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Confirm (or correct) the tags: the image gets the concept identity a
    generated image with the same values carries."""
    body = body or ExternalCreativeConfirmRequest()
    try:
        view = await run_in_threadpool(
            lambda: service.confirm_external_creative(
                context, external_creative_id,
                tags=[tag.model_dump() for tag in body.tags],
                direction=body.direction.model_dump() if body.direction else None,
                approve=body.approve,
                idempotency_key=idempotency_key,
            )
        )
    except service.ExternalCreativeProblem as exc:
        raise _problem(exc) from exc
    await _with_thumbnails([view], context)
    return view
