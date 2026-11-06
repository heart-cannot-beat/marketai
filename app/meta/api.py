"""HTTP endpoints for the Meta read-only phase."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import connections as connections_service
from . import observation as observation_service
from . import service
from .models import (
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ObservationContractGenerationRecord,
)
from ..adapters.persistence.database import tenant_session
from ..platform.http_identity import request_tenant_context
from ..platform.meta_mode_auth import MetaModeNotPermitted, require_meta_mode
from ..platform.pagination import InvalidCursorError, decode_cursor, encode_cursor
from ..platform.tenancy import TenantContext, TenantScopeError


router = APIRouter(prefix="/api/meta", tags=["meta-read-only"])


class SyncRequest(BaseModel):
    date_start: date | None = None
    date_stop: date | None = None
    force: bool = False


@router.get("/status")
def status(context: TenantContext = Depends(request_tenant_context)):
    try:
        return service.sync_status(context)
    except service.MetaConfigurationError as exc:
        raise HTTPException(
            503, {"code": "meta_not_configured", "message": str(exc)}
        ) from exc


@router.post("/sync", status_code=202)
def start_sync(body: SyncRequest, context: TenantContext = Depends(request_tenant_context)):
    try:
        sync_id = service.request_sync(context, body.date_start, body.date_stop, force=body.force)
    except service.MetaConfigurationError as exc:
        raise HTTPException(503, {"code": "meta_not_configured", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": sync_id, "state": "queued"}


@router.get("/syncs/{sync_id}")
def get_sync(sync_id: str, context: TenantContext = Depends(request_tenant_context)):
    result = service.sync_detail(context, sync_id)
    if result is None:
        raise HTTPException(404, "Meta sync 不存在")
    return result


@router.post("/reconcile")
def reconcile(context: TenantContext = Depends(request_tenant_context)):
    try:
        return service.reconcile(context)
    except service.MetaConfigurationError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------------------------------------------------------------------------
# Meta domain (Task 16, spec §8.2): connection/binding lifecycle and
# observation contract generations. Separate router (`/api/v1/meta`) from
# the read-only-phase `router` above (`/api/meta`), which stays untouched.
#
# Every route here opens its own `tenant_session(context)` and calls a
# Task 10/11 service function with the raw `session` -- those functions
# manage their own flush but never their own transaction boundary, matching
# the pattern documented on `app.launch.service.create_multi_candidate_preview`.
# ---------------------------------------------------------------------------

router_v1 = APIRouter(prefix="/api/v1/meta", tags=["meta-domain"])


class CreateConnectionRequest(BaseModel):
    meta_mode: str
    ad_account_id: str | None = None


class MetaModeRequest(BaseModel):
    """Body for POST routes that otherwise carry no payload but still need
    to bind the request to a Meta mode (see `_effective_context` below)."""

    meta_mode: str


class CreateBindingRequest(BaseModel):
    ad_account_id: str
    meta_mode: str
    access_token_version_id: str | None = None
    app_secret_version_id: str | None = None


class SupersedeBindingRequest(BaseModel):
    ad_account_id: str
    meta_mode: str
    access_token_version_id: str | None = None
    app_secret_version_id: str | None = None


class CreateContractRequest(BaseModel):
    connection_id: str
    meta_mode: str
    contract_type: str
    ad_account_id: str
    fields: list
    attribution_window: dict
    breakdowns: list
    time_window: dict
    local_timezone: str
    delivery_config: dict | None = None


def _connection_dict(row: MetaConnectionRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "meta_mode": row.meta_mode,
        "ad_account_id": row.ad_account_id,
        "state": row.state,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "retired_at": row.retired_at.isoformat() if row.retired_at else None,
    }


def _binding_dict(row: MetaDataSourceBindingRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "meta_mode": row.meta_mode,
        "meta_connection_id": row.meta_connection_id,
        "ad_account_id": row.ad_account_id,
        "state": row.state,
        "superseded_by_id": row.superseded_by_id,
        "superseded_at": row.superseded_at.isoformat() if row.superseded_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _contract_dict(row: ObservationContractGenerationRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "meta_mode": row.meta_mode,
        "meta_connection_id": row.meta_connection_id,
        "contract_type": row.contract_type,
        "generation_number": row.generation_number,
        "ad_account_id": row.ad_account_id,
        "fields": row.fields,
        "attribution_window": row.attribution_window,
        "breakdowns": row.breakdowns,
        "time_window": row.time_window,
        "delivery_config": row.delivery_config,
        "contract_hash": row.contract_hash,
        "local_timezone": row.local_timezone,
        "state": row.state,
        "clock_started_at": row.clock_started_at.isoformat() if row.clock_started_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _connection_error_status(exc: connections_service.ConnectionLifecycleError) -> int:
    if isinstance(exc, connections_service.MetaForbidden):
        return 403
    if isinstance(exc, connections_service.ConnectionAlreadyExists):
        return 409
    return 404 if exc.code.endswith("_not_found") else 400


def _connection_problem(exc: connections_service.ConnectionLifecycleError) -> HTTPException:
    return HTTPException(_connection_error_status(exc), {"code": exc.code, "message": str(exc)})


def _effective_context(context: TenantContext, meta_mode: str) -> TenantContext:
    """Bind `meta_mode` onto the verified `TenantContext` for this request.

    `request_tenant_context()` never populates `meta_mode` -- it comes from
    verified BFF/delegated-identity claims (spec §3), which carry Workspace
    and actor identity but say nothing about which Meta mode a given
    request targets (spec §2: a Workspace may operate connections in more
    than one mode). Every Meta-domain route therefore accepts `meta_mode`
    explicitly (request body field or query parameter) and binds it here.

    ``require_meta_mode`` checks the context's ``identity_kind`` --
    delegated users may not select ``internal_validation`` (ADR 0009).
    """
    try:
        require_meta_mode(context, meta_mode)
        return replace(context, meta_mode=meta_mode)
    except MetaModeNotPermitted as exc:
        status = 400 if exc.code == "invalid_meta_mode" else 403
        raise HTTPException(status, {"code": exc.code, "message": exc.detail}) from exc
    except TenantScopeError as exc:
        raise HTTPException(400, {"code": "invalid_meta_mode", "message": str(exc)}) from exc


def _paginate(items: list, *, context: TenantContext, filters_key: str,
              cursor: str | None, limit: int) -> dict:
    """Opaque offset cursor bound to workspace + filter set (mirrors
    ``app.library.v1_router._paginate``)."""
    start = 0
    if cursor:
        try:
            decoded = decode_cursor(cursor, context.workspace_id, filters_key)
        except InvalidCursorError as exc:
            raise HTTPException(
                status_code=400, detail={"code": "invalid_cursor", "message": str(exc)},
            ) from exc
        start = int(decoded[0]) if decoded else 0
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        next_cursor = encode_cursor(context.workspace_id, filters_key, (start + limit,))
    return {"items": page, "next_cursor": next_cursor}


@router_v1.post("/connections", status_code=201, operation_id="create_meta_connection")
def create_connection_v1(
    body: CreateConnectionRequest, context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    try:
        with tenant_session(effective) as session:
            try:
                connection = connections_service.create_connection(
                    session, context=effective, ad_account_id=body.ad_account_id,
                )
            except connections_service.ConnectionLifecycleError as exc:
                raise _connection_problem(exc) from exc
    except IntegrityError as exc:
        if "uq_meta_connection_active" in str(exc).lower():
            raise HTTPException(
                409, {"code": "connection_already_exists",
                      "message": "active connection already exists"},
            ) from exc
        raise
    return _connection_dict(connection)


@router_v1.get("/connections", operation_id="list_meta_connections")
def list_meta_connections(
    meta_mode: str = Query(...),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        rows = session.scalars(
            select(MetaConnectionRecord)
            .where(
                MetaConnectionRecord.workspace_id == effective.workspace_id,
                MetaConnectionRecord.meta_mode == effective.meta_mode,
            )
            .order_by(MetaConnectionRecord.created_at)
        ).all()
        items = [_connection_dict(row) for row in rows]
    return _paginate(
        items, context=effective,
        filters_key=f"meta_connections:{effective.meta_mode}",
        cursor=cursor, limit=limit,
    )


@router_v1.get("/connections/{connection_id}", operation_id="get_meta_connection")
def get_connection_v1(
    connection_id: str,
    meta_mode: str = Query(...),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        connection = session.scalar(
            select(MetaConnectionRecord).where(
                MetaConnectionRecord.id == connection_id,
                MetaConnectionRecord.workspace_id == effective.workspace_id,
                MetaConnectionRecord.meta_mode == effective.meta_mode,
            )
        )
        if connection is None:
            raise HTTPException(404, {"code": "connection_not_found", "message": "Meta connection not found"})
        return _connection_dict(connection)


@router_v1.post("/connections/{connection_id}/retire", operation_id="retire_meta_connection")
def retire_connection_v1(
    connection_id: str,
    body: MetaModeRequest,
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    with tenant_session(effective) as session:
        try:
            connection = connections_service.retire_connection(
                session, context=effective, connection_id=connection_id,
            )
        except connections_service.ConnectionLifecycleError as exc:
            raise _connection_problem(exc) from exc
        return _connection_dict(connection)


@router_v1.get("/connections/{connection_id}/bindings", operation_id="list_meta_bindings")
def list_bindings_v1(
    connection_id: str,
    meta_mode: str = Query(...),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        rows = session.scalars(
            select(MetaDataSourceBindingRecord)
            .where(
                MetaDataSourceBindingRecord.workspace_id == effective.workspace_id,
                MetaDataSourceBindingRecord.meta_mode == effective.meta_mode,
                MetaDataSourceBindingRecord.meta_connection_id == connection_id,
            )
            .order_by(MetaDataSourceBindingRecord.created_at)
        ).all()
        items = [_binding_dict(row) for row in rows]
    return _paginate(
        items, context=effective, filters_key=f"meta_bindings:{connection_id}",
        cursor=cursor, limit=limit,
    )


@router_v1.post("/connections/{connection_id}/bindings", status_code=201, operation_id="create_meta_binding")
def create_binding_v1(
    connection_id: str,
    body: CreateBindingRequest,
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    with tenant_session(effective) as session:
        try:
            binding = connections_service.create_binding(
                session, context=effective, connection_id=connection_id,
                ad_account_id=body.ad_account_id,
                access_token_version_id=body.access_token_version_id,
                app_secret_version_id=body.app_secret_version_id,
            )
        except connections_service.ConnectionLifecycleError as exc:
            raise _connection_problem(exc) from exc
        return _binding_dict(binding)


@router_v1.post(
    "/connections/{connection_id}/bindings/{binding_id}/supersede",
    status_code=201,
    operation_id="supersede_meta_binding",
)
def supersede_binding_v1(
    connection_id: str,
    binding_id: str,
    body: SupersedeBindingRequest,
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    with tenant_session(effective) as session:
        try:
            new_binding = connections_service.supersede_binding(
                session, context=effective, old_binding_id=binding_id, connection_id=connection_id,
                ad_account_id=body.ad_account_id,
                access_token_version_id=body.access_token_version_id,
                app_secret_version_id=body.app_secret_version_id,
            )
        except connections_service.ConnectionLifecycleError as exc:
            raise _connection_problem(exc) from exc
        return _binding_dict(new_binding)


@router_v1.get("/contracts", operation_id="list_meta_contracts")
def list_contracts_v1(
    meta_mode: str = Query(...),
    connection_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        stmt = select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == effective.workspace_id,
            ObservationContractGenerationRecord.meta_mode == effective.meta_mode,
        )
        if connection_id:
            stmt = stmt.where(ObservationContractGenerationRecord.meta_connection_id == connection_id)
        rows = session.scalars(stmt.order_by(ObservationContractGenerationRecord.created_at)).all()
        items = [_contract_dict(row) for row in rows]
    return _paginate(
        items, context=effective, filters_key=f"meta_contracts:{connection_id or ''}",
        cursor=cursor, limit=limit,
    )


@router_v1.post("/contracts", status_code=201, operation_id="create_meta_contract")
def create_contract_v1(
    body: CreateContractRequest, context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    generation = None
    try:
        with tenant_session(effective) as session:
            try:
                generation = observation_service.create_contract_generation(
                    session,
                    workspace_id=effective.workspace_id,
                    meta_mode=effective.meta_mode,
                    connection_id=body.connection_id,
                    contract_type=body.contract_type,
                    ad_account_id=body.ad_account_id,
                    fields=body.fields,
                    attribution_window=body.attribution_window,
                    breakdowns=body.breakdowns,
                    time_window=body.time_window,
                    local_timezone=body.local_timezone,
                    delivery_config=body.delivery_config,
                )
            except observation_service.ContractError as exc:
                status = 409 if exc.code == "meta_contract_already_exists" else 400
                raise HTTPException(status, {"code": exc.code, "message": str(exc)}) from exc
    except IntegrityError as exc:
        # `create_contract_generation` does not pre-verify that
        # `connection_id` resolves to a real MetaConnectionRecord (Task 11
        # report concern): an unknown connection surfaces here as the
        # composite FK `fk_ocg_workspace_mode_conn` rejecting the insert.
        raise HTTPException(
            404, {"code": "connection_not_found", "message": "connection_id does not exist"},
        ) from exc
    return _contract_dict(generation)
