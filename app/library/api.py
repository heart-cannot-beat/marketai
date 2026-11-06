"""Creative library command interface (contract S17).

Identity comes from the verified BFF claim via ``request_tenant_context``.
All endpoints require a valid ``TenantContext``; unauthenticated requests
are rejected with 401.
"""
from __future__ import annotations

import hashlib
import logging
from typing import NamedTuple

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext, new_request_id

from . import references, service
from .contracts import LibraryContractError, iter_entry_types

router = APIRouter(prefix="/api/library", tags=["library"])
_LOG = logging.getLogger(__name__)


def _problem(status: int, code: str, message: str, *, retryable: bool = False,
             trace_id: str = "", instance: str = "", params: dict | None = None,
             **extra) -> JSONResponse:
    """``code`` (and ``params``) are what the interface renders; ``detail`` is
    a diagnostic for logs and audit only (#253)."""
    body: dict = {
        "type": f"urn:marketai:error:{code.replace('_', '-')}",
        "title": code,
        "status": status,
        "detail": message,
        "instance": instance,
        "code": code,
        "trace_id": trace_id,
        "retryable": retryable,
        "field_errors": extra.pop("field_errors", []),
    }
    if params:
        body["params"] = params
    body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


def _request_problem(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    **extra,
) -> JSONResponse:
    context = getattr(request.state, "tenant_context", None)
    trace_id = context.trace_id if isinstance(context, TenantContext) else ""
    if not trace_id:
        trace_id = (request.headers.get("X-Trace-Id") or "").strip()[:128]
    if trace_id and not all(
        character.isascii() and (character.isalnum() or character in "._:-")
        for character in trace_id
    ):
        trace_id = ""
    if not trace_id:
        trace_id = new_request_id()
    route = request.scope.get("route")
    instance = str(getattr(route, "path", "") or request.url.path)
    response = _problem(
        status,
        code,
        message,
        retryable=retryable,
        trace_id=trace_id,
        instance=instance,
        **extra,
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


def _library_problem(request: Request, exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", None) or "library_invalid"
    params = getattr(exc, "params", None) or None
    if isinstance(exc, service.UploadTooLarge):
        return _request_problem(
            request, 413, code, str(exc), params=params, limit=exc.limit,
        )
    if isinstance(exc, service.LibraryNotFound):
        return _request_problem(request, 404, code, str(exc), params=params)
    if isinstance(exc, service.LibraryForbidden):
        return _request_problem(request, 403, code, str(exc), params=params)
    if isinstance(exc, service.IdempotencyConflict):
        return _request_problem(
            request, 409, "idempotency_key_reused", str(exc),
            conflict_info=exc.detail,
        )
    if isinstance(exc, service.RevisionConflict):
        return _request_problem(
            request, 409, "revision_conflict", str(exc),
            conflict_info=exc.detail,
        )
    if isinstance(exc, service.LibraryConflict):
        return _request_problem(
            request, 409, code, str(exc), params=params,
            conflict_info=exc.detail,
        )
    if isinstance(exc, service.LibraryValidationError):
        return _request_problem(
            request, 422, code, str(exc), report=exc.report,
        )
    if code == references.FAILURE_CONTENT_SAFETY_UNAVAILABLE:
        # Deployment configuration, not the upload, is what is wrong: a stable
        # code the UI renders as "内容安全服务未配置" instead of a 500.
        return _request_problem(request, 503, code, str(exc))
    if isinstance(exc, references.ReferenceGateError):
        return _request_problem(request, 422, code, str(exc), params=params)
    return _request_problem(request, 400, code, str(exc), params=params)


def _command_fingerprint(body: BaseModel, *, resource_field: str = "", resource_id: str = "") -> str:
    payload = body.model_dump(mode="json")
    if resource_field:
        payload = {resource_field: resource_id, **payload}
    return service.compute_fingerprint(payload)


class CreateEntryInput(BaseModel):
    entry_type: str
    display_name: str = Field(min_length=1, max_length=300)
    content: dict = Field(default_factory=dict)


class SaveDraftInput(BaseModel):
    expected_revision: int = Field(ge=1)
    content: dict


class PublishInput(BaseModel):
    expected_revision: int = Field(ge=1)
    acknowledged_warnings: list[str] = Field(default_factory=list)


class WithdrawInput(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class BindInput(BaseModel):
    expected_revision: int = Field(ge=1)
    reference_version_id: str
    purpose: str = Field(min_length=1, max_length=100)
    viewpoint: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=0, ge=0)
    note: str = ""
    conditions: dict = Field(default_factory=dict)


class ArchiveInput(BaseModel):
    expected_revision: int = Field(ge=1)


class ValidateInput(BaseModel):
    expected_revision: int = Field(ge=1)


class ForkInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)


class CloneProductInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)
    include_optional: list[str] = Field(default_factory=list)


