"""Generation v1 sub-router -- 25 operations under ``/api/v1/generation``
(Issue #27 Task 3).

Mounted on the shared ``v1_router`` in ``app.main``, following
``app.library.v1_router``'s pattern exactly: every route is a thin HTTP
adapter over the existing Generation domain modules (``draft``, ``preview``,
``submission``, ``export``, ``review``, ``regeneration``) plus the new
read/command projections this task adds (``run_queries``, ``run_commands``).
No ``store``/``manager`` legacy imports, no direct database access outside a
``tenant_session``. Each route carries an explicit ``operation_id`` and
``response_model``; every command that has side effects advertises
``x-marketai-idempotent: true`` via ``openapi_extra`` and requires an
``Idempotency-Key`` header (422 if missing/blank).

Revision / If-Match: Drafts and Runs both carry an optimistic-concurrency
``revision`` -- ``update_draft``/``create_preview``/``submit_generation`` and
the four Run commands (``cancel``/``retry_failed_items``/
``resume_paused_items``/``reconcile``) all read it from ``If-Match`` and pass
it to the domain layer as ``expected_revision``. AssetVersions are immutable
(no revision counter): reviews and regeneration-drafts require no If-Match.

``allowed_actions`` on Draft/Preview/Run/RunItem/AssetVersion responses are
computed server-side (``app.generation.run_queries`` for reads; this module
for the Draft/Preview shapes since ``draft``/``preview`` return bare ORM
records) via ``app.platform.action_eligibility.compute_actions``, so the
browser never reconstructs eligibility rules client-side.

Errors raised by ``draft``/``preview``/``submission``/``export``
(``DraftNotFoundError``, ``StaleRevisionError``, ``PreviewError``,
``SubmissionError``, ``ExportError``, ``IdempotencyKeyReusedError``)
propagate to the problem handlers ``app.generation.api.
register_problem_handlers`` already registers app-wide in ``app.main`` --
this router registers no handlers for them. The error types this task
introduces (``GenerationResourceNotFoundError``, ``RunCommandError``,
``ReviewError``, ``RegenerationError``, and Issue #49 Task 2's
``AssetBlobError``) are registered by :func:`register_v1_problem_handlers`
below, called from ``app.main`` alongside ``register_generation_problem_handlers``.

The asset-version download-intent route (Issue #49 Task 2) resolves the
AssetVersion's exact committed blob reference server-side
(``app.generation.asset_queries.resolve_asset_blob_ref``), persists an
idempotent ``DownloadIntentRecord``
(``app.platform.download_intents.create_or_replay_download_intent``), and
issues a signed capability via the process's shared ``BlobStore``. The
export-package download-intent route below it follows the exact same
pattern against ``app.generation.export.resolve_export_blob_ref`` (Issue
#49 Task 9).

``POST /exports`` (Issue #49 Task 9) returns 202 with
``{ package_id, operation_id, state }`` plus a ``Location`` header pointing
at the Operation -- ``app.generation.export_worker.handle_export_package``
is the Worker Job handler that builds the deterministic ZIP and transitions
the package/Operation to a terminal state; ``GET /exports/{id}`` polls the
package's own state for refresh recovery.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import QualityReviewRecord, ResolutionPreviewRecord
from app.generation.models import AssetQCAttemptRecord, GenerationDraftRecord
from app.platform.blob_store import DownloadSpec, extension_for_mime
from app.platform.blob_store_factory import build_blob_store
from app.platform.download_intents import (
    DEFAULT_DOWNLOAD_TTL_SECONDS,
    create_or_replay_download_intent,
)
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.idempotency import compute_fingerprint, create_or_replay_intent
from app.platform.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.platform.tenancy import TenantContext, new_request_id
from app.platform.idempotency import compute_fingerprint, create_or_replay_intent
from .selection import product_input

from . import capabilities as capabilities_module
from . import draft as draft_module
from . import export as export_module
from . import preview as preview_module
from . import recovery_preview as recovery_preview_module
from . import regeneration as regeneration_module
from . import review as review_module
from . import run_commands
from . import run_queries
from . import submission as submission_module
from .asset_queries import (
    AssetBlobError,
    GalleryQueryError,
    get_asset_version_detail,
    list_asset_versions,
    resolve_asset_blob_ref,
)
from .thumbnails import attach_thumbnail_links, thumbnail_refs_for_versions
from .recovery_preview import RecoveryPreviewError
from .run_commands import RunCommandError
from .run_queries import GenerationResourceNotFoundError
from .regeneration import RegenerationError
from .review import ReviewConflictError, ReviewError
from .response_models import (
    AssetGalleryPage,
    AssetReview,
    AssetVersionDetail,
    DownloadIntentResponse,
    ExportManifest,
    ExportPackageDetail,
    ExportPackageQueued,
    GenerationCapabilities,
    GenerationDraftDetail,
    GenerationPreviewDetail,
    GenerationRecoveryPreview,
    GenerationResolutionPreview,
    GenerationRunDetail,
    GenerationRunItemDetail,
    OperationRef,
    Page,
    RecoveryPreviewInput,
    RegenerationDraftBatchResult,
    RunCommandResult,
)

router = APIRouter(prefix="/generation", tags=["generation"])

#: Shared BlobStore Adapter for this process (spec §7) -- built once at
#: import time, matching ``app.main``'s ``_blob_store`` and
#: ``app.worker_runtime``'s ``self.blob_store`` (each process/module holds
#: its own instance rather than sharing one across process boundaries).
_blob_store = build_blob_store()


# --------------------------------------------------------------------------
# Idempotency-Key -- required on every command (see module docstring).
# --------------------------------------------------------------------------


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


# --------------------------------------------------------------------------
# Pagination -- same opaque offset cursor as app.library.v1_router.
# --------------------------------------------------------------------------


def _paginate(items: list, *, context: TenantContext, filters_key: str,
              cursor: str | None, limit: int) -> dict:
    start = 0
    if cursor:
        try:
            decoded = decode_cursor(cursor, context.workspace_id, filters_key)
        except InvalidCursorError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_cursor", "message": str(exc)},
            ) from exc
        start = int(decoded[0]) if decoded else 0
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        next_cursor = encode_cursor(context.workspace_id, filters_key, (start + limit,))
    return {"items": page, "next_cursor": next_cursor}


def _limit_query(default: int = 50) -> int:
    return Query(default=default, ge=1, le=100)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------
# Draft / Preview projections -- draft.py and preview.py return bare ORM
# records (they own CAS mutation, not BFF shaping); this module builds the
# dict shape + allowed_actions the same way app.generation.run_queries does
# for Run/RunItem/AssetVersion.
# --------------------------------------------------------------------------


def _resource_command(session, context, request, key, command, body, create, replay):
    fingerprint = compute_fingerprint(request.method, command, request.url.path, {}, body,
                                      {"If-Match": request.headers["If-Match"]}
                                      if "If-Match" in request.headers else {})
    def create_resource(s):
        resource = create(s)
        return command, resource.id, resource
    return create_or_replay_intent(
        session, idempotency_key=key, command_type=command,
        fingerprint_hash=fingerprint, create_fn=create_resource,
        replay_fn=lambda _, resource_id, s: replay(s, resource_id),
    )

def _draft_dict(d: GenerationDraftRecord) -> dict:
    return {
        "id": d.id,
        "product_id": d.product_id,
        "provider": d.provider,
        "model": d.model,
        "execution_mode": d.execution_mode,
        "state": d.state,
        "revision": d.revision,
        "updated_at": _iso(d.updated_at),
        "selectors": d.selectors_json,
        "combination_strategy": d.combination_strategy_json,
        "format_config": d.format_json,
        "exclusions": d.exclusions_json,
        "regeneration_intent": d.regeneration_intent,
        "source_asset_version_id": d.source_asset_version_id,
        "source_recipe_identity": d.source_recipe_identity,
    }


def _finalize_draft(d: GenerationDraftRecord) -> dict:
    payload = _draft_dict(d)
    payload["allowed_actions"] = run_queries.draft_actions(d)
    return payload


def _preview_dict(p: ResolutionPreviewRecord) -> dict:
    proj = (p.snapshot_json or {}).get("ui_projections", {})
    return {
        "purpose": "initial_generation",
        "id": p.id,
        "preview_hash": p.preview_hash,
        "recipe_input_hash": p.recipe_input_hash,
        "execution_mode": p.execution_mode,
        "valid_until": _iso(p.valid_until),
        "draft_id": p.generation_draft_id,
        "draft_revision": p.draft_revision,
        "invalidation_reason": p.invalidation_reason or None,
        "combination_count": proj.get("combination_count", 0),
        "call_count": proj.get("call_count", 0),
        "maximum_cost": proj.get("maximum_cost"),
        "final_prompt": proj.get("final_prompt"),
        "items": proj.get("items", []),
        "capability_decisions": proj.get("capability_decisions", []),
        "exclusion_summary": proj.get("exclusion_summary"),
        "skipped_signature_count": proj.get("skipped_signature_count", 0),
        "skipped_call_count": proj.get("skipped_call_count", 0),
        "estimated_cost_saving": proj.get("estimated_cost_saving"),
        "strategy_applied": proj.get("strategy_applied"),
        "combinations_dropped_by_constraints": proj.get(
            "combinations_dropped_by_constraints", 0
        ),
    }


def _finalize_preview(p: ResolutionPreviewRecord) -> dict:
    payload = _preview_dict(p)
    payload["allowed_actions"] = run_queries.preview_actions(p)
    recipe = (p.snapshot_json or {}).get("recipe") or {}
    selection = recipe.get("selection") or {}
    if selection:
        quote = selection["cost_quote"]
        if selection.get("image_tool"):
            payload["image_tool"] = selection["image_tool"]
        payload.update({"prompt": selection["prompt"], "quantity": quote["quantity"], "selection_maximum_cost": quote["maximum_amount"], "maximum_cost": {"amount": quote["maximum_amount"], "currency": quote["currency"]}, "currency": quote["currency"], "pricing_version": quote["pricing_version"], "provider": recipe["provider"], "model": recipe["model"], "format_config": recipe["format"], "submitted_run_id": p.submitted_run_id})
    return payload


def _recovery_preview_dict(p: ResolutionPreviewRecord) -> dict:
    frozen = p.recovery_target_json.get("items", [])
    snapshot = p.snapshot_json or {}
    return {
        "purpose": "run_recovery",
        "id": p.id,
        "preview_hash": p.preview_hash,
        "command": p.recovery_command_kind or "",
        "targets": [t["id"] for t in frozen],
        "estimated_additional_cost": {
            "amount": str(p.recovery_max_additional_cost or 0),
            "currency": p.recovery_cost_currency or "USD",
        },
        "valid_until": _iso(p.valid_until),
        "run_id": p.recovery_run_id or "",
        "expected_run_revision": snapshot.get("expected_run_revision", 0),
    }


def _finalize_recovery_preview(p: ResolutionPreviewRecord) -> dict:
    payload = _recovery_preview_dict(p)
    payload["allowed_actions"] = run_queries.recovery_preview_actions(p)
    return payload


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@router.get("/capabilities", operation_id="get_generation_capabilities",
            response_model=GenerationCapabilities)
def get_capabilities(
    request: Request,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    import hashlib, json
    with tenant_session(context) as session:
        result = capabilities_module.get_capabilities(session, context)
    content_hash = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]
    etag = f'"{content_hash}"'
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


@router.get("/drafts", operation_id="list_generation_drafts", response_model=Page)
def list_drafts(
    product_id: str | None = Query(default=None),
    state: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        items = run_queries.list_drafts(session, context, product_id=product_id, state=state)
    filters_key = f"drafts:{product_id or ''}:{state or ''}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@router.get("/drafts/{draft_id}", operation_id="get_generation_draft",
            response_model=GenerationDraftDetail)
def get_draft(
    draft_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        draft = draft_module.get_draft(session, context, draft_id)
        payload = _finalize_draft(draft)
    set_etag(response, payload["revision"])
    return payload


@router.get("/previews/{preview_id}", operation_id="get_generation_preview",
            response_model=GenerationResolutionPreview)
def get_preview(
    preview_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        preview = run_queries.get_preview(session, context, preview_id)
        if preview.purpose == "run_recovery":
            return _finalize_recovery_preview(preview)
        return _finalize_preview(preview)


@router.get("/runs", operation_id="list_generation_runs", response_model=Page)
def list_runs(
    product_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    execution_mode: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        items = run_queries.list_runs(
            session, context, product_id=product_id, status=status, execution_mode=execution_mode,
        )
    filters_key = f"runs:{product_id or ''}:{status or ''}:{execution_mode or ''}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@router.get("/runs/{run_id}", operation_id="get_generation_run", response_model=GenerationRunDetail)
def get_run(
    run_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        detail = run_queries.get_run_detail(session, context, run_id)
    set_etag(response, detail["revision"])
    return detail


@router.get("/runs/{run_id}/items", operation_id="list_generation_run_items", response_model=Page)
async def list_run_items(
    run_id: str,
    state: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    """One page of a Run's items, each with its bound AssetVersion's signed
    thumbnail link when it has one (Issue #159). The synchronous projection
    runs off the event loop; only the page actually returned gets its
    thumbnails signed, after the session has closed."""

    def _load() -> tuple[dict, dict]:
        with tenant_session(context) as session:
            items = run_queries.list_run_items(session, context, run_id, state=state)
            filters_key = f"run_items:{run_id}:{state or ''}"
            page = _paginate(
                items, context=context, filters_key=filters_key, cursor=cursor, limit=limit,
            )
            refs = thumbnail_refs_for_versions(
                session, context, (item["bound_asset_version_id"] for item in page["items"]),
            )
        return page, refs

    page, refs = await run_in_threadpool(_load)
    await attach_thumbnail_links(
        _blob_store, page["items"], refs, version_id_key="bound_asset_version_id",
    )
    return page


@router.get(
    "/runs/{run_id}/items/{run_item_id}",
    operation_id="get_generation_run_item",
    response_model=GenerationRunItemDetail,
)
def get_run_item(
    run_id: str,
    run_item_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return run_queries.get_run_item_detail(session, context, run_id, run_item_id)


@router.get("/assets", operation_id="list_generation_assets", response_model=AssetGalleryPage)
async def list_assets(
    product_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    origin_kind: str | None = Query(default=None),
    execution_mode: str | None = Query(default=None),
    review_value: str | None = Query(default=None),
    aspect_ratio: str | None = Query(default=None),
    qc_state: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    experiment_context: str | None = Query(default=None),
    conclusion: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    def _load() -> tuple[dict, dict]:
        with tenant_session(context) as session:
            page = list_asset_versions(
                session, context,
                product_id=product_id, run_id=run_id, provider=provider,
                origin_kind=origin_kind, execution_mode=execution_mode,
                review_value=review_value, aspect_ratio=aspect_ratio,
                qc_state=qc_state, tag=tag, experiment_context=experiment_context,
                conclusion=conclusion, cursor=cursor, limit=limit,
            )
            refs = thumbnail_refs_for_versions(
                session, context, (item["id"] for item in page["items"]),
            )
        return page, refs

    try:
        page, refs = await run_in_threadpool(_load)
    except InvalidCursorError as exc:
        # Same conversion every other Generation v1 list route's _paginate
        # applies to its offset cursor -- the gallery's keyset cursor is an
        # opaque app.platform.pagination token too.
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cursor", "message": str(exc)},
        ) from exc
    # Thumbnail links (Issue #159) are signed after the session has closed:
    # the gallery item projection is pure, the BlobStore belongs to the
    # process, and a tile without a derivative keeps ``thumbnail: null``.
    await attach_thumbnail_links(_blob_store, page["items"], refs, version_id_key="id")
    return page


@router.get(
    "/asset-versions/{version_id}",
    operation_id="get_generation_asset_version",
    response_model=AssetVersionDetail,
)
def get_asset_version(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return get_asset_version_detail(session, context, version_id)


@router.get(
    "/exports/{export_package_id}",
    operation_id="get_generation_export_package",
    response_model=ExportPackageDetail,
)
def get_export_package(
    export_package_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """Current ExportPackage state (Issue #49 Task 9): the browser's
    ``useExportFlow`` polls this alongside the Operation to recover state
    across a refresh, independent of the 202 response's Operation-state
    ``state``."""
    with tenant_session(context) as session:
        package = export_module.get_export_package(session, context, export_package_id)
        return {
            "package_id": package.id,
            "operation_id": package.operation_id,
            "execution_mode": package.execution_mode,
            "state": package.state,
            "revision": package.revision,
            "byte_size": package.byte_size or None,
            "zip_hash": package.zip_hash or None,
        }


@router.get(
    "/exports/{export_package_id}/manifest",
    operation_id="get_generation_export_manifest",
    response_model=ExportManifest,
)
def get_export_manifest(
    export_package_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return export_module.build_manifest(session, context, export_package_id)


# --------------------------------------------------------------------------
# Commands -- every route below carries x-marketai-idempotent: true and
# requires Idempotency-Key (422 if missing/blank).
# --------------------------------------------------------------------------


class CreateDraftInput(BaseModel):
    product_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    combination_strategy: dict = Field(default_factory=dict)
    format_config: dict = Field(default_factory=dict)
    exclusions: dict = Field(default_factory=dict)
    selectors: dict = Field(default_factory=dict)


@router.post(
    "/drafts",
    operation_id="create_generation_draft",
    response_model=GenerationDraftDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_draft(
    body: CreateDraftInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    with tenant_session(context) as session:
        def create(s):
            product_input(s, context, body.product_id, ensure_anchor=True)
            return draft_module.create_draft(s, context, **body.model_dump())
        draft = _resource_command(
            session, context, request, idempotency_key, "generation.create_draft", body.model_dump(),
            create, lambda s, rid: draft_module.get_draft(s, context, rid),
        )
        return _finalize_draft(draft)


class UpdateDraftInput(BaseModel):
    product_id: str | None = Field(default=None, min_length=1)
    provider: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    execution_mode: str | None = Field(default=None, min_length=1)
    selectors: dict | None = None
    combination_strategy: dict | None = None
    format_config: dict | None = None
    exclusions: dict | None = None


@router.patch(
    "/drafts/{draft_id}",
    operation_id="update_generation_draft",
    response_model=GenerationDraftDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def update_draft(
    draft_id: str,
    body: UpdateDraftInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    fields = body.model_dump(exclude_none=True)
    with tenant_session(context) as session:
        def create(s):
            if "product_id" in fields:
                product_input(s, context, fields["product_id"], ensure_anchor=True)
            return draft_module.update_draft(s, context, draft_id, expected_revision=expected_revision, **fields)
        draft = _resource_command(
            session, context, request, idempotency_key, "generation.update_draft", fields,
            create, lambda s, rid: draft_module.get_draft(s, context, rid),
        )
        return _finalize_draft(draft)


@router.post(
    "/drafts/{draft_id}/previews",
    operation_id="create_generation_preview",
    response_model=GenerationPreviewDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_preview(
    draft_id: str,
    request: Request,
    experiment_context: str | None = Query(default=None),
    skip_losers: bool | None = Query(default=None),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        preview = _resource_command(session, context, request, idempotency_key, "generation.create_preview", {"experiment_context": experiment_context, "skip_losers": skip_losers},
            lambda s: preview_module.create_preview(s, context, draft_id, expected_revision, experiment_context=experiment_context, skip_losers=skip_losers),
            lambda s, rid: run_queries.get_preview(s, context, rid))
        return _finalize_preview(preview)


class SubmitRunInput(BaseModel):
    preview_id: str = Field(min_length=1)
    preview_hash: str = Field(min_length=1)


@router.post(
    "/runs",
    operation_id="submit_generation_run",
    response_model=OperationRef,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def submit_run(
    body: SubmitRunInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_draft_revision = extract_if_match(request)
    with tenant_session(context) as session:
        result = submission_module.submit_generation(
            session, context,
            preview_id=body.preview_id, preview_hash=body.preview_hash,
            expected_draft_revision=expected_draft_revision, idempotency_key=idempotency_key,
        )
        return result


@router.post(
    "/runs/{run_id}/recovery-previews",
    operation_id="create_generation_run_recovery_preview",
    response_model=GenerationRecoveryPreview,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_recovery_preview(
    run_id: str,
    body: RecoveryPreviewInput,
    request: Request,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        result = recovery_preview_module.create_recovery_preview(
            session, context, run_id,
            command=body.command,
            selection_kind=body.selection.kind,
            selection_item_ids=body.selection.item_ids,
            expected_run_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        response.headers["Location"] = f"/api/v1/generation/previews/{result['id']}"
        return result


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancel_generation_run",
    response_model=RunCommandResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def cancel_run(
    run_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        return run_commands.cancel_run(
            session, context, run_id=run_id, expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )


class RetryFailedItemsInput(BaseModel):
    preview_id: str = Field(min_length=1)
    preview_hash: str = Field(min_length=1)


@router.post(
    "/runs/{run_id}/retry-failed-items",
    operation_id="retry_generation_run_failed_items",
    response_model=RunCommandResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def retry_failed_items(
    run_id: str,
    body: RetryFailedItemsInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        return run_commands.retry_failed_items(
            session, context, run_id=run_id, expected_revision=expected_revision,
            preview_id=body.preview_id, preview_hash=body.preview_hash,
            idempotency_key=idempotency_key,
        )


class ResumePausedItemsInput(BaseModel):
    preview_id: str = Field(min_length=1)
    preview_hash: str = Field(min_length=1)


@router.post(
    "/runs/{run_id}/resume-paused-items",
    operation_id="resume_generation_run_paused_items",
    response_model=RunCommandResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def resume_paused_items(
    run_id: str,
    body: ResumePausedItemsInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        return run_commands.resume_paused_items(
            session, context, run_id=run_id, expected_revision=expected_revision,
            preview_id=body.preview_id, preview_hash=body.preview_hash,
            idempotency_key=idempotency_key,
        )


class ReconcileRunInput(BaseModel):
    uncertain_targets: list[str] = Field(default_factory=list)
    operator_note: str = ""


@router.post(
    "/runs/{run_id}/reconcile",
    operation_id="reconcile_generation_run",
    response_model=RunCommandResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def reconcile_run(
    run_id: str,
    body: ReconcileRunInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    expected_revision = extract_if_match(request)
    with tenant_session(context) as session:
        return run_commands.reconcile_run(
            session, context, run_id=run_id, expected_revision=expected_revision,
            uncertain_targets=body.uncertain_targets, operator_note=body.operator_note,
            idempotency_key=idempotency_key,
        )


class CreateReviewInput(BaseModel):
    review_value: str = Field(min_length=1)
    reason: str = ""
    supersedes_review_id: str | None = None


def _review_response(review: QualityReviewRecord) -> dict:
    return {
        "id": review.id,
        "asset_version_id": review.asset_version_id,
        "review_value": review.review_value,
        "reason": review.reason,
        "actor_id": review.actor_id,
        "created_at": _iso(review.created_at),
        "source": review.source or "human",
    }


@router.post(
    "/asset-versions/{version_id}/reviews",
    operation_id="create_generation_asset_review",
    response_model=AssetReview,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_review(
    version_id: str,
    body: CreateReviewInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Idempotent review creation (Issue #49 Task 5, spec §0.3/§5.4).

    Fingerprinted on ``(version_id, review_value, reason,
    supersedes_review_id)`` via ``compute_fingerprint`` -- replaying the same
    ``Idempotency-Key`` against the same request returns the original
    review; the same key against a different request raises
    ``IdempotencyKeyReusedError`` (409, handled app-wide). A head guard in
    ``app.generation.review.create_review`` rejects (409
    ``effective_review_exists``) a review that omits
    ``supersedes_review_id`` while an effective review head already exists.
    """
    actor_id = context.initiator_id or context.executor
    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_generation_asset_review",
        request.url.path,
        {},
        {
            "version_id": version_id,
            "review_value": body.review_value,
            "reason": body.reason,
            "supersedes_review_id": body.supersedes_review_id,
        },
        {},
    )

    def _create(session):
        review = review_module.create_review(
            session, context, version_id, body.review_value, body.reason, actor_id,
            supersedes_review_id=body.supersedes_review_id,
        )
        return "quality_review", review.id, _review_response(review)

    def _replay(_resource_type, resource_id, session):
        review = session.get(QualityReviewRecord, resource_id)
        return _review_response(review)

    with tenant_session(context) as session:
        return create_or_replay_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="generation.create_asset_review",
            fingerprint_hash=fingerprint_hash,
            create_fn=_create,
            replay_fn=_replay,
        )


