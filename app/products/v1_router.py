"""``/api/v1/products/intake`` -- the "set the product" front flow (Issue #161).

Thin HTTP adapter over :mod:`app.products.intake_service`. Every command
requires an ``Idempotency-Key`` (``x-marketai-idempotent``). Workspace and
actor come only from the verified identity (``request_tenant_context``);
another Workspace's intake or upload id answers ``404`` exactly like an
unknown one.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.platform.blob_store_factory import build_blob_store
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import catalog, intake_service, reference_sets
from app.library import service as library

router = APIRouter(prefix="/products", tags=["products"])

#: Signs short-lived links to released image derivatives (same seam as the
#: Library reference download intent).
_blob_store = build_blob_store()


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _problem(exc: intake_service.IntakeProblem) -> HTTPException:
    return HTTPException(
        exc.status,
        {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "params": exc.params},
    )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProductIntakeCreateRequest(BaseModel):
    """At least one of ``url``, ``upload_intent_ids`` (finalized uploads of
    purpose ``product_intake_image``) or ``text``."""

    model_config = {"extra": "forbid"}

    url: str | None = Field(default=None, max_length=2048)
    upload_intent_ids: list[str] | None = Field(default=None, max_length=12)
    text: str | None = Field(default=None, max_length=20000)


class ProductIntakeAccepted(BaseModel):
    """202: the intake and the Operation to poll (``GET /operations/{id}``)
    -- or ``GET /products/intake/{intake_id}`` for the card itself.
    ``replayed`` is true when the same input (within 24 h) or the same
    Idempotency-Key returned an existing intake."""

    intake_id: str
    operation_id: str | None = None
    status: str
    replayed: bool = False


class AudienceCard(BaseModel):
    who: str = Field(min_length=1, max_length=500)
    why: str = Field(default="", max_length=500)
    pain: str = Field(default="", max_length=500)


class MustMatchView(BaseModel):
    id: str | None = None
    type: str
    description: str
    severity: str | None = None
    proposed_by: str | None = None
    confirmed: bool | None = None


class ModelVariant(BaseModel):
    """One variant the source sells: a model, size or power, or a colour /
    appearance edition of the same model (Issue #192). ``specs`` are only the
    specs that differ; ``appearance`` are its own must-match items (colour,
    material, pattern) and ``replaces_must_match_ids`` the shared items they
    supersede once it is chosen; ``image_ids`` are the candidate images that
    show it (an image no variant claims is general), and
    ``default_image_ids`` what the card preselects when it is chosen: its
    own and the general images, never another variant's."""

    label: str
    specs: dict[str, str] = Field(default_factory=dict)
    appearance: list[MustMatchView] = Field(default_factory=list)
    replaces_must_match_ids: list[str] = Field(default_factory=list)
    image_ids: list[str] = Field(default_factory=list)
    default_image_ids: list[str] = Field(default_factory=list)


class IntakeImage(BaseModel):
    """A candidate image. ``gate_state`` is the reference gate's verdict
    (``released`` images are ``selectable`` and can be previewed through the
    download-intent route); ``role`` is the model's ranking role."""

    image_id: str
    rank: int | None = None
    role: str | None = None
    source: str
    page_source: str | None = None
    source_url: str | None = None
    alt: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    gate_state: str | None = None
    gate_code: str | None = None
    reference_object_id: str | None = None
    selectable: bool = False
    #: The variant this image shows; ``None`` for a general image.
    variant_label: str | None = None


class ProductCardDraft(BaseModel):
    """The AI's first draft. Every field is editable on confirm; ``must_match``
    items carry ``proposed_by: "ai"`` and ``confirmed: false`` until then."""

    name: str | None = None
    description: str | None = None
    specs: dict[str, str] = Field(default_factory=dict)
    model_variants: list[ModelVariant] = Field(default_factory=list)
    requires_variant_selection: bool = False
    selling_points: list[str] = Field(default_factory=list)
    audiences: list[AudienceCard] = Field(default_factory=list)
    must_match: list[MustMatchView] = Field(default_factory=list)
    images: list[IntakeImage] = Field(default_factory=list)
    default_image_ids: list[str] = Field(default_factory=list)
    source_url: str | None = None
    dropped_commercial_specs: list[str] = Field(default_factory=list)


class IntakeFailure(BaseModel):
    """Why the intake stopped. ``code`` is stable (``access_denied``,
    ``timeout``, ``not_product_page``, ``blocked_address``,
    ``extraction_invalid``, ``credential_unavailable`` ...);
    ``suggested_action`` tells the UI what to offer
    (``paste_text_or_screenshot``, ``check_link``, ``retry`` ...)."""

    code: str
    step: str | None = None
    message: str
    detail: str | None = None
    suggested_action: str
    retryable: bool = False
    blocked_category: str | None = None


class IntakeInputSummary(BaseModel):
    url: str | None = None
    text_chars: int = 0
    upload_count: int = 0


class BoundImage(BaseModel):
    image_id: str
    reference_object_id: str
    reference_entry_id: str
    reference_version_id: str
    viewpoint: str


class ConfirmedProduct(BaseModel):
    product_id: str | None = None
    entry_id: str | None = None
    version_id: str | None = None
    version_no: int | None = None
    confirmed_at: str | None = None
    card: dict[str, Any] = Field(default_factory=dict)
    bound_images: list[BoundImage] = Field(default_factory=list)


class ProductIntakeDetail(BaseModel):
    """``status``: ``queued`` -> ``fetching`` -> ``extracting`` ->
    ``preparing_images`` -> ``ready`` (card available) -> ``confirmed``;
    or ``fetch_failed`` / ``extraction_failed`` / ``failed`` with
    ``failure``. ``evidence`` holds per-step outcome, duration and digests."""

    intake_id: str
    operation_id: str | None = None
    status: str
    current_step: str | None = None
    execution_mode: str
    input: IntakeInputSummary
    failure: IntakeFailure | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    card: ProductCardDraft | None = None
    confirmation: ConfirmedProduct | None = None
    product_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    replayed: bool = False


class MustMatchEdit(BaseModel):
    model_config = {"extra": "forbid"}

    id: str | None = Field(default=None, max_length=64)
    type: Literal["color", "logo", "visual", "material", "text", "other"] = "visual"
    description: str = Field(min_length=1, max_length=500)
    severity: Literal["blocking", "warning"] | None = None


class AudienceCardEdit(BaseModel):
    model_config = {"extra": "forbid"}

    who: str = Field(min_length=1, max_length=500)
    why: str = Field(default="", max_length=500)
    pain: str = Field(default="", max_length=500)


class ProductCardEdits(BaseModel):
    """The user's edits; an omitted field keeps the AI draft's value."""

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    specs: dict[str, str] | None = None
    selling_points: list[str] | None = Field(default=None, max_length=10)
    audiences: list[AudienceCardEdit] | None = Field(default=None, max_length=5)
    primary_audience_index: int | None = Field(default=None, ge=0)
    must_match: list[MustMatchEdit] | None = Field(default=None, max_length=20)


class ProductIntakeConfirmRequest(BaseModel):
    """Confirm (and publish) the card. ``selected_variant`` is required when
    the draft has more than one ``model_variants`` entry; only that variant
    enters the version. ``image_ids`` defaults to the chosen variant's
    ``default_image_ids`` (``card.default_image_ids`` without variants);
    an omitted ``card.must_match`` defaults to the shared items plus the
    chosen variant's ``appearance``, and an omitted ``card.specs`` to the
    shared specs with the variant's laid over them. ``entry_id`` updates an
    existing product instead of creating one."""

    model_config = {"extra": "forbid"}

    card: ProductCardEdits | None = None
    selected_variant: str | None = Field(default=None, max_length=100)
    image_ids: list[str] | None = Field(default=None, max_length=8)
    entry_id: str | None = Field(default=None, max_length=64)
    acknowledged_warnings: list[str] = Field(default_factory=list, max_length=20)


class IntakeImageDownloadGrant(BaseModel):
    intake_id: str
    image_id: str
    reference_object_id: str
    download_url: str
    expires_at: str
    mime_type: str
    byte_size: int
    width: int
    height: int


class ProductLatestRun(BaseModel):
    """The product's most recent Generation Run, summarized for the home card."""

    status: str
    done: int = 0
    total: int = 0
    failed: int = 0
    created_at: str | None = None


class ProductSummary(BaseModel):
    """One product as the front flow lists it (Issue #162). ``name`` is the
    effective ProductVersion's name (``null`` only when nothing names it);
    ``ready`` is whether a new Generation draft may use it;
    ``primary_reference_version_id`` previews through
    ``create_library_reference_download_intent``."""

    product_id: str
    entry_id: str | None = None
    name: str | None = None
    variant_label: str | None = None
    ready: bool = False
    primary_reference_version_id: str | None = None
    latest_run: ProductLatestRun | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ProductList(BaseModel):
    items: list[ProductSummary] = Field(default_factory=list)


class ProductReferenceItem(BaseModel):
    item_id: str
    reference_object_id: str
    reference_version_id: str
    model_variant: str = ""
    role: Literal["primary", "detail", "logo", "certification_mark"]
    identity_status: Literal["verified_physical", "unverified"]
    note: str = ""
    added_by: str | None = None
    added_at: str
    available: bool


class ProductReferenceSet(BaseModel):
    """A projection of the product's immutable Library version/bindings.

    ``revision`` is the If-Match value for edits. Deleted items remain in
    prior ProductVersions; ``version_id`` can be frozen by later consumers.
    """
    product_id: str
    entry_id: str
    version_id: str
    revision: int
    model_variants: list[str]
    has_unpublished_changes: bool = False
    items: list[ProductReferenceItem]


class AddProductReference(BaseModel):
    model_config = {"extra": "forbid"}
    reference_object_id: str | None = Field(default=None, min_length=1, max_length=64)
    reference_version_id: str | None = Field(default=None, min_length=1, max_length=64)
    model_variant: str = Field(default="", max_length=100)
    role: Literal["primary", "detail", "logo", "certification_mark"] = "detail"
    identity_status: Literal["verified_physical", "unverified"] = "unverified"
    note: str = Field(default="", max_length=1200)

    @model_validator(mode="after")
    def reference_identity(self):
        if bool(self.reference_object_id) == bool(self.reference_version_id):
            raise ValueError("Choose exactly one published reference object or version.")
        return self


class UpdateProductReference(BaseModel):
    model_config = {"extra": "forbid"}
    role: Literal["primary", "detail", "logo", "certification_mark"] | None = None
    identity_status: Literal["verified_physical", "unverified"] | None = None
    note: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Provide a role, identity status or note.")
        return self


def _reference_error(exc):
    if isinstance(exc, library.LibraryNotFound):
        return HTTPException(404, {"code": "product_reference_not_found", "message": str(exc)})
    if isinstance(exc, library.LibraryForbidden):
        return HTTPException(403, {"code": "product_reference_forbidden", "message": str(exc)})
    if isinstance(exc, library.IdempotencyConflict):
        return HTTPException(409, {"code": "idempotency_key_reused", "message": str(exc)})
    if isinstance(exc, library.RevisionConflict):
        return HTTPException(409, {"code": "revision_conflict", "message": str(exc)})
    if isinstance(exc, library.LibraryConflict):
        return HTTPException(409, {"code": exc.code, "message": str(exc)})
    return HTTPException(422, {"code": "product_reference_validation_failed", "message": str(exc)})


def _reference_revision(if_match: int = Header(..., alias="If-Match", ge=1)):
    return if_match


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    operation_id="list_products",
    response_model=ProductList,
)
def list_products(
    context: TenantContext = Depends(request_tenant_context),
):
    """The Workspace's products with display name, main image and latest Run,
    newest first -- the read behind the product home, product names on
    Generation pages and the New Draft product picker."""
    return {"items": catalog.list_products(context)}