@router.get("/entry-types")
def entry_types(
    _context: TenantContext = Depends(request_tenant_context),
):
    return {"entry_types": list(iter_entry_types())}


@router.get("/entries")
def list_entries(
    entry_type: str | None = None,
    include_archived: bool = False,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.list_entries(
        context, entry_type=entry_type, include_archived=include_archived,
    )


@router.post("/entries", status_code=201)
def create_entry(
    body: CreateEntryInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body)
    return service.create_entry(
        context, entry_type=body.entry_type, display_name=body.display_name, content=body.content,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.get("/entries/{entry_id}")
def get_entry(
    entry_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.get_entry(context, entry_id)


@router.get("/entries/{entry_id}/events")
def list_events(
    entry_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.list_events(context, entry_id)


@router.put("/entries/{entry_id}/draft")
def save_draft(
    entry_id: str,
    body: SaveDraftInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="entry_id", resource_id=entry_id)
    return service.save_draft(
        context, entry_id, body.content, body.expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/entries/{entry_id}/validate")
def validate_draft(
    entry_id: str,
    body: ValidateInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="entry_id", resource_id=entry_id)
    return service.validate_draft(
        context, entry_id, expected_revision=body.expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/entries/{entry_id}/publish")
def publish(
    entry_id: str,
    body: PublishInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="entry_id", resource_id=entry_id)
    return service.publish(
        context, entry_id, body.expected_revision, acknowledged_warnings=body.acknowledged_warnings,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/entries/{entry_id}/archive")
def archive(
    entry_id: str,
    body: ArchiveInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="entry_id", resource_id=entry_id)
    return service.archive_entry(
        context, entry_id, expected_revision=body.expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/entries/{entry_id}/bind")
def bind(
    entry_id: str,
    body: BindInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="entry_id", resource_id=entry_id)
    return service.bind_reference(
        context,
        entry_id,
        body.expected_revision,
        reference_version_id=body.reference_version_id,
        purpose=body.purpose,
        viewpoint=body.viewpoint,
        priority=body.priority,
        note=body.note,
        conditions=body.conditions,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.get("/versions/{version_id}")
def get_version(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.get_version(context, version_id)


@router.get("/versions/{version_id}/impact")
def impact(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.impact_analysis(context, version_id)


@router.get("/versions/{version_id}/bindings")
def bindings(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.list_bindings(context, version_id)


@router.post("/versions/{version_id}/withdraw")
def withdraw(
    version_id: str,
    body: WithdrawInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    return service.withdraw_version(
        context, version_id, reason=body.reason,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/versions/{version_id}/fork", status_code=201)
def fork(
    version_id: str,
    body: ForkInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    return service.fork_version(
        context, version_id, display_name=body.display_name,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/versions/{version_id}/clone-product", status_code=201)
def clone_product(
    version_id: str,
    body: CloneProductInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    return service.clone_product(
        context, version_id, display_name=body.display_name, include_optional=body.include_optional,
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


@router.post("/reference-objects", status_code=201)
async def upload_reference_object(
    file: UploadFile = File(...),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    chunks: list[bytes] = []
    total = 0
    digest = hashlib.sha256()
    oversized = False
    while True:
        chunk = await file.read(8192)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        if total <= config.LIBRARY_MAX_REFERENCE_BYTES:
            chunks.append(chunk)
        else:
            # Continue hashing the stream so the idempotency fingerprint binds
            # the complete request, but never retain more than the byte limit.
            oversized = True
            chunks.clear()
    fp = service.compute_fingerprint({
        "content_sha256": digest.hexdigest(),
        "filename": file.filename or "",
        "declared_mime": file.content_type or "",
    })
    if oversized:
        return service.reject_oversized_upload(
            context,
            reason=f"File exceeds {config.LIBRARY_MAX_REFERENCE_BYTES} byte limit",
            idempotency_key=idempotency_key,
            filename=file.filename or "",
            declared_mime=file.content_type or "",
            byte_size=total,
            content_sha256=digest.hexdigest(),
            limit=config.LIBRARY_MAX_REFERENCE_BYTES,
            request_fingerprint=fp,
        )
    raw = b"".join(chunks)
    return service.upload_reference_object(
        context,
        raw=raw,
        declared_mime=file.content_type or "",
        filename=file.filename or "",
        idempotency_key=idempotency_key, request_fingerprint=fp,
    )


class _StatusProblem(NamedTuple):
    status: int
    code: str


#: A bare ``HTTPException(status, "text")`` still answers with a stable code
#: the interface can render (#253); a route that means something more specific
#: raises ``{"code": ..., "message": ...}`` instead. Written as ``code=``
#: literals so ``web/__tests__/locales/error-codes.spec.ts`` finds each one.
_STATUS_CODES = {
    problem.status: problem.code
    for problem in (
        _StatusProblem(400, code="request_invalid"),
        _StatusProblem(401, code="unauthenticated"),
        _StatusProblem(403, code="forbidden"),
        _StatusProblem(404, code="not_found"),
        _StatusProblem(405, code="method_not_allowed"),
        _StatusProblem(409, code="conflict"),
        _StatusProblem(410, code="gone"),
        _StatusProblem(413, code="payload_too_large"),
        _StatusProblem(415, code="unsupported_media_type"),
        _StatusProblem(422, code="request_invalid"),
        _StatusProblem(428, code="precondition_required"),
        _StatusProblem(429, code="rate_limited"),
        _StatusProblem(503, code="service_unavailable"),
    )
}


def _status_code(status: int) -> str:
    return _STATUS_CODES.get(status, "error")


def _field_params(error: dict) -> dict:
    """Pydantic's numeric/text limits (``max_length``, ``ge`` ...) for the
    field's ``error-<code>`` copy; never the rejected input itself."""
    params = {
        key: value for key, value in (error.get("ctx") or {}).items()
        if isinstance(value, (int, float, str)) and not isinstance(value, bool)
        and key not in ("error", "input")
    }
    return {"params": params} if params else {}


def register_problem_handlers(app: FastAPI) -> None:
    """Register global exception handlers that produce Problem Details responses."""

    @app.exception_handler(service.LibraryError)
    async def _library_exception_handler(request: Request, exc: service.LibraryError):
        return _library_problem(request, exc)

    @app.exception_handler(references.ReferenceGateError)
    async def _reference_exception_handler(request: Request, exc: references.ReferenceGateError):
        return _library_problem(request, exc)

    @app.exception_handler(LibraryContractError)
    async def _contract_exception_handler(request: Request, exc: LibraryContractError):
        return _library_problem(request, exc)

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        params = None
        if isinstance(detail, dict):
            code = str(detail.get("code") or _status_code(exc.status_code))
            message = str(detail.get("message") or str(exc))
            retryable = bool(detail.get("retryable", False))
            field_errors = detail.get("field_errors", [])
            if not isinstance(field_errors, list):
                field_errors = []
            if isinstance(detail.get("params"), dict):
                params = detail["params"]
        else:
            code = _status_code(exc.status_code)
            message = str(detail) if detail else "An error occurred"
            retryable = exc.status_code in (408, 425, 429, 502, 503, 504)
            field_errors = []
        return _request_problem(
            request,
            exc.status_code,
            code,
            message,
            retryable=retryable,
            field_errors=field_errors,
            params=params,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        field_errors = [
            {"field": ".".join(str(loc) for loc in e.get("loc", ())),
             "code": e.get("type", "validation_error"),
             "message": e.get("msg", ""),
             **_field_params(e)}
            for e in errors
        ]
        return _request_problem(
            request,
            422,
            "validation_error",
            "Request validation failed",
            field_errors=field_errors,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        response = _request_problem(
            request, 500, "internal_error", "Internal server error",
        )
        _LOG.error(
            "Unhandled request exception type=%s trace_id=%s instance=%s",
            type(exc).__name__,
            response.headers["X-Trace-Id"],
            str(getattr(request.scope.get("route"), "path", "") or request.url.path),
        )
        return response
