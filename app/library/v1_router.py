"""Library v1 sub-router -- 20 operations under ``/api/v1/library`` (Issue #27 Task 2).

Mounted on the shared ``v1_router`` in ``app.main``. Every route is a thin
HTTP adapter over ``app.library.service`` -- no ``store``/``manager`` legacy
imports, no direct database access. Each route carries an explicit
``operation_id`` and ``response_model`` so ``scripts/generate_openapi.py``
can extract a stable BFF-facing contract; every command that has side
effects advertises ``x-marketai-idempotent: true`` via ``openapi_extra`` and
requires an ``Idempotency-Key`` header (422 if missing). Domain errors raised
by ``service`` (``LibraryNotFound``, ``RevisionConflict``, ...) propagate to
the problem handlers already registered by ``app.library.api.
register_problem_handlers`` -- this router registers no handlers of its own.

Revision / If-Match: only Draft-backed aggregates (the entry's open Draft)
carry an optimistic-concurrency ``revision`` -- ``save_draft``, ``validate``,
``publish``, ``archive`` and ``bind`` all read it from ``If-Match`` and pass
it to ``service`` as ``expected_revision``, which itself raises
``RevisionConflict`` (409) on mismatch. Published Versions are immutable
(no revision counter): ``withdraw`` changes a Version's ``state`` but
``service.withdraw_version`` takes no ``expected_revision``, so no
``If-Match`` is required or accepted for it.

``allowed_actions`` on entry/version responses are computed here from
lifecycle/state/role via ``app.platform.action_eligibility.compute_actions``
so the browser never has to reconstruct eligibility rules client-side.

All routes are production-backed; no remaining 501 stubs.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field

from app.platform.action_eligibility import ActionEligibility, compute_actions
from app.platform.blob_store_factory import build_blob_store
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.pagination import InvalidCursorError, decode_offset_cursor, encode_cursor
from app.platform.tenancy import TenantContext

from . import command_impacts, connection_checks, datasources, navigation, service, templates
from .contracts import (
    ENTRY_PERSON,
    ENTRY_PRODUCT,
    LIFECYCLE_ACTIVE,
    VERSION_EFFECTIVE,
    VERSION_SUPERSEDED,
    can_publish,
    iter_entry_types,
)
from .response_models import (
    CommandImpactProjection,
    CredentialBindingSummary,
    DataSourceConnectionCheck,
    InternalEntryContext,
    InternalEntryMeta,
    InternalEntrySummary,
    LibraryCommandOutcome,
    LibraryEntryDetail,
    LibraryEntryTypeList,
    LibraryValidationResult,
    LibraryVersionDetail,
    LibraryVersionImpact,
    OperationAccepted,
    Page,
    ReferenceDownloadGrant,
    ReferenceObjectDetail,
    ReferencePromotionResult,
    TemplateDiffReportResponse,
)
from .response_models import DraftSummary

router = APIRouter(prefix="/library", tags=["library"])

#: The process's BlobStore adapter, built once at import time like
#: ``app.main._blob_store`` / ``app.generation.v1_router._blob_store``: the
#: reference download-intent route signs derivative downloads through it.
_blob_store = build_blob_store()

_OWNER_ROLES = ("owner", "system")
_BINDABLE_ENTRY_TYPES = frozenset({ENTRY_PRODUCT, ENTRY_PERSON})
_PUBLISHED_STATES = frozenset({VERSION_EFFECTIVE, VERSION_SUPERSEDED})


# --------------------------------------------------------------------------
# Idempotency-Key -- required on every command (see module docstring).
# --------------------------------------------------------------------------


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _command_identity(
    command_identity: str | None = Header(
        default=None,
        alias="X-Command-Identity",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    ),
) -> str | None:
    """Opaque BFF-forwarded browser intent; optional only for legacy clients."""
    return command_identity


def _command_fingerprint(body: BaseModel, *, resource_field: str = "",
                          resource_id: str = "", if_match: int | None = None) -> str:
    payload = body.model_dump(mode="json")
    if resource_field:
        payload = {resource_field: resource_id, **payload}
    if if_match is not None:
        payload["_if_match"] = if_match
    return service.compute_fingerprint(payload)


# --------------------------------------------------------------------------
# Server-calculated action eligibility (spec §2) -- the browser must not
# reconstruct these rules; it only renders what comes back here.
# --------------------------------------------------------------------------


def _check(allowed: bool, reason: str) -> tuple[bool, str | None]:
    return (allowed, None if allowed else reason)


def _entry_allowed_actions(entry: dict, context: TenantContext) -> list[ActionEligibility]:
    active = entry.get("lifecycle") == LIFECYCLE_ACTIVE
    draft = entry.get("draft")
    has_draft = draft is not None
    is_owner = context.role in _OWNER_ROLES
    entry_type = entry.get("entry_type")
    # The same rule ``service.publish`` enforces, so a Member is not
    # offered a Publish the command then refuses.
    may_publish = can_publish(entry_type, context.role)
    checks = {
        "save_draft": _check(active and has_draft, "entry_archived" if not active else "no_draft"),
        "validate_draft": _check(active and has_draft, "entry_archived" if not active else "no_draft"),
        "publish": _check(
            active and may_publish and has_draft,
            "entry_archived" if not active else ("owner_required" if not may_publish else "no_draft"),
        ),
        "archive": _check(active and is_owner, "entry_archived" if not active else "owner_required"),
        "bind_reference": _check(active and has_draft, "entry_archived" if not active else "no_draft"),
    }
    if entry_type not in _BINDABLE_ENTRY_TYPES:
        # Only Products and People take reference bindings; other types never
        # list the action rather than listing it as always refused.
        del checks["bind_reference"]
    requirements: dict[str, list[str]] = {
        "publish": ["impact_confirmation"],
        "archive": ["impact_confirmation"],
    }
    return compute_actions(list(checks), checks, requirements)


def _version_allowed_actions(version: dict, context: TenantContext) -> list[ActionEligibility]:
    state = version.get("state")
    entry_type = version.get("entry_type")
    is_owner = context.role in _OWNER_ROLES
    published = state in _PUBLISHED_STATES
    checks = {
        "withdraw": _check(is_owner and published, "owner_required" if not is_owner else "invalid_state"),
        "fork": _check(published, "invalid_state"),
        "clone_product": _check(
            entry_type == ENTRY_PRODUCT and published,
            "entry_type_mismatch" if entry_type != ENTRY_PRODUCT else "invalid_state",
        ),
    }
    requirements: dict[str, list[str]] = {
        "withdraw": ["impact_confirmation"],
    }
    return compute_actions(list(checks), checks, requirements)


def _finalize_entry(entry: dict, context: TenantContext) -> dict:
    draft = entry.get("draft")
    entry["revision"] = draft.get("revision", 1) if draft else 1
    entry["allowed_actions"] = _entry_allowed_actions(entry, context)
    return entry


def _finalize_version(version: dict, context: TenantContext) -> dict:
    version["allowed_actions"] = _version_allowed_actions(version, context)
    return version


# --------------------------------------------------------------------------
# Pagination -- opaque offset cursor bound to workspace + filter set
# (app.platform.pagination, Task 1). The service layer returns plain lists;
# this wraps them into the shared ``Page[T]`` envelope.
# --------------------------------------------------------------------------


def _paginate(items: list, *, context: TenantContext, filters_key: str,
              cursor: str | None, limit: int) -> dict:
    start = 0
    if cursor:
        try:
            start = decode_offset_cursor(cursor, context.workspace_id, filters_key)
        except InvalidCursorError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_cursor", "message": str(exc)},
            ) from exc
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        next_cursor = encode_cursor(context.workspace_id, filters_key, (start + limit,))
    return {"items": page, "next_cursor": next_cursor}


def _limit_query(default: int = 50) -> int:
    return Query(default=default, ge=1, le=100)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@router.get(
    "/command-outcomes/{command_identity}",
    operation_id="get_library_command_outcome",
    response_model=LibraryCommandOutcome,
)
def get_command_outcome(
    command_identity: str = Path(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    ),
    command_kind: str | None = Query(default=None, min_length=1, max_length=40),
    context: TenantContext = Depends(request_tenant_context),
):
    return service.get_command_outcome(
        context,
        command_identity,
        expected_command_kind=command_kind,
    )


@router.post(
    "/command-outcomes/{command_identity}/reconcile",
    operation_id="reconcile_library_command_outcome",
    response_model=LibraryCommandOutcome,
)
def reconcile_command_outcome(
    command_identity: str = Path(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    ),
    command_kind: str | None = Query(default=None, min_length=1, max_length=40),
    context: TenantContext = Depends(request_tenant_context),
):
    return service.reconcile_command_outcome(
        context,
        command_identity,
        expected_command_kind=command_kind,
    )


@router.get(
    "/entry-types",
    operation_id="list_library_entry_types",
    response_model=LibraryEntryTypeList,
)
def list_entry_types(
    _context: TenantContext = Depends(request_tenant_context),
):
    return {"entry_types": list(iter_entry_types())}


@router.get(
    "/entries",
    operation_id="list_library_entries",
    response_model=Page,
)
def list_entries(
    entry_type: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    items = service.list_entries(context, entry_type=entry_type, include_archived=include_archived)
    filters_key = f"entries:{entry_type or ''}:{include_archived}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@router.get(
    "/entries/{entry_id}",
    operation_id="get_library_entry",
    response_model=LibraryEntryDetail,
)
def get_entry(
    entry_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    entry = service.get_entry(context, entry_id)
    _finalize_entry(entry, context)
    set_etag(response, entry["revision"])
    return entry


@router.get(
    "/entries/{entry_id}/versions",
    operation_id="list_library_entry_versions",
    response_model=Page,
)
def list_entry_versions(
    entry_id: str,
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    entry = service.get_entry(context, entry_id)
    filters_key = f"entry_versions:{entry_id}"
    return _paginate(entry.get("versions", []), context=context, filters_key=filters_key,
                      cursor=cursor, limit=limit)


@router.get(
    "/entries/{entry_id}/events",
    operation_id="list_library_entry_events",
    response_model=Page,
)
def list_entry_events(
    entry_id: str,
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    events = service.list_events(context, entry_id)
    filters_key = f"entry_events:{entry_id}"
    return _paginate(events, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@router.get(
    "/versions/{version_id}",
    operation_id="get_library_version",
    response_model=LibraryVersionDetail,
)
def get_version(
    version_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    version = service.get_version(context, version_id)
    _finalize_version(version, context)
    set_etag(response, version["version_no"])
    return version


@router.get(
    "/versions/{version_id}/impact",
    operation_id="get_library_version_impact",
    response_model=LibraryVersionImpact,
)
def get_version_impact(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return service.impact_analysis(context, version_id)


# --------------------------------------------------------------------------
# Impact projections — browser previews before destructive commands
# --------------------------------------------------------------------------


@router.post(
    "/entries/{entry_id}/publish-impact",
    operation_id="preview_library_publish_impact",
    response_model=CommandImpactProjection,
    status_code=201,
)
def publish_impact(
    entry_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return command_impacts.preview_publish_impact(context, entry_id)


@router.post(
    "/entries/{entry_id}/archive-impact",
    operation_id="preview_library_archive_impact",
    response_model=CommandImpactProjection,
    status_code=201,
)
def archive_impact(
    entry_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return command_impacts.preview_archive_impact(context, entry_id)


@router.post(
    "/versions/{version_id}/withdraw-impact",
    operation_id="preview_library_withdraw_impact",
    response_model=CommandImpactProjection,
    status_code=201,
)
def withdraw_impact(
    version_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return command_impacts.preview_withdraw_impact(context, version_id)


@router.get(
    "/versions/{version_id}/bindings",
    operation_id="list_library_version_bindings",
    response_model=Page,
)
def list_version_bindings(
    version_id: str,
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    bindings = service.list_bindings(context, version_id)
    filters_key = f"version_bindings:{version_id}"
    return _paginate(bindings, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


# --------------------------------------------------------------------------
# Internal resource navigation (Gate 0.5)
# --------------------------------------------------------------------------


@router.get(
    "/entries/{root_entry_id}/internal",
    operation_id="list_library_internal_entries",
    response_model=Page,
)
def list_internal_entries(
    root_entry_id: str,
    entry_type: str = Query(...),
    parent_type: str | None = Query(default=None),
    parent_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = _limit_query(),
    context: TenantContext = Depends(request_tenant_context),
):
    items = navigation.list_internal_entries(
        context, root_entry_id, entry_type,
        parent_type=parent_type, parent_id=parent_id,
    )
    filters_key = f"internal:{root_entry_id}:{entry_type}"
    return _paginate(items, context=context, filters_key=filters_key, cursor=cursor, limit=limit)


@router.get(
    "/internal/{entry_type}/{entry_id}",
    operation_id="get_library_internal_entry",
    response_model=InternalEntryMeta,
)
def get_internal_entry(
    entry_type: str,
    entry_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return navigation.get_internal_entry(context, entry_type, entry_id)


@router.get(
    "/entries/{root_entry_id}/internal/{entry_type}/{entry_id}",
    operation_id="get_library_internal_entry_context",
    response_model=InternalEntryContext,
)
def get_internal_entry_context(
    root_entry_id: str,
    entry_type: str,
    entry_id: str,
    parent_type: str | None = Query(default=None),
    parent_id: str | None = Query(default=None),
    context: TenantContext = Depends(request_tenant_context),
):
    return navigation.get_internal_entry_context(
        context, root_entry_id, entry_type, entry_id,
        parent_type=parent_type, parent_id=parent_id,
    )


class CreateInternalEntryInput(BaseModel):
    entry_type: str
    display_name: str = Field(min_length=1, max_length=300)
    content: dict = Field(default_factory=dict)
    parent_type: str | None = None
    parent_id: str | None = None


@router.post(
    "/entries/{root_entry_id}/internal",
    operation_id="create_library_internal_entry",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_internal_entry(
    root_entry_id: str,
    body: CreateInternalEntryInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="root_entry_id", resource_id=root_entry_id)
    entry = navigation.create_internal_entry(
        context, root_entry_id,
        entry_type=body.entry_type,
        display_name=body.display_name,
        content=body.content,
        parent_type=body.parent_type,
        parent_id=body.parent_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
        command_identity=command_identity,
    )
    return _finalize_entry(entry, context)


# --------------------------------------------------------------------------
# Commands -- every route below carries x-marketai-idempotent: true and
# requires Idempotency-Key (422 if missing/blank).
# --------------------------------------------------------------------------


class CreateEntryInput(BaseModel):
    entry_type: str
    display_name: str = Field(min_length=1, max_length=300)
    content: dict = Field(default_factory=dict)


@router.post(
    "/entries",
    operation_id="create_library_entry",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_entry(
    body: CreateEntryInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body)
    entry = service.create_entry(
        context, entry_type=body.entry_type, display_name=body.display_name,
        content=body.content, idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )
    return _finalize_entry(entry, context)


class SaveDraftInput(BaseModel):
    content: dict


@router.put(
    "/entries/{entry_id}/draft",
    operation_id="save_library_draft",
    response_model=LibraryEntryDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def save_draft(
    entry_id: str,
    body: SaveDraftInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                               resource_id=entry_id, if_match=expected_revision)
    service.save_draft(
        context, entry_id, body.content, expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )
    entry = service.get_entry(context, entry_id)
    return _finalize_entry(entry, context)


class ValidateInput(BaseModel):
    pass


@router.post(
    "/entries/{entry_id}/validate",
    operation_id="validate_library_draft",
    response_model=LibraryValidationResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def validate_draft(
    entry_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(ValidateInput(), resource_field="entry_id",
                               resource_id=entry_id, if_match=expected_revision)
    return service.validate_draft(
        context, entry_id, expected_revision=expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )


class PublishInput(BaseModel):
    acknowledged_warnings: list[str] = Field(default_factory=list)
    impact_id: str | None = None
    impact_hash: str | None = None


@router.post(
    "/entries/{entry_id}/publish",
    operation_id="publish_library_entry",
    response_model=LibraryVersionDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def publish(
    entry_id: str,
    body: PublishInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                               resource_id=entry_id, if_match=expected_revision)
    version = service.publish(
        context, entry_id, expected_revision,
        acknowledged_warnings=body.acknowledged_warnings,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
        impact_id=body.impact_id, impact_hash=body.impact_hash,
    )
    return _finalize_version(version, context)


class ArchiveInput(BaseModel):
    impact_id: str | None = None
    impact_hash: str | None = None


@router.post(
    "/entries/{entry_id}/archive",
    operation_id="archive_library_entry",
    response_model=LibraryEntryDetail,
    openapi_extra={"x-marketai-idempotent": True},
)
def archive(
    entry_id: str,
    request: Request,
    body: ArchiveInput = Body(default=None),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    if body is None:
        body = ArchiveInput()
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                               resource_id=entry_id, if_match=expected_revision)
    entry = service.archive_entry(
        context, entry_id, expected_revision=expected_revision,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
        impact_id=body.impact_id, impact_hash=body.impact_hash,
    )
    return _finalize_entry(entry, context)


class BindInput(BaseModel):
    reference_version_id: str
    purpose: str = Field(min_length=1, max_length=100)
    viewpoint: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=0, ge=0)
    note: str = ""
    conditions: dict = Field(default_factory=dict)


@router.post(
    "/entries/{entry_id}/bind",
    operation_id="bind_library_reference",
    response_model=DraftSummary,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def bind(
    entry_id: str,
    body: BindInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    expected_revision = extract_if_match(request)
    fp = _command_fingerprint(body, resource_field="entry_id",
                               resource_id=entry_id, if_match=expected_revision)
    return service.bind_reference(
        context, entry_id, expected_revision,
        reference_version_id=body.reference_version_id,
        purpose=body.purpose, viewpoint=body.viewpoint,
        priority=body.priority, note=body.note, conditions=body.conditions,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )


class WithdrawInput(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    impact_id: str | None = None
    impact_hash: str | None = None


@router.post(
    "/versions/{version_id}/withdraw",
    operation_id="withdraw_library_version",
    response_model=LibraryVersionImpact,
    openapi_extra={"x-marketai-idempotent": True},
)
def withdraw(
    version_id: str,
    body: WithdrawInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    return service.withdraw_version(
        context, version_id, reason=body.reason,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
        impact_id=body.impact_id, impact_hash=body.impact_hash,
    )


class ForkInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)


@router.post(
    "/versions/{version_id}/fork",
    operation_id="fork_library_version",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def fork(
    version_id: str,
    body: ForkInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    entry = service.fork_version(
        context, version_id, display_name=body.display_name,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )
    return _finalize_entry(entry, context)


class CloneProductInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)
    include_optional: list[str] = Field(default_factory=list)


@router.post(
    "/versions/{version_id}/clone-product",
    operation_id="clone_library_product",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def clone_product(
    version_id: str,
    body: CloneProductInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="version_id", resource_id=version_id)
    entry = service.clone_product(
        context, version_id, display_name=body.display_name,
        include_optional=body.include_optional,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )
    return _finalize_entry(entry, context)


# --------------------------------------------------------------------------
# Template and DataSource operations (Gate 0.7)
# --------------------------------------------------------------------------


class ApplyTemplateInput(BaseModel):
    product_entry_id: str
    display_name: str = Field(min_length=1, max_length=300)


@router.post(
    "/versions/{template_version_id}/apply",
    operation_id="apply_library_template",
    response_model=LibraryEntryDetail,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def apply_template(
    template_version_id: str,
    body: ApplyTemplateInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="template_version_id", resource_id=template_version_id)
    entry = templates.apply_template(
        context, template_version_id,
        product_entry_id=body.product_entry_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
    )
    return _finalize_entry(entry, context)


class ReapplyPreviewInput(BaseModel):
    existing_schema_version_id: str


@router.post(
    "/versions/{template_version_id}/reapply-preview",
    operation_id="preview_library_template_reapply",
    response_model=TemplateDiffReportResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def reapply_preview(
    template_version_id: str,
    body: ReapplyPreviewInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="template_version_id", resource_id=template_version_id)
    return templates.reapply_template(
        context, template_version_id,
        existing_schema_version_id=body.existing_schema_version_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
    )


# --------------------------------------------------------------------------
# DataSource credential binding (Gate 0.7)
# --------------------------------------------------------------------------


@router.get(
    "/entries/{data_source_entry_id}/credential-binding",
    operation_id="get_library_data_source_credential_binding",
    response_model=CredentialBindingSummary,
)
def get_credential_binding(
    data_source_entry_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    result = datasources.get_active_binding(context, data_source_entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "no_active_binding", "message": "该数据源没有活跃的凭据绑定"})
    return result


class BindCredentialInput(BaseModel):
    credential_version_id: str


@router.post(
    "/entries/{data_source_entry_id}/credential-binding",
    operation_id="bind_library_data_source_credential",
    response_model=CredentialBindingSummary,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def bind_credential(
    data_source_entry_id: str,
    body: BindCredentialInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    fp = _command_fingerprint(body, resource_field="data_source_entry_id", resource_id=data_source_entry_id)
    return datasources.bind_credential(
        context, data_source_entry_id,
        credential_version_id=body.credential_version_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fp,
    )


# --------------------------------------------------------------------------
# DataSource connection checks (Gate 0.7)
# --------------------------------------------------------------------------


class ConnectionCheckInput(BaseModel):
    # Accepted for wire compatibility but never decides anything: the Worker
    # checks in the effective version's mode and only records this one
    # (#383). Nullable without a default so the generated TypeScript type is
    # optional and the Web client sends nothing.
    execution_mode: str | None = Field(
        default=None,
        max_length=40,
        deprecated=True,
        description="Ignored. A data source is checked in its published version's execution mode.",
    )


@router.post(
    "/entries/{data_source_entry_id}/connection-checks",
    operation_id="check_library_data_source_connection",
    response_model=OperationAccepted,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def check_connection(
    data_source_entry_id: str,
    body: ConnectionCheckInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    """Queue a local check of the data source (#383, ``app.library.connection_checks``).

    The Worker takes the execution mode from the effective version;
    ``execution_mode`` is only recorded beside it. A replayed Idempotency-Key
    returns the Operation it created, wherever that has got to; the same key
    for another data source is ``409 idempotency_key_reused``.
    """
    from sqlalchemy import select

    from ..adapters.persistence.database import tenant_session
    from ..adapters.persistence.models import JobRecord
    from ..platform.jobs import enqueue_in_session
    from ..platform.operations import create_operation, get_operation

    job_key = f"connection_check:{idempotency_key}"
    with tenant_session(context) as session:
        replayed = session.scalar(
            select(JobRecord).where(
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.idempotency_key == job_key,
            )
        )
        if replayed is not None:
            if replayed.resource_id != data_source_entry_id:
                raise service.IdempotencyConflict(
                    "该 Idempotency-Key 已用于另一个数据源的连接检查"
                )
            operation = get_operation(session, str(replayed.payload_json["operation_id"]))
            return {"operation_id": operation.id, "state": operation.state, "command_type": "connection_check"}

    entry = service.get_entry(context, data_source_entry_id)
    if entry.get("entry_type") != "data_source":
        raise HTTPException(status_code=422, detail={"code": "entry_type_mismatch", "message": "条目不是数据源"})
    if entry.get("lifecycle") != LIFECYCLE_ACTIVE:
        raise HTTPException(status_code=409, detail={"code": "entry_archived", "message": "数据源已归档"})

    with tenant_session(context) as session:
        operation = create_operation(
            session,
            command_type="connection_check",
            idempotency_key_hash=service.compute_fingerprint({"key": idempotency_key}),
        )
        enqueue_in_session(
            session,
            context=context,
            job_type="library.connection_check",
            payload={
                "data_source_entry_id": data_source_entry_id,
                "operation_id": operation.id,
                # Recorded as the requested mode only; read without the
                # deprecated attribute access, which warns.
                "execution_mode": body.model_dump()["execution_mode"],
            },
            idempotency_key=job_key,
            resource_type="data_source",
            resource_id=data_source_entry_id,
            # No external side effect, and the evidence settles with the
            # Operation: a rerun after a crash finds it settled.
            safe_to_retry_after_crash=True,
            max_attempts=3,
        )
    return {"operation_id": operation.id, "state": "queued", "command_type": "connection_check"}


@router.get(
    "/entries/{data_source_entry_id}/connection-checks/{check_id}",
    operation_id="get_library_data_source_connection_check",
    response_model=DataSourceConnectionCheck,
)
def get_connection_check(
    data_source_entry_id: str,
    check_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    """One finished check: ``check_id`` is the ``resource_id`` of its Operation's result."""
    return connection_checks.get_connection_check(context, data_source_entry_id, check_id)


# --------------------------------------------------------------------------
# Manual fact imports (Gate 0.7)
# --------------------------------------------------------------------------


class ManualFactImportInput(BaseModel):
    upload_intent_id: str
    purpose: str = Field(default="library_manual_fact_import", min_length=1, max_length=100)


@router.post(
    "/entries/{data_source_entry_id}/manual-fact-imports",
    operation_id="import_library_manual_facts",
    response_model=OperationAccepted,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def import_manual_facts(
    data_source_entry_id: str,
    body: ManualFactImportInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    # Queue an import of a finalized upload into a new Manual Fact Set draft
    # (#397). A replayed Idempotency-Key returns the Operation it created,
    # wherever that has got to, when the data source, the upload and the
    # initiator are all the same; anything else under that key is
    # ``409 idempotency_key_reused``. An upload another key already imports is
    # ``409 upload_intent_consumed`` unless that import failed or was
    # cancelled. (A docstring here would become the OpenAPI description.)
    from sqlalchemy import select

    from ..adapters.persistence.database import tenant_session
    from ..adapters.persistence.models import JobRecord
    from ..platform.jobs import enqueue_in_session
    from ..platform.operations import create_operation, get_operation

    if body.purpose != "library_manual_fact_import":
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_purpose", "message": "purpose 必须是 library_manual_fact_import"},
        )

    job_type = "library.manual_fact_import"
    job_key = f"manual_fact_import:{idempotency_key}"
    with tenant_session(context) as session:
        replayed = session.scalar(
            select(JobRecord).where(
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.idempotency_key == job_key,
            )
        )
        if replayed is not None:
            if (
                replayed.resource_id != data_source_entry_id
                or replayed.payload_json.get("upload_intent_id") != body.upload_intent_id
                or (replayed.initiator_id or "") != (context.initiator_id or "")
            ):
                raise service.IdempotencyConflict(
                    "该 Idempotency-Key 已用于另一次手工事实导入"
                )
            operation = get_operation(session, str(replayed.payload_json["operation_id"]))
            return {"operation_id": operation.id, "state": operation.state, "command_type": "manual_fact_import"}

    entry = service.get_entry(context, data_source_entry_id)
    if entry.get("entry_type") != "data_source":
        raise HTTPException(status_code=422, detail={"code": "entry_type_mismatch", "message": "条目不是数据源"})
    if entry.get("lifecycle") != LIFECYCLE_ACTIVE:
        raise HTTPException(status_code=409, detail={"code": "entry_archived", "message": "数据源已归档"})

    with tenant_session(context) as session:
        consumed = session.scalar(
            select(JobRecord.id).where(
                JobRecord.workspace_id == context.workspace_id,
                JobRecord.type == job_type,
                JobRecord.payload_json["upload_intent_id"].as_string() == body.upload_intent_id,
                JobRecord.state.not_in(("failed", "cancelled")),
            ).limit(1)
        )
        if consumed is not None:
            raise service.LibraryConflict("该上传已被另一次导入使用", code="upload_intent_consumed")
        operation = create_operation(
            session,
            command_type="manual_fact_import",
            idempotency_key_hash=service.compute_fingerprint({"key": idempotency_key}),
        )
        enqueue_in_session(
            session,
            context=context,
            job_type=job_type,
            payload={
                "data_source_entry_id": data_source_entry_id,
                "operation_id": operation.id,
                "upload_intent_id": body.upload_intent_id,
            },
            idempotency_key=job_key,
            resource_type="data_source",
            resource_id=data_source_entry_id,
            # No external side effect, and the draft commits with the
            # Operation: a rerun after a crash finds both or neither.
            safe_to_retry_after_crash=True,
            max_attempts=1,
        )
    return {"operation_id": operation.id, "state": "queued", "command_type": "manual_fact_import"}


# --------------------------------------------------------------------------
# Reference creation, gate evidence, promotion and download (Gate 0.8, #158)
# --------------------------------------------------------------------------


class ReferenceRightsDeclarationInput(BaseModel):
    """The uploader's rights declaration (contract §7.2: 权利状态、授权范围、
    有效期和操作者声明). ``state`` is always ``granted`` and ``declared_by`` is
    always the current initiator -- neither is accepted from the client."""

    scope: str = Field(min_length=1, max_length=200)
    valid_to: str = Field(min_length=10, max_length=10, description="ISO date (YYYY-MM-DD)")
    evidence: str | None = Field(default=None, max_length=1000)


class CreateReferenceObjectInput(BaseModel):
    upload_intent_id: str
    filename: str = ""
    declared_mime: str = ""
    # Accepted for wire compatibility with the previous Web release but never
    # read: the Worker resolves the content-safety mode from deployment
    # configuration (contract §18 -- the caller must not pick the Mock).
    # Nullable without a default so the generated TypeScript type is optional
    # and the current Web client can stop sending them altogether.
    execution_mode: str | None = Field(
        default=None,
        deprecated=True,
        description="Ignored. The content-safety execution mode is decided by the server.",
    )
    safety_scenario: str | None = Field(
        default=None,
        deprecated=True,
        description="Ignored. The controlled-Mock scenario is decided by the server.",
    )
    rights: ReferenceRightsDeclarationInput | None = None


@router.post(
    "/reference-objects",
    operation_id="create_library_reference_object",
    response_model=OperationAccepted,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_reference_object(
    body: CreateReferenceObjectInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    from . import reference_commands

    return reference_commands.create_reference_from_upload_intent(
        context,
        upload_intent_id=body.upload_intent_id,
        filename=body.filename,
        declared_mime=body.declared_mime,
        rights=body.rights.model_dump(exclude_none=True) if body.rights else None,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/reference-objects/{object_id}",
    operation_id="get_library_reference_object",
    response_model=ReferenceObjectDetail,
)
def get_reference_object(
    object_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    from . import reference_commands

    return reference_commands.get_reference_object(context, object_id)


class PromoteReferenceBindInput(BaseModel):
    """Every field but ``entry_id`` is nullable without a default: the server
    fills the defaults (``service.PROMOTE_DEFAULT_PURPOSE`` / ``_VIEWPOINT``,
    priority 0, empty note/conditions), and the generated TypeScript type stays
    optional so the automatic post-upload bind sends only the entry id."""

    entry_id: str = Field(min_length=1)
    purpose: str | None = Field(default=None, max_length=100)
    viewpoint: str | None = Field(default=None, max_length=100)
    priority: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=1000)
    conditions: dict | None = None
    # The product/person Draft revision to claim; omitted for the automatic
    # post-upload bind, where the server claims the Draft's current revision.
    expected_revision: int | None = Field(default=None, ge=1)


class PromoteReferenceObjectInput(BaseModel):
    display_name: str | None = Field(default=None, max_length=300)
    rights: ReferenceRightsDeclarationInput | None = None
    bind: PromoteReferenceBindInput | None = None


@router.post(
    "/reference-objects/{object_id}/promote",
    operation_id="promote_library_reference_object",
    response_model=ReferencePromotionResult,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
def promote_reference_object(
    object_id: str,
    body: PromoteReferenceObjectInput = Body(default=None),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
    command_identity: str | None = Depends(_command_identity),
):
    """Publish a released reference object as a ReferenceAssetVersion and,
    optionally, bind it to a product/person Draft.

    ``reference_asset`` is Member-publishable (contract §4) and every byte of
    the version content is server-derived gate evidence -- see
    ``service.promote_reference_object``. Owner-only publish types are
    untouched -- this command can only ever create ``reference_asset``
    entries.
    """
    if body is None:
        body = PromoteReferenceObjectInput()
    fp = _command_fingerprint(body, resource_field="object_id", resource_id=object_id)
    return service.promote_reference_object(
        context, object_id,
        display_name=body.display_name,
        rights=body.rights.model_dump(exclude_none=True) if body.rights else None,
        bind=body.bind.model_dump() if body.bind else None,
        idempotency_key=idempotency_key, request_fingerprint=fp,
        command_identity=command_identity,
    )


@router.post(
    "/versions/{version_id}/download-intent",
    operation_id="create_library_reference_download_intent",
    response_model=ReferenceDownloadGrant,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_reference_download_intent(
    version_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Issue a signed direct link for a published reference's controlled derivative.

    Same seam as generated-asset downloads (``app.generation.v1_router``):
    the derivative is resolved server-side from the Version, an immutable
    ``DownloadIntentRecord`` is written (idempotently), and the ``BlobStore``
    adapter signs a short-lived GET the browser opens against the storage
    origin. Nothing binary passes through FastAPI or the BFF.
    """
    from datetime import datetime, timezone

    from ..adapters.persistence.database import tenant_session
    from ..platform.blob_store import DownloadSpec
    from ..platform.download_intents import (
        DEFAULT_DOWNLOAD_TTL_SECONDS,
        create_or_replay_download_intent,
    )
    from ..platform.idempotency import compute_fingerprint
    from . import reference_commands

    facts = reference_commands.resolve_reference_derivative(
        context, version_id=version_id,
    )
    committed_ref = facts["committed_ref"]
    if committed_ref is None:
        committed_ref = await reference_commands.materialize_local_derivative(
            context, _blob_store, object_id=facts["object_id"],
        )

    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_library_reference_download_intent",
        request.url.path,
        {},
        None,
        {},
    )
    with tenant_session(context) as session:
        intent = create_or_replay_download_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="library.reference_download_intent",
            fingerprint_hash=fingerprint_hash,
            owner_kind="reference_asset_version",
            owner_id=version_id,
            committed_ref=committed_ref,
        )
        expires_at = intent.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

    remaining_seconds = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    grant = await _blob_store.issue_download(
        committed_ref,
        DownloadSpec(
            filename=facts["filename"],
            expires_in_seconds=min(DEFAULT_DOWNLOAD_TTL_SECONDS, remaining_seconds),
        ),
    )

    return {
        "version_id": version_id,
        "object_id": facts["object_id"],
        "download_url": grant.url,
        "content_disposition": grant.content_disposition,
        "expires_at": grant.expires_at.isoformat(),
        "derivative_mime": facts["derivative_mime"],
        "derivative_byte_size": facts["derivative_byte_size"],
    }