@router.get("/{product_id}/references", operation_id="get_product_references", response_model=ProductReferenceSet)
def get_product_references(product_id: str, context: TenantContext = Depends(request_tenant_context)):
    """Published product/model references, including unavailable items and draft conflicts."""
    try:
        return reference_sets.get(context, product_id)
    except library.LibraryError as exc:
        raise _reference_error(exc) from exc


@router.post("/{product_id}/references", operation_id="add_product_reference", response_model=ProductReferenceSet,
             openapi_extra={"x-marketai-idempotent": True})
def add_product_reference(product_id: str, body: AddProductReference,
    context: TenantContext = Depends(request_tenant_context),
    revision: int = Depends(_reference_revision), idempotency_key: str = Depends(_idempotency_key)):
    """Bind a released, promoted object/version after the governed upload flow.

    No binary ingestion or in-request file gate. Publishes one ProductVersion
    atomically, preserving all previous bindings and frozen generation inputs.
    """
    try:
        return reference_sets.mutate(context, product_id, action="add", expected_revision=revision,
            payload=body.model_dump(mode="json"), idempotency_key=idempotency_key)
    except library.LibraryError as exc:
        raise _reference_error(exc) from exc


@router.patch("/{product_id}/references/{item_id}", operation_id="update_product_reference",
              response_model=ProductReferenceSet, openapi_extra={"x-marketai-idempotent": True})