class CreateRegenerationDraftInput(BaseModel):
    intent: str = Field(min_length=1)
    prompt_overrides: dict | None = None
    qc_attempt_id: str | None = None


@router.post(
    "/asset-versions/{version_id}/regeneration-drafts",
    operation_id="create_generation_regeneration_draft",
    response_model=GenerationDraftDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_regeneration_draft(
    version_id: str,
    body: CreateRegenerationDraftInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Idempotent regeneration-draft creation (Issue #49 Task 7, spec §5.4).

    Fingerprinted on ``(version_id, intent, prompt_overrides,
    qc_attempt_id)`` via ``compute_fingerprint`` -- replaying the same
    ``Idempotency-Key`` against the same request returns the original
    Draft (``generation_draft`` resource, spec §5.4 sync resource path); the
    same key against a different request raises ``IdempotencyKeyReusedError``
    (409, handled app-wide, matching ``create_review``'s pattern above).
    """
    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_generation_regeneration_draft",
        request.url.path,
        {},
        {
            "version_id": version_id,
            "intent": body.intent,
            "prompt_overrides": body.prompt_overrides,
            "qc_attempt_id": body.qc_attempt_id,
        },
        {},
    )

    def _create(session):
        if body.intent == "quality_feedback":
            if not body.qc_attempt_id:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "qc_attempt_id_required",
                        "message": "qc_attempt_id is required when intent is quality_feedback",
                    },
                )
            qc = session.get(AssetQCAttemptRecord, body.qc_attempt_id)
            if (
                qc is None
                or qc.workspace_id != context.workspace_id
                or qc.asset_version_id != version_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "qc_attempt_mismatch",
                        "message": (
                            f"QC Attempt {body.qc_attempt_id!r} does not belong to "
                            f"AssetVersion {version_id!r} in this Workspace"
                        ),
                    },
                )

        draft = regeneration_module.create_regeneration_draft(
            session, context, version_id, body.intent,
            prompt_overrides=body.prompt_overrides, qc_attempt_id=body.qc_attempt_id,
        )
        return "generation_draft", draft.id, _finalize_draft(draft)

    def _replay(_resource_type, resource_id, session):
        draft = draft_module.get_draft(session, context, resource_id)
        return _finalize_draft(draft)

    with tenant_session(context) as session:
        return create_or_replay_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="generation.create_regeneration_draft",
            fingerprint_hash=fingerprint_hash,
            create_fn=_create,
            replay_fn=_replay,
        )


class RegenerationDraftBatchInput(BaseModel):
    action: str = Field(min_length=1)
    candidate_version_ids: list[str] = Field(min_length=1)


@router.post(
    "/regeneration-draft-batches",
    operation_id="create_generation_review_summary_regeneration",
    response_model=RegenerationDraftBatchResult,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_review_summary_regeneration(
    body: RegenerationDraftBatchInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Atomic review-summary regeneration batch (Issue #49 Task 7, spec
    §0.3/§0.4). Groups ``candidate_version_ids`` by stable-dimension-
    signature and creates one regeneration Draft per group -- 2 variants for
    ``action == "regenerate_needs_changes"``, 3 for
    ``"supplement_approved"`` (``app.generation.regeneration.
    create_review_summary_regeneration``). Browser Task 10's
    ``ReviewSummary`` is the consumer.

    Idempotent the same way ``create_regeneration_draft`` above is, except
    the resource this binds is the whole batch: ``resource_id`` is the
    *first* created Draft's id (in the deterministic, by-signature order
    the domain call returns them) -- ``response_resource_id`` is a bounded
    ``VARCHAR(100)`` and joining every created Draft id would overflow it
    past two signature groups (Issue #49 Task 7 review, Finding 2), so the
    full ordered id list instead rides along on each Draft's own
    ``combination_strategy_json`` (``regeneration_module.
    create_review_summary_regeneration`` writes it as ``batch_draft_ids``).
    A replay looks up that first Draft, reads the sibling ids off it, and
    reconstructs each batch item fresh from its persisted Draft
    (``regeneration_module.batch_item_from_draft``) rather than re-running
    the grouping -- so a replay is stable even if intervening reviews would
    change how a fresh call groups the same candidates.
    """
    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_generation_review_summary_regeneration",
        request.url.path,
        {},
        {
            "action": body.action,
            "candidate_version_ids": body.candidate_version_ids,
        },
        {},
    )

    def _create(session):
        drafts = regeneration_module.create_review_summary_regeneration(
            session, context,
            action=body.action, candidate_version_ids=body.candidate_version_ids,
        )
        # First Draft's id only -- see the docstring above for why the full
        # id list doesn't fit in response_resource_id.
        resource_id = drafts[0]["generation_draft_id"]
        return "regeneration_draft_batch", resource_id, {"drafts": drafts}

    def _replay(_resource_type, resource_id, session):
        anchor = draft_module.get_draft(session, context, resource_id)
        strategy = anchor.combination_strategy_json or {}
        draft_ids = strategy.get("batch_draft_ids") or [resource_id]
        drafts = []
        for draft_id in draft_ids:
            draft = draft_module.get_draft(session, context, draft_id)
            drafts.append(regeneration_module.batch_item_from_draft(session, context, draft))
        return {"drafts": drafts}

    with tenant_session(context) as session:
        return create_or_replay_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="generation.review_summary_regeneration",
            fingerprint_hash=fingerprint_hash,
            create_fn=_create,
            replay_fn=_replay,
        )


@router.post(
    "/asset-versions/{version_id}/download-intent",
    operation_id="create_generation_asset_version_download_intent",
    response_model=DownloadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_asset_version_download_intent(
    version_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    # The committed ref is always resolved server-side, never supplied by
    # the browser (spec §0.1) -- and resolved fresh on every call, replays
    # included, so a replay still fails closed if the AssetVersion's blob
    # somehow became unavailable between requests.
    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_generation_asset_version_download_intent",
        request.url.path,
        {},
        None,
        {},
    )

    with tenant_session(context) as session:
        committed_ref = resolve_asset_blob_ref(session, context, version_id)
        intent = create_or_replay_download_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="generation.asset_version_download_intent",
            fingerprint_hash=fingerprint_hash,
            owner_kind="asset_version",
            owner_id=version_id,
            committed_ref=committed_ref,
        )
        expires_at = intent.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

    remaining_seconds = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    filename = f"asset-{version_id}.{extension_for_mime(committed_ref.content_type_verified)}"
    grant = await _blob_store.issue_download(
        committed_ref,
        DownloadSpec(
            filename=filename,
            expires_in_seconds=min(DEFAULT_DOWNLOAD_TTL_SECONDS, remaining_seconds),
        ),
    )

    return {
        "intent_id": intent.id,
        "expires_at": _iso(expires_at),
        "download_grant": {
            "url": grant.url,
            "kind": "signed_get",
            "headers": {},
            "content_disposition": grant.content_disposition,
        },
    }


class ExportSelectorInput(BaseModel):
    """A discriminated ``ExportSelector`` (Issue #49 Task 9, spec §0.4).

    Not a strict pydantic discriminated union -- ``kind`` picks which of the
    other (all-optional) fields ``app.generation.export._resolve_selector``
    actually reads, and that function is the single place that enforces
    each kind's own preconditions (e.g. ``winner_only`` requiring
    ``experiment_context_id``) as a domain ``ExportError`` rather than a
    422 schema-validation error, so the response ``code`` stays a stable
    domain vocabulary instead of a pydantic error shape.
    """

    kind: str = Field(min_length=1)
    version_ids: list[str] | None = None
    scope: dict | None = None
    experiment_context_id: str | None = None


class CreateExportInput(BaseModel):
    selector: ExportSelectorInput


@router.post(
    "/exports",
    operation_id="create_generation_export_package",
    response_model=ExportPackageQueued,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_export(
    body: CreateExportInput,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Freeze a selection into an ExportPackage and enqueue the Worker
    Job that builds its ZIP (Issue #49 Task 9). Returns 202 with the
    package/Operation ids the browser's ``useExportFlow`` polls -- the
    ``Location`` header points at the Operation, matching the async-command
    convention every other 202 route in this router follows."""
    with tenant_session(context) as session:
        result = export_module.create_export_package(
            session, context,
            selector=body.selector.model_dump(exclude_none=True),
            idempotency_key=idempotency_key,
        )
    response.headers["Location"] = f"/api/v1/operations/{result['operation_id']}"
    return result


@router.post(
    "/exports/{export_package_id}/download-intent",
    operation_id="create_generation_export_download_intent",
    response_model=DownloadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_export_download_intent(
    export_package_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Same pattern as the AssetVersion download-intent route above
    (Issue #49 Task 2): the committed ZIP ref is resolved server-side
    (``export_module.resolve_export_blob_ref``), never supplied by the
    browser, and re-resolved fresh on every call -- replays included -- so
    a replay still fails closed if the package's blob somehow became
    unavailable between requests."""
    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_generation_export_download_intent",
        request.url.path,
        {},
        None,
        {},
    )

    with tenant_session(context) as session:
        committed_ref = export_module.resolve_export_blob_ref(session, context, export_package_id)
        intent = create_or_replay_download_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="generation.export_download_intent",
            fingerprint_hash=fingerprint_hash,
            owner_kind="export_package",
            owner_id=export_package_id,
            committed_ref=committed_ref,
        )
        expires_at = intent.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

    remaining_seconds = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    filename = f"export-{export_package_id}.zip"
    grant = await _blob_store.issue_download(
        committed_ref,
        DownloadSpec(
            filename=filename,
            expires_in_seconds=min(DEFAULT_DOWNLOAD_TTL_SECONDS, remaining_seconds),
        ),
    )

    return {
        "intent_id": intent.id,
        "expires_at": _iso(expires_at),
        "download_grant": {
            "url": grant.url,
            "kind": "signed_get",
            "headers": {},
            "content_disposition": grant.content_disposition,
        },
    }


# --------------------------------------------------------------------------
# Problem handlers for the error types this task introduces. Errors raised
# by the pre-existing draft/preview/submission/export modules are already
# handled app-wide by app.generation.api.register_problem_handlers.
# --------------------------------------------------------------------------


def _problem(
    request: Request, status: int, code: str, message: str, *, params: dict | None = None,
) -> JSONResponse:
    context = getattr(request.state, "tenant_context", None)
    trace_id = context.trace_id if isinstance(context, TenantContext) else new_request_id()
    route = request.scope.get("route")
    instance = str(getattr(route, "path", "") or request.url.path)
    response = JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"urn:marketai:error:{code.replace('_', '-')}",
            "title": code,
            "status": status,
            "detail": message,
            "instance": instance,
            "code": code,
            "trace_id": trace_id,
            "retryable": False,
            **({"params": params} if params else {}),
        },
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


_RUN_COMMAND_STATUS = {
    "revision_conflict": 409,
    "run_not_cancellable": 409,
}

_REVIEW_STATUS = {
    "blocking_qc_failure": 409,
    "asset_version_not_found": 404,
    "asset_not_found": 404,
}

_REGENERATION_STATUS = {
    "source_not_found": 404,
    "asset_not_found": 404,
    "invalid_intent": 422,
    "qc_attempt_required": 409,
}

_RECOVERY_PREVIEW_STATUS = {
    "stale_run_revision": 409,
    "preview_hash_mismatch": 409,
    "preview_expired": 410,
    "preview_already_submitted": 409,
    "empty_preview": 422,
    "invalid_target_state": 422,
    "not_resumable": 422,
    "target_not_found": 404,
    "invalid_command": 422,
    "invalid_selection_kind": 422,
    "target_state_changed": 409,
    "wrong_purpose": 422,
    "wrong_run": 422,
    "wrong_command": 422,
    "preview_invalidated": 410,
    "preview_not_found": 404,
    "revision_conflict": 409,
    # BYOK spend admission (app.generation.spend) on retry / resume.
    "guardrail_exceeded": 422,
    "entitlement_unavailable": 422,
    "unpriced_call": 422,
}


def register_v1_problem_handlers(app) -> None:
    """Register RFC 9457 handlers for the error types Task 3 introduces."""

    @app.exception_handler(GenerationResourceNotFoundError)
    async def _resource_not_found_handler(request: Request, exc: GenerationResourceNotFoundError):
        return _problem(request, 404, exc.code, str(exc))

    @app.exception_handler(RunCommandError)
    async def _run_command_error_handler(request: Request, exc: RunCommandError):
        return _problem(
            request, _RUN_COMMAND_STATUS.get(exc.code, 422), exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )

    @app.exception_handler(ReviewError)
    async def _review_error_handler(request: Request, exc: ReviewError):
        return _problem(
            request, _REVIEW_STATUS.get(exc.code, 422), exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )

    @app.exception_handler(ReviewConflictError)
    async def _review_conflict_error_handler(request: Request, exc: ReviewConflictError):
        return _problem(
            request, 409, exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )

    @app.exception_handler(RegenerationError)
    async def _regeneration_error_handler(request: Request, exc: RegenerationError):
        return _problem(
            request, _REGENERATION_STATUS.get(exc.code, 422), exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )

    @app.exception_handler(RecoveryPreviewError)
    async def _recovery_preview_error_handler(request: Request, exc: RecoveryPreviewError):
        return _problem(
            request, _RECOVERY_PREVIEW_STATUS.get(exc.code, 400), exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )

    @app.exception_handler(AssetBlobError)
    async def _asset_blob_error_handler(request: Request, exc: AssetBlobError):
        return _problem(
            request, 422, exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )

    @app.exception_handler(GalleryQueryError)
    async def _gallery_query_error_handler(request: Request, exc: GalleryQueryError):
        return _problem(
            request, 422, exc.code, exc.detail,
            params=getattr(exc, "params", None),
        )
