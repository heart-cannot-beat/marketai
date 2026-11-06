"""Generation → Assets command surface (Issue #21 Task 2).

Identity comes from the verified BFF claim via ``request_tenant_context``,
matching ``app.library.api``: no endpoint accepts a browser-provided
Workspace. Errors are reported as RFC 9457 Problem Details via the same
``_problem``/exception-handler split ``app.library.api`` uses, so both
domains produce one consistent error shape to the BFF.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.adapters.persistence.database import tenant_session
from app.platform.download_intents import create_download_intent_record, get_download_intent
from app.platform.http_identity import request_tenant_context
from app.platform.idempotency import (
    IdempotencyKeyReusedError,
    compute_fingerprint,
    create_or_replay_intent,
)
from app.platform.tenancy import TenantContext, new_request_id

from .draft import DraftNotFoundError, StaleRevisionError, create_draft
from .export import (
    ExportError,
    build_manifest,
    create_export_package,
    resolve_export_blob_ref,
)
from .preview import PreviewError, create_preview
from .selection import SelectionError
from .submission import SubmissionError, submit_generation

router = APIRouter(prefix="/api/generation", tags=["generation"])
_LOG = logging.getLogger(__name__)


def _problem(status: int, code: str, message: str, *, retryable: bool = False,
             trace_id: str = "", instance: str = "", params: dict | None = None,
             **extra) -> JSONResponse:
    body: dict = {
        "type": f"urn:marketai:error:{code.replace('_', '-')}",
        "title": code,
        "status": status,
        "detail": message,
        "instance": instance,
        "code": code,
        "trace_id": trace_id,
        "retryable": retryable,
    }
    if params:
        body["params"] = params
    body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


def _request_problem(request: Request, status: int, code: str, message: str, *,
                      retryable: bool = False, **extra) -> JSONResponse:
    context = getattr(request.state, "tenant_context", None)
    trace_id = context.trace_id if isinstance(context, TenantContext) else ""
    if not trace_id:
        trace_id = new_request_id()
    route = request.scope.get("route")
    instance = str(getattr(route, "path", "") or request.url.path)
    response = _problem(status, code, message, retryable=retryable, trace_id=trace_id,
                         instance=instance, **extra)
    response.headers["X-Trace-Id"] = trace_id
    return response


#: PreviewError/SubmissionError codes that mean "the caller's understanding
#: of state is stale" (409) or "the referenced resource is gone/consumed"
#: (404/410), rather than a plain admission failure (422).
_CONFLICT_CODES = {"stale_draft_revision"}
_NOT_FOUND_CODES = {"preview_not_found"}
_GONE_CODES = {"preview_expired", "preview_already_submitted", "preview_invalidated"}


def _status_for(code: str) -> int:
    if code in _NOT_FOUND_CODES:
        return 404
    if code in _GONE_CODES:
        return 409
    if code in _CONFLICT_CODES:
        return 409
    return 422


#: ExportError codes (app.generation.export) that mean "the referenced
#: resource is not in this Workspace" (404) or "the caller's request
#: conflicts with frozen/current state" (409), rather than a plain
#: admission failure (422).
_EXPORT_NOT_FOUND_CODES = {"export_package_not_found", "asset_version_not_found"}
#: "export_package_not_ready" is a conflict with frozen/current state (409):
#: the package exists but the Worker hasn't committed a ZIP yet.
#: "mixed_execution_modes" (like "empty_selection"/"experiment_context_required")
#: is a plain admission failure on the request itself (422, Issue #49 Task 9),
#: not a conflict with server-side state.
_EXPORT_CONFLICT_CODES = {"export_package_not_ready"}


def _status_for_export(code: str) -> int:
    if code in _EXPORT_NOT_FOUND_CODES:
        return 404
    if code in _EXPORT_CONFLICT_CODES:
        return 409
    return 422


class CreateDraftInput(BaseModel):
    product_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    combination_strategy: dict
    format_config: dict = Field(default_factory=dict)
    exclusions: dict = Field(default_factory=dict)
    selectors: dict = Field(default_factory=dict)
    regeneration_intent: str = "new_generation"
    source_asset_version_id: str | None = None
    source_recipe_identity: str | None = None


class CreatePreviewInput(BaseModel):
    expected_revision: int = Field(ge=1)


class SubmitInput(BaseModel):
    preview_id: str = Field(min_length=1)
    preview_hash: str = Field(min_length=1)
    expected_draft_revision: int = Field(ge=1)


class CreateExportInput(BaseModel):
    selector: dict


@router.post("/drafts", status_code=201)
def create_draft_route(
    body: CreateDraftInput,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        draft = create_draft(
            session, context,
            product_id=body.product_id,
            provider=body.provider,
            model=body.model,
            execution_mode=body.execution_mode,
            combination_strategy=body.combination_strategy,
            format_config=body.format_config,
            exclusions=body.exclusions,
            selectors=body.selectors,
            regeneration_intent=body.regeneration_intent,
            source_asset_version_id=body.source_asset_version_id,
            source_recipe_identity=body.source_recipe_identity,
        )
        return {"id": draft.id, "revision": draft.revision, "state": draft.state}


@router.post("/drafts/{draft_id}/preview")
def create_preview_route(
    draft_id: str,
    body: CreatePreviewInput,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        preview = create_preview(session, context, draft_id, body.expected_revision)
        return {
            "id": preview.id,
            "preview_hash": preview.preview_hash,
            "recipe_input_hash": preview.recipe_input_hash,
            "execution_mode": preview.execution_mode,
            "valid_until": preview.valid_until.isoformat(),
        }


@router.post("/submit", status_code=202)
def submit_route(
    body: SubmitInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    with tenant_session(context) as session:
        result = submit_generation(
            session, context,
            preview_id=body.preview_id,
            preview_hash=body.preview_hash,
            expected_draft_revision=body.expected_draft_revision,
            idempotency_key=idempotency_key,
        )
        return result


@router.post("/exports", status_code=202)
def create_export_route(
    body: CreateExportInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    # NOTE: this legacy /api/generation router is unmounted (app.main only
    # imports register_problem_handlers from this module -- see
    # app.generation.v1_router's module docstring); kept import-safe here so
    # app.main's module-level import doesn't break, not exercised by tests.
    with tenant_session(context) as session:
        result = create_export_package(
            session, context,
            selector=body.selector,
            idempotency_key=idempotency_key,
        )
        return {
            "export_package_id": result["package_id"],
            "operation_id": result["operation_id"],
            "state": result["state"],
        }


@router.get("/exports/{export_package_id}/manifest")
def get_export_manifest_route(
    export_package_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return build_manifest(session, context, export_package_id)


@router.post("/exports/{export_package_id}/download-intent", status_code=201)
def create_export_download_intent_route(
    export_package_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key:
        return _request_problem(
            request, 400, "idempotency_key_required", "Idempotency-Key header is required",
        )

    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_export_download_intent",
        request.url.path,
        {},
        None,
        {},
    )

    def _create(session):
        from app.adapters.persistence.models import new_id

        committed_ref = resolve_export_blob_ref(session, context, export_package_id)
        intent = create_download_intent_record(
            session,
            intent_id=new_id("di"),
            owner_kind="export_package",
            owner_id=export_package_id,
            committed_blob_ref_key=committed_ref.committed_key,
            committed_blob_ref_version=committed_ref.storage_version_id,
        )
        return ("download_intent", intent.id, intent)

    def _replay(_resource_type, resource_id, session):
        return get_download_intent(session, resource_id)

    with tenant_session(context) as session:
        intent = create_or_replay_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="generation.export_download_intent",
            fingerprint_hash=fingerprint_hash,
            create_fn=_create,
            replay_fn=_replay,
        )
        return {
            "intent_id": intent.id,
            "expires_at": intent.expires_at.isoformat(),
        }


def register_problem_handlers(app: FastAPI) -> None:
    """Register the RFC 9457 exception handlers for this module's errors."""

    @app.exception_handler(DraftNotFoundError)
    async def _draft_not_found_handler(request: Request, exc: DraftNotFoundError):
        return _request_problem(request, 404, "draft_not_found", str(exc))

    @app.exception_handler(StaleRevisionError)
    async def _stale_revision_handler(request: Request, exc: StaleRevisionError):
        return _request_problem(request, 409, "stale_revision", str(exc))

    @app.exception_handler(PreviewError)
    async def _preview_error_handler(request: Request, exc: PreviewError):
        return _request_problem(request, _status_for(exc.code), exc.code, exc.detail, params=exc.params)

    @app.exception_handler(SelectionError)
    async def _selection_error_handler(request: Request, exc: SelectionError):
        status = 403 if exc.code == "generation_not_entitled" else 422
        return _request_problem(request, status, exc.code, exc.detail, params=exc.params)

    @app.exception_handler(SubmissionError)
    async def _submission_error_handler(request: Request, exc: SubmissionError):
        return _request_problem(request, _status_for(exc.code), exc.code, exc.detail, params=exc.params)

    @app.exception_handler(ExportError)
    async def _export_error_handler(request: Request, exc: ExportError):
        return _request_problem(request, _status_for_export(exc.code), exc.code, exc.detail, params=exc.params)

    @app.exception_handler(IdempotencyKeyReusedError)
    async def _idempotency_key_reused_handler(request: Request, exc: IdempotencyKeyReusedError):
        return _request_problem(
            request, 409, "idempotency_key_reused",
            "Idempotency-Key was already used for a different request",
        )