def update_product_reference(product_id: str, item_id: str, body: UpdateProductReference,
    context: TenantContext = Depends(request_tenant_context),
    revision: int = Depends(_reference_revision), idempotency_key: str = Depends(_idempotency_key)):
    """Append a product version with changed role, identity attestation or note."""
    try:
        return reference_sets.mutate(context, product_id, action="update", item_id=item_id,
            expected_revision=revision, payload=body.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key)
    except library.LibraryError as exc:
        raise _reference_error(exc) from exc


@router.delete("/{product_id}/references/{item_id}", operation_id="delete_product_reference",
               response_model=ProductReferenceSet, openapi_extra={"x-marketai-idempotent": True})
def delete_product_reference(product_id: str, item_id: str,
    context: TenantContext = Depends(request_tenant_context),
    revision: int = Depends(_reference_revision), idempotency_key: str = Depends(_idempotency_key)):
    """Remove only from the new version; retain file, old bindings and history."""
    try:
        return reference_sets.mutate(context, product_id, action="delete", item_id=item_id,
            expected_revision=revision, payload={}, idempotency_key=idempotency_key)
    except library.LibraryError as exc:
        raise _reference_error(exc) from exc


@router.post(
    "/intake",
    operation_id="create_product_intake",
    response_model=ProductIntakeAccepted,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_product_intake(
    body: ProductIntakeCreateRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Start recognizing a product from a link, images and/or text."""
    try:
        view = intake_service.create_intake(
            context,
            url=body.url,
            upload_intent_ids=body.upload_intent_ids,
            text_input=body.text,
            idempotency_key=idempotency_key,
        )
    except intake_service.IntakeProblem as exc:
        raise _problem(exc) from exc
    return {
        "intake_id": view["intake_id"],
        "operation_id": view["operation_id"],
        "status": view["status"],
        "replayed": view["replayed"],
    }


@router.get(
    "/intake/{intake_id}",
    operation_id="get_product_intake",
    response_model=ProductIntakeDetail,
)
def get_product_intake(
    intake_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Status, per-step evidence and the product-card draft."""
    try:
        return intake_service.get_intake(context, intake_id)
    except intake_service.IntakeProblem as exc:
        raise _problem(exc) from exc


@router.post(
    "/intake/{intake_id}/confirm",
    operation_id="confirm_product_intake",
    response_model=ProductIntakeDetail,
    status_code=200,
    openapi_extra={"x-marketai-idempotent": True},
)
def confirm_product_intake(
    intake_id: str,
    body: ProductIntakeConfirmRequest = Body(default=None),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Publish the (edited) card as a ProductVersion and return its ``prod_`` id."""
    body = body or ProductIntakeConfirmRequest()
    try:
        return intake_service.confirm_intake(
            context, intake_id,
            card=body.card.model_dump(exclude_none=True, mode="json") if body.card else None,
            selected_variant=body.selected_variant,
            image_ids=body.image_ids,
            entry_id=body.entry_id,
            acknowledged_warnings=body.acknowledged_warnings,
            idempotency_key=idempotency_key,
        )
    except intake_service.IntakeProblem as exc:
        raise _problem(exc) from exc


@router.post(
    "/intake/{intake_id}/images/{image_id}/download-intent",
    operation_id="create_product_intake_image_download_intent",
    response_model=IntakeImageDownloadGrant,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_product_intake_image_download_intent(
    intake_id: str,
    image_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """A short-lived signed link to a candidate image's released derivative
    (the browser previews candidates before confirming; never the
    un-gated original)."""
    from datetime import datetime, timezone

    from app.adapters.persistence.database import tenant_session
    from app.platform.blob_store import DownloadSpec
    from app.platform.download_intents import (
        DEFAULT_DOWNLOAD_TTL_SECONDS,
        create_or_replay_download_intent,
    )
    from app.platform.idempotency import compute_fingerprint

    try:
        facts = intake_service.resolve_image_derivative(context, intake_id, image_id)
    except intake_service.IntakeProblem as exc:
        raise _problem(exc) from exc
    committed_ref = facts["committed_ref"]
    fingerprint_hash = compute_fingerprint(
        "POST", "create_product_intake_image_download_intent", request.url.path, {}, None, {},
    )
    with tenant_session(context) as session:
        intent = create_or_replay_download_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="products.intake_image_download_intent",
            fingerprint_hash=fingerprint_hash,
            owner_kind="product_intake_image",
            owner_id=facts["reference_object_id"],
            committed_ref=committed_ref,
        )
        expires_at = intent.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    remaining = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    grant = await _blob_store.issue_download(
        committed_ref,
        DownloadSpec(
            filename=f"{image_id}.jpg",
            expires_in_seconds=min(DEFAULT_DOWNLOAD_TTL_SECONDS, remaining),
        ),
    )
    return {
        "intake_id": intake_id,
        "image_id": image_id,
        "reference_object_id": facts["reference_object_id"],
        "download_url": grant.url,
        "expires_at": grant.expires_at.isoformat(),
        "mime_type": facts["mime_type"],
        "byte_size": int(facts["byte_size"] or 0),
        "width": int(facts["width"] or 0),
        "height": int(facts["height"] or 0),
    }
