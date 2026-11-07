"""MarketAI Studio — 前期生图工作台（产品 → 角度 → 组合 → 生图 → 质量审核）。
v9 双状态：质量审核（人判"能不能用"）由本页维护；投放判定（数据判"好不好卖"）
由 Meta 阶段终判回写 /verdict，两套状态独立。"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import config, store
from .angle_engine import generate_dimensions
from .pipeline import build_combos, manager
from .providers.router import get_provider, providers_info
from .schemas import AngleSchema, GenRequest, Plan, PlanRequest, Product, ProductCreate
from .meta.api import router as meta_router, router_v1 as meta_v1_router
from .meta.platform_api import router as meta_platform_router
from .meta.test_support import (
    delegated_router as meta_test_support_delegated_router,
    router as meta_test_support_router,
)
from .experiments.api import router as experiment_router
from .launch.api import router as launch_router, router_v1 as launch_v1_router
from .localization.routes import register_problem_handlers as register_localization_problem_handlers
from .localization.routes import router as localization_router
from .library.api import register_problem_handlers
from .generation.api import register_problem_handlers as register_generation_problem_handlers
from .entitlements.api import internal_router as entitlement_internal_router
from .entitlements.api import v1_entitlement_router
from .adapters.persistence.database import (
    SessionLocal,
    assert_local_identity_boundary,
    assert_runtime_role,
    database_ready,
    engine,
    tenant_session,
)
from sqlalchemy import select, text
from .adapters.persistence.models import AuditEventRecord, new_id, utcnow
from .platform import jobs as jobs_platform
from .platform.blob_store import (
    BlobStore,
    CommittedBlobRef,
    DownloadSpec,
    UploadSpec,
)
from .platform.blob_store_factory import build_blob_store
from .platform.delegated_identity import DelegatedIdentityMiddleware, IdentityConfig
from .platform.download_intents import (
    DEFAULT_DOWNLOAD_TTL_SECONDS,
    create_download_intent_record,
    get_download_intent,
)
from .platform.http_identity import request_tenant_context
from .platform.audit import record_event
from .platform.pagination import InvalidCursorError, decode_offset_cursor, encode_cursor
from .platform.idempotency import (
    IdempotencyKeyReusedError,
    compute_fingerprint,
    create_or_replay_intent,
    create_or_replay_operation,
    get_idempotency_record,
    hash_idempotency_key,
)
from .platform.jwks_store import JwksKeyStore
from .platform.platform_operator_identity import PlatformOperatorJWTVerifier
from .platform.operations import (
    OperationNotFoundError,
    OperationState,
    get_operation as fetch_operation,
    result_ref_of,
)
from .platform.projection_sync import (
    ControlPlaneAuthorizationSnapshotHttpAdapter,
    PostgresWorkspaceProjectionStore,
    WorkspaceProjectionSyncer,
)
from .platform.purpose_policies import PURPOSE_POLICIES
from .platform.tenancy import TenantContext
from .platform.upload_intents import (
    UploadIntentNotFoundError,
    build_staging_key,
    create_upload_intent_record,
    expire_upload_intent,
    get_upload_intent,
)
from .platform.workspace_admission import WorkspaceAdmissionReader, WorkspaceNotFoundError

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# ---------------- delegated identity wiring (spec §3) ----------------
# Built once at import time so `app.add_middleware` -- which Starlette
# refuses to call once the middleware stack has been built -- can run before
# the app ever handles a request. The async pieces (the JWKS store's initial
# load and background refresh timer, the HTTP clients) are started and torn
# down in `lifespan` instead.
#
# Wired only when `DELEGATED_JWKS_URL` is configured and local identity is
# not the active bypass: neither is set by default (CI, `.env.example`,
# `run.py`'s SQLite launcher), so the legacy `/api/*` surface -- outside the
# middleware's `/api/v1/*` protection scope in any case -- and today's test
# suite are unaffected until a deployment opts in.
_jwks_http_client: httpx.AsyncClient | None = None
_projection_http_client: httpx.AsyncClient | None = None
_jwks_store: JwksKeyStore | None = None

if config.DELEGATED_IDENTITY_ENABLED:
    _jwks_http_client = httpx.AsyncClient()
    _jwks_store = JwksKeyStore(
        jwks_url=config.DELEGATED_JWKS_URL,
        max_stale_seconds=config.DELEGATED_JWKS_MAX_STALE_SECONDS,
        http_client=_jwks_http_client,
    )
    _projection_http_client = httpx.AsyncClient()
    _snapshot_fetcher = ControlPlaneAuthorizationSnapshotHttpAdapter(
        base_url=config.CONTROL_PLANE_INTERNAL_BASE_URL,
        service_token=config.PROJECTION_SYNC_SERVICE_TOKEN,
        timeout_ms=config.PROJECTION_SYNC_TIMEOUT_MS,
        http_client=_projection_http_client,
    )
    _projection_store = PostgresWorkspaceProjectionStore(
        SessionLocal, default_currency=config.DEFAULT_CURRENCY
    )
    _identity_admission_reader = WorkspaceAdmissionReader(SessionLocal)
    _identity_config = IdentityConfig(
        issuer=config.DELEGATED_JWT_ISSUER,
        audience=config.DELEGATED_JWT_AUDIENCE,
        max_clock_skew_seconds=config.DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS,
        max_token_bytes=config.DELEGATED_JWT_MAX_TOKEN_BYTES,
        max_token_ttl_seconds=config.DELEGATED_TOKEN_MAX_TTL_SECONDS,
        projection_repair_interval_seconds=config.PROJECTION_REPAIR_INTERVAL_SECONDS,
    )


# ---------------- platform-operator identity wiring ----------------
# A second, independent JwksKeyStore/verifier pair for `/api/platform/v1/*`
# only (see app/platform/platform_operator_identity.py). Wired only when
# `PLATFORM_OPERATOR_JWKS_URL` is configured, so deployments that never
# enable platform-operator routes are unaffected -- `require_platform_operator_jwt`
# fails closed with 503 when no verifier has been attached to `app.state`.
_platform_jwks_http_client: httpx.AsyncClient | None = None
_platform_jwks_store: JwksKeyStore | None = None
_platform_operator_verifier: PlatformOperatorJWTVerifier | None = None

if config.PLATFORM_OPERATOR_JWKS_URL:
    _platform_jwks_http_client = httpx.AsyncClient()
    _platform_jwks_store = JwksKeyStore(
        jwks_url=config.PLATFORM_OPERATOR_JWKS_URL,
        max_stale_seconds=config.PLATFORM_OPERATOR_JWKS_MAX_STALE_SECONDS,
        http_client=_platform_jwks_http_client,
    )
    _platform_operator_verifier = PlatformOperatorJWTVerifier(_platform_jwks_store)


# ---------------- BlobStore wiring (spec §7) ----------------
# Constructed unconditionally (unlike the delegated-identity block above):
# upload/download intents work the same way whether or not delegated
# identity is wired, since local/SQLite development also exercises this
# seam. `build_blob_store` is shared with `app.worker_runtime`, which needs
# the same Adapter selection logic in its own process. The FastAPI process
# only creates/reads intents -- the domain-projection admission re-check
# (spec §7.8 steps 1-3) happens in the Worker, which owns its own
# `WorkspaceAdmissionReader` (see `WorkerRuntime.__init__`).
_blob_store: BlobStore = build_blob_store()


def _warn_if_projection_sync_unconfigured(base_url: str, service_token: str) -> None:
    """Name an empty projection-sync setting at startup, as booleans only.

    Neither setting is checked anywhere else before the first on-demand sync,
    which then fails as `projection_sync_unavailable`; this line shows the
    gap in the deploy log instead. Values never reach the log.
    """
    if base_url and service_token:
        return
    logger.warning(
        "projection_sync_config base_url_set=%s token_set=%s",
        str(bool(base_url)).lower(),
        str(bool(service_token)).lower(),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    assert_runtime_role("marketai_domain_api")
    assert_local_identity_boundary()
    if config.DELEGATED_IDENTITY_ENABLED:
        # Before the JWKS load, which can fail startup on its own.
        _warn_if_projection_sync_unconfigured(
            config.CONTROL_PLANE_INTERNAL_BASE_URL, config.PROJECTION_SYNC_SERVICE_TOKEN
        )
    if _jwks_store is not None:
        await _jwks_store.initial_load(
            attempts=config.DELEGATED_JWKS_INITIAL_LOAD_ATTEMPTS,
            initial_backoff_seconds=config.DELEGATED_JWKS_INITIAL_LOAD_BACKOFF_SECONDS,
            max_backoff_seconds=config.DELEGATED_JWKS_INITIAL_LOAD_MAX_BACKOFF_SECONDS,
        )
        _jwks_store.start_background_refresh(config.DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS)
    if _platform_jwks_store is not None:
        await _platform_jwks_store.initial_load(
            attempts=config.DELEGATED_JWKS_INITIAL_LOAD_ATTEMPTS,
            initial_backoff_seconds=config.DELEGATED_JWKS_INITIAL_LOAD_BACKOFF_SECONDS,
            max_backoff_seconds=config.DELEGATED_JWKS_INITIAL_LOAD_MAX_BACKOFF_SECONDS,
        )
        _platform_jwks_store.start_background_refresh(
            config.PLATFORM_OPERATOR_JWKS_REFRESH_INTERVAL_SECONDS
        )
    try:
        yield
    finally:
        if _jwks_store is not None:
            await _jwks_store.stop_background_refresh()
        if _jwks_http_client is not None:
            await _jwks_http_client.aclose()
        if _projection_http_client is not None:
            await _projection_http_client.aclose()
        if _platform_jwks_store is not None:
            await _platform_jwks_store.stop_background_refresh()
        if _platform_jwks_http_client is not None:
            await _platform_jwks_http_client.aclose()


app = FastAPI(title="MarketAI Studio", lifespan=lifespan)
app.state.platform_operator_jwt_verifier = _platform_operator_verifier
# Unconditional (unlike `_identity_admission_reader` above, which only
# exists when DELEGATED_IDENTITY_ENABLED): `app.scale.platform_router`'s
# `_platform_context` needs a WorkspaceAdmissionReader regardless of
# whether delegated-user identity is wired, since platform-operator auth
# (Task 3) is an independent surface from DelegatedIdentityMiddleware.
app.state.admission_reader = WorkspaceAdmissionReader(SessionLocal)

if config.DELEGATED_IDENTITY_ENABLED:
    app.add_middleware(
        DelegatedIdentityMiddleware,
        jwks_store=_jwks_store,
        admission_reader=_identity_admission_reader,
        projection_syncer=WorkspaceProjectionSyncer(_snapshot_fetcher, _projection_store),
        config=_identity_config,
    )

# Internal entitlement inbox -- service-token auth, outside DelegatedIdentityMiddleware.
app.include_router(entitlement_internal_router)

app.include_router(meta_router)
app.include_router(meta_v1_router)
app.include_router(meta_platform_router)
app.include_router(meta_test_support_router)
app.include_router(meta_test_support_delegated_router)
app.include_router(experiment_router)
app.include_router(launch_router)
app.include_router(launch_v1_router)
app.include_router(localization_router)
# The legacy /api/library/* and /api/generation/* routers are gone (#27
# Task 9) -- all Library and Generation operations now live exclusively on
# /api/v1/library/* and /api/v1/generation/* (mounted below via
# library_v1_router / generation_v1_router). These two registrations stay:
# the exception types they map to Problem responses are raised by the
# shared domain code underneath the v1 routers too.
register_problem_handlers(app)
register_generation_problem_handlers(app)
register_localization_problem_handlers(app)


@app.get("/api/health/live")
def api_health_live():
    return {"ok": True}


@app.get("/api/health/ready")
async def api_health_ready():
    try:
        database_ready()
    except Exception as exc:
        raise HTTPException(503, f"数据库未就绪: {type(exc).__name__}") from exc

    if config.DELEGATED_IDENTITY_ENABLED:
        # spec §3.8: readiness proves both that the JWKS cache is loaded and
        # not hard-stale, and that `marketai_workspace_admission` is
        # callable -- a NULL row for a workspace that cannot exist proves the
        # SECURITY DEFINER function itself is reachable, without asserting
        # anything about a real Workspace.
        if _jwks_store is None or not _jwks_store.is_fresh():
            raise HTTPException(503, "JWKS 缓存不新鲜")
        try:
            await _identity_admission_reader.read("__marketai_readiness_probe__")
        except WorkspaceNotFoundError:
            pass
        except Exception as exc:
            raise HTTPException(503, f"workspace admission 不可用: {type(exc).__name__}") from exc

    result: dict[str, object] = {"ok": True, "persistence": "sql"}

    if engine.dialect.name == "postgresql":
        try:
            with engine.connect() as conn:
                stats = conn.execute(text("SELECT marketai_health_stats_v1()")).scalar()
            if stats:
                result.update(stats)
        except Exception:
            pass

    return result


# ---------------- identity probe (spec §3) ----------------
v1_router = APIRouter(prefix="/api/v1")


class IdentityProbeResponse(BaseModel):
    """The verified TenantContext, echoed back for the tracer round trip.

    Declared only so `generate_openapi.py` can extract a real response
    schema from `response_model` — the handler returns a `JSONResponse`
    directly, which FastAPI never re-validates against `response_model`, so
    this model documents the contract without changing runtime behavior."""

    model_config = {"extra": "forbid"}

    workspace_id: str
    role: Literal["owner", "member"]
    initiator_id: str
    authorization_epoch: int


@v1_router.get("/identity", operation_id="get_identity_probe", response_model=IdentityProbeResponse)
async def get_identity_probe(
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
) -> JSONResponse:
    return JSONResponse(
        {
            "workspace_id": tenant.workspace_id,
            "role": tenant.role,
            "initiator_id": tenant.initiator_id,
            "authorization_epoch": tenant.authorization_epoch,
        }
    )


# ---------------- Operation polling (spec §5.6) ----------------
#: Non-terminal states get a Retry-After hint; terminal ones do not (a
#: terminal Operation never changes again, so there is nothing to retry for).
_NON_TERMINAL_STATES = (
    "admitted",
    OperationState.QUEUED.value,
    OperationState.RUNNING.value,
    OperationState.UNCERTAIN.value,
)
_OPERATION_POLL_RETRY_AFTER_SECONDS = "5"


class OperationResultRefResponse(BaseModel):
    """Resource type + id (spec §5.1) -- never a URL the browser could not resolve."""

    model_config = {"extra": "forbid"}

    resource_type: str
    resource_id: str


class OperationDetailResponse(BaseModel):
    """Declared only so `generate_openapi.py` can extract a real response
    schema (see the identical note on `IdentityProbeResponse`); the handler
    returns a `JSONResponse` directly, so this documents the contract
    without changing runtime behavior."""

    model_config = {"extra": "forbid"}

    operation_id: str
    command_type: str
    state: Literal[
        "admitted",
        "queued",
        "running",
        "partially_succeeded",
        "succeeded",
        "failed",
        "cancelled",
        "uncertain",
    ]
    result: OperationResultRefResponse | None
    created_at: datetime
    updated_at: datetime


def _operation_to_response(operation) -> dict:
    ref = result_ref_of(operation)
    return {
        "operation_id": operation.id,
        "command_type": operation.command_type,
        "state": operation.state,
        "result": (
            {"resource_type": ref.resource_type, "resource_id": ref.resource_id}
            if ref is not None
            else None
        ),
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
    }


@v1_router.get(
    "/operations/{operation_id}",
    operation_id="get_operation",
    response_model=OperationDetailResponse,
    responses={304: {"description": "Not Modified -- body matches If-None-Match"}},
)
async def get_operation_detail(
    operation_id: str,
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    try:
        with tenant_session(tenant) as session:
            operation = fetch_operation(session, operation_id)
            body = _operation_to_response(operation)
    except OperationNotFoundError:
        return JSONResponse(
            status_code=404,
            media_type="application/problem+json",
            content={
                "type": "urn:marketai:error:operation-not-found",
                "title": "Operation not found",
                "status": 404,
                "code": "operation_not_found",
                "trace_id": tenant.trace_id,
                "retryable": False,
            },
        )

    body_bytes = json.dumps(body, sort_keys=True).encode()
    etag = f'"sha256:{hashlib.sha256(body_bytes).hexdigest()[:16]}"'

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    headers = {"ETag": etag}
    if body["state"] in _NON_TERMINAL_STATES:
        headers["Retry-After"] = _OPERATION_POLL_RETRY_AFTER_SECONDS

    return JSONResponse(content=body, status_code=200, headers=headers)


# ---------------- Upload/download intents (spec §7.5-§7.12) ----------------


def _aware(value: datetime) -> datetime:
    """SQLite may return a naive value even for a ``DateTime(timezone=True)``
    column; normalize before comparing against ``utcnow()`` (tz-aware).
    Mirrors ``app.platform.jobs._normalize_sqlite_datetime``."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _problem(
    status_code: int,
    code: str,
    title: str,
    tenant: TenantContext,
    *,
    retryable: bool,
    detail: str | None = None,
    params: dict | None = None,
) -> JSONResponse:
    body = {
        "type": f"urn:marketai:error:{code.replace('_', '-')}",
        "title": title,
        "status": status_code,
        "code": code,
        "trace_id": tenant.trace_id,
        "retryable": retryable,
    }
    if detail:
        body["detail"] = detail
    if params:
        body["params"] = params
    return JSONResponse(status_code=status_code, media_type="application/problem+json", content=body)


class CreateUploadIntentRequest(BaseModel):
    model_config = {"extra": "forbid"}

    purpose: str
    owner_kind: str
    owner_id: str | None = None
    filename: str
    content_type: str
    content_length_min: int
    content_length_max: int
    checksum_sha256_b64: str


class UploadGrantResponse(BaseModel):
    model_config = {"extra": "forbid"}

    kind: Literal["presigned_post", "signed_put"]
    url: str
    fields: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    expires_at: datetime


class UploadIntentResponse(BaseModel):
    """Declared only so `generate_openapi.py` can extract a real response
    schema (see the identical note on `IdentityProbeResponse`); the handler
    returns a `JSONResponse` directly."""

    model_config = {"extra": "forbid"}

    intent_id: str
    state: Literal["pending", "verifying", "finalized", "rejected", "expired"]
    purpose: str
    expires_at: datetime
    upload_grant: UploadGrantResponse


def _upload_grant_to_dict(grant) -> dict:
    payload = {"kind": grant.kind, "url": grant.url, "expires_at": grant.expires_at.isoformat()}
    if grant.kind == "presigned_post":
        payload["fields"] = grant.fields
    else:
        payload["headers"] = grant.headers
    return payload


@v1_router.post(
    "/upload-intents",
    operation_id="create_upload_intent",
    response_model=UploadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_upload_intent_endpoint(
    body: CreateUploadIntentRequest,
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if not idempotency_key:
        return _problem(
            400, "idempotency_key_required", "Idempotency-Key header is required", tenant, retryable=False
        )

    policy = PURPOSE_POLICIES.get(body.purpose)
    if policy is None:
        return _problem(400, "unknown_purpose", f"Unknown purpose: {body.purpose!r}", tenant, retryable=False)
    if body.content_type not in policy.allowed_mimes:
        return _problem(
            400,
            "content_type_not_allowed",
            f"{body.content_type!r} is not allowed for purpose {body.purpose!r}",
            tenant,
            retryable=False,
        )
    if body.content_length_min < 0 or body.content_length_max < body.content_length_min:
        return _problem(400, "content_length_invalid", "content_length_min/max is invalid", tenant, retryable=False)
    if body.content_length_max > policy.max_size_bytes:
        return _problem(
            400,
            "content_length_exceeds_policy",
            f"content_length_max exceeds the {policy.max_size_bytes} byte limit for purpose {body.purpose!r}",
            tenant,
            retryable=False,
            params={"limit_mb": max(1, round(policy.max_size_bytes / (1024 * 1024)))},
        )

    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_upload_intent",
        request.url.path,
        {},
        body.model_dump(mode="json"),
        {},
    )

    def _create(session):
        intent_id = new_id("ui")
        staging_key = build_staging_key(tenant.workspace_id, body.purpose)
        record = create_upload_intent_record(
            session,
            intent_id=intent_id,
            staging_key=staging_key,
            owner_kind=body.owner_kind,
            owner_id=body.owner_id,
            purpose=body.purpose,
            filename_original=body.filename,
            content_type=body.content_type,
            content_length_min=body.content_length_min,
            content_length_max=body.content_length_max,
            checksum_sha256_b64=body.checksum_sha256_b64,
            ttl_seconds=policy.default_ttl_seconds,
        )
        return ("upload_intent", intent_id, record)

    def _replay(_resource_type, resource_id, session):
        return get_upload_intent(session, resource_id)

    try:
        with tenant_session(tenant) as session:
            record = create_or_replay_intent(
                session,
                idempotency_key=idempotency_key,
                command_type="create_upload_intent",
                fingerprint_hash=fingerprint_hash,
                create_fn=_create,
                replay_fn=_replay,
            )
    except IdempotencyKeyReusedError:
        return _problem(
            409,
            "idempotency_key_reused",
            "Idempotency-Key was already used for a different request",
            tenant,
            retryable=False,
        )

    now = utcnow()
    expires_at = _aware(record.expires_at)
    if expires_at < now:
        with tenant_session(tenant) as session:
            expire_upload_intent(session, record.id)
        return _problem(410, "upload_intent_expired", "The upload intent has expired", tenant, retryable=False)

    # Spec §7.11: idempotent replay never stores/returns the original signed
    # URL -- every call (first or replay) re-issues a fresh capability
    # against the persisted staging_key, capped at the intent's expires_at.
    remaining_seconds = max(1, int((expires_at - now).total_seconds()))
    spec = UploadSpec(
        staging_key=record.staging_key,
        content_type=record.content_type,
        content_length_min=record.content_length_min,
        content_length_max=record.content_length_max,
        checksum_sha256_b64=record.checksum_sha256_b64,
        expires_in_seconds=min(policy.default_ttl_seconds, remaining_seconds),
    )
    grant = await _blob_store.issue_upload(spec)

    return JSONResponse(
        status_code=201,
        content={
            "intent_id": record.id,
            "state": record.state,
            "purpose": record.purpose,
            "expires_at": expires_at.isoformat(),
            "upload_grant": _upload_grant_to_dict(grant),
        },
    )


class FinalizeUploadResponse(BaseModel):
    """See the note on `UploadIntentResponse` -- the handler returns a
    `JSONResponse` directly."""

    model_config = {"extra": "forbid"}

    operation_id: str
    state: Literal[
        "queued", "running", "partially_succeeded", "succeeded", "failed", "cancelled", "uncertain"
    ]


@v1_router.post(
    "/upload-intents/{intent_id}/finalize",
    operation_id="finalize_upload",
    response_model=FinalizeUploadResponse,
    status_code=202,
    openapi_extra={"x-marketai-idempotent": True},
)
async def finalize_upload_endpoint(
    intent_id: str,
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if not idempotency_key:
        return _problem(
            400, "idempotency_key_required", "Idempotency-Key header is required", tenant, retryable=False
        )

    fingerprint_hash = compute_fingerprint(
        "POST", "finalize_upload", request.url.path, {}, None, {}
    )

    try:
        with tenant_session(tenant) as session:
            # Spec §5.4: idempotent replay returns the original outcome --
            # it never re-validates against state the resource moved to
            # *after* the original call already succeeded. So the intent's
            # state/actor gate below applies only when this Idempotency-Key
            # has no existing reservation yet (a genuinely new attempt);
            # an existing reservation (pending -- a concurrent request is
            # still resolving it -- or committed -- a true replay) skips
            # straight to create_or_replay_operation, which handles both
            # cases itself (waits on pending, returns the committed result).
            # A different actor/command/fingerprint reusing this exact key
            # is still caught there too, via its own binding check.
            existing = get_idempotency_record(
                session, hash_idempotency_key(idempotency_key), tenant.workspace_id
            )

            if existing is None:
                intent = get_upload_intent(session, intent_id)
                now = utcnow()

                if intent.state == "expired" or (
                    intent.state == "pending" and _aware(intent.expires_at) < now
                ):
                    expire_upload_intent(session, intent.id)
                    return _problem(
                        410, "upload_intent_expired", "The upload intent has expired", tenant, retryable=False
                    )
                if intent.state == "rejected":
                    return _problem(
                        409,
                        "upload_intent_rejected",
                        f"The upload intent was rejected: {intent.rejection_code}",
                        tenant,
                        retryable=False,
                    )
                if (
                    intent.initiator_id
                    and tenant.initiator_id
                    and intent.initiator_id != tenant.initiator_id
                ):
                    return _problem(
                        403,
                        "finalize_forbidden",
                        "Only the actor that created the upload intent may finalize it",
                        tenant,
                        retryable=False,
                    )

            operation = create_or_replay_operation(
                session,
                idempotency_key=idempotency_key,
                command_type="finalize_upload",
                fingerprint_hash=fingerprint_hash,
                actor=tenant.initiator_id or tenant.executor,
            )
            jobs_platform.enqueue_in_session(
                session,
                context=tenant,
                job_type="verify_upload_intent",
                payload={"intent_id": intent_id, "operation_id": operation.id},
                idempotency_key=f"verify_upload_intent:{operation.id}",
                safe_to_retry_after_crash=True,
                resource_type="upload_intent",
                resource_id=intent_id,
            )
    except UploadIntentNotFoundError:
        return _problem(404, "upload_intent_not_found", "Upload intent not found", tenant, retryable=False)
    except IdempotencyKeyReusedError:
        return _problem(
            409,
            "idempotency_key_reused",
            "Idempotency-Key was already used for a different request",
            tenant,
            retryable=False,
        )

    return JSONResponse(
        status_code=202,
        content={"operation_id": operation.id, "state": operation.state},
    )


class CreateDownloadIntentRequest(BaseModel):
    model_config = {"extra": "forbid"}

    owner_kind: str
    owner_id: str
    committed_blob_ref_key: str
    committed_blob_ref_version: str
    filename: str


class DownloadGrantResponse(BaseModel):
    model_config = {"extra": "forbid"}

    url: str
    expires_at: datetime
    content_disposition: str


class DownloadIntentResponse(BaseModel):
    """See the note on `UploadIntentResponse` -- the handler returns a
    `JSONResponse` directly."""

    model_config = {"extra": "forbid"}

    intent_id: str
    expires_at: datetime
    download_grant: DownloadGrantResponse


def _committed_blob_ref_workspace(key: str) -> str | None:
    """The `{workspace_id}` segment of a `committed/{workspace_id}/...` key,
    or None if the key does not have that shape (spec §7.13)."""
    parts = key.split("/")
    if len(parts) < 2 or parts[0] != "committed":
        return None
    return parts[1]


@v1_router.post(
    "/download-intents",
    operation_id="create_download_intent",
    response_model=DownloadIntentResponse,
    status_code=201,
    openapi_extra={"x-marketai-idempotent": True},
)
async def create_download_intent_endpoint(
    body: CreateDownloadIntentRequest,
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if not idempotency_key:
        return _problem(
            400, "idempotency_key_required", "Idempotency-Key header is required", tenant, retryable=False
        )

    # The committed key embeds its owning Workspace (spec §7.13); a caller
    # asking for a download grant against another Workspace's object is
    # rejected here rather than trusted from the request body.
    if _committed_blob_ref_workspace(body.committed_blob_ref_key) != tenant.workspace_id:
        return _problem(
            403,
            "committed_blob_ref_not_in_workspace",
            "committed_blob_ref_key does not belong to this Workspace",
            tenant,
            retryable=False,
        )

    fingerprint_hash = compute_fingerprint(
        "POST",
        "create_download_intent",
        request.url.path,
        {},
        body.model_dump(mode="json"),
        {},
    )

    def _create(session):
        intent_id = new_id("di")
        record = create_download_intent_record(
            session,
            intent_id=intent_id,
            owner_kind=body.owner_kind,
            owner_id=body.owner_id,
            committed_blob_ref_key=body.committed_blob_ref_key,
            committed_blob_ref_version=body.committed_blob_ref_version,
        )
        return ("download_intent", intent_id, record)

    def _replay(_resource_type, resource_id, session):
        return get_download_intent(session, resource_id)

    try:
        with tenant_session(tenant) as session:
            record = create_or_replay_intent(
                session,
                idempotency_key=idempotency_key,
                command_type="create_download_intent",
                fingerprint_hash=fingerprint_hash,
                create_fn=_create,
                replay_fn=_replay,
            )
    except IdempotencyKeyReusedError:
        return _problem(
            409,
            "idempotency_key_reused",
            "Idempotency-Key was already used for a different request",
            tenant,
            retryable=False,
        )

    now = utcnow()
    expires_at = _aware(record.expires_at)
    if expires_at < now:
        return _problem(410, "download_intent_expired", "The download intent has expired", tenant, retryable=False)

    remaining_seconds = max(1, int((expires_at - now).total_seconds()))

    # CommittedBlobRef's size/checksum/content-type fields are not persisted
    # on DownloadIntentRecord (spec §7.12's dataclass carries only the key +
    # version) -- the adapters' issue_download only reads committed_key and
    # storage_version_id, so the unused fields are filled with inert values.
    grant = await _blob_store.issue_download(
        CommittedBlobRef(
            committed_key=record.committed_blob_ref_key,
            storage_version_id=record.committed_blob_ref_version,
            size_bytes=0,
            checksum_sha256_b64="",
            content_type_verified="",
        ),
        DownloadSpec(
            filename=body.filename,
            expires_in_seconds=min(DEFAULT_DOWNLOAD_TTL_SECONDS, remaining_seconds),
        ),
    )

    return JSONResponse(
        status_code=201,
        content={
            "intent_id": record.id,
            "expires_at": expires_at.isoformat(),
            "download_grant": {
                "url": grant.url,
                "expires_at": grant.expires_at.isoformat(),
                "content_disposition": grant.content_disposition,
            },
        },
    )


# ---------------- audit (Task 16, spec §8.8) ----------------
# `operation`/`resource` map onto AuditEventRecord's `resource_type` /
# `resource_id` -- the model has no separate "operation" column; every
# domain command's audit trail is recorded against the resource it acted on.


def _audit_event_to_dict(row: AuditEventRecord) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "initiator_id": row.initiator_id,
        "initiator_role": row.initiator_role,
        "executor": row.executor,
        "authorization_epoch": row.authorization_epoch,
        "meta_mode": row.meta_mode,
        "event_type": row.event_type,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "details": row.details_json,
        "created_at": row.created_at.isoformat(),
    }


@v1_router.get("/audit", operation_id="list_audit_events")
def list_audit_events(
    operation: str | None = Query(default=None, description="Filters by resource_type"),
    resource: str | None = Query(default=None, description="Filters by resource_id"),
    initiator: str | None = Query(default=None, description="Filters by initiator_id"),
    executor: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    meta_mode: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    tenant: TenantContext = Depends(request_tenant_context),
) -> dict:
    filters_key = "|".join([
        operation or "", resource or "", initiator or "", executor or "",
        event_type or "", meta_mode or "", since.isoformat() if since else "",
        until.isoformat() if until else "",
    ])
    with tenant_session(tenant) as session:
        stmt = select(AuditEventRecord).where(AuditEventRecord.workspace_id == tenant.workspace_id)
        if operation:
            stmt = stmt.where(AuditEventRecord.resource_type == operation)
        if resource:
            stmt = stmt.where(AuditEventRecord.resource_id == resource)
        if initiator:
            stmt = stmt.where(AuditEventRecord.initiator_id == initiator)
        if executor:
            stmt = stmt.where(AuditEventRecord.executor == executor)
        if event_type:
            stmt = stmt.where(AuditEventRecord.event_type == event_type)
        if meta_mode:
            stmt = stmt.where(AuditEventRecord.meta_mode == meta_mode)
        if since:
            stmt = stmt.where(AuditEventRecord.created_at >= since)
        if until:
            stmt = stmt.where(AuditEventRecord.created_at <= until)
        rows = session.scalars(stmt.order_by(AuditEventRecord.created_at.desc())).all()
        items = [_audit_event_to_dict(row) for row in rows]

    start = 0
    if cursor:
        try:
            start = decode_offset_cursor(cursor, tenant.workspace_id, filters_key)
        except InvalidCursorError as exc:
            raise HTTPException(400, {"code": "invalid_cursor", "message": str(exc)}) from exc
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        next_cursor = encode_cursor(tenant.workspace_id, filters_key, (start + limit,))
    return {"items": page, "next_cursor": next_cursor}


from .library.v1_router import router as library_v1_router

v1_router.include_router(library_v1_router)

from .generation.v1_router import register_v1_problem_handlers as register_generation_v1_problem_handlers
from .generation.v1_router import router as generation_v1_router
from .experiments.v1_router import router as experiments_v1_router

v1_router.include_router(generation_v1_router)
v1_router.include_router(experiments_v1_router)
register_generation_v1_problem_handlers(app)

from .localization.v1_router import router as localization_v1_router

v1_router.include_router(localization_v1_router)

from .products.v1_router import router as products_v1_router

v1_router.include_router(products_v1_router)

from .plans.v1_router import router as test_plans_v1_router

v1_router.include_router(test_plans_v1_router)

from .marketing.api import router as marketing_router, register_handlers as register_marketing_handlers
v1_router.include_router(marketing_router)
register_marketing_handlers(app)

from .learnings.v1_router import router as learnings_v1_router

v1_router.include_router(learnings_v1_router)

from .external_creatives.v1_router import router as external_creatives_v1_router

v1_router.include_router(external_creatives_v1_router)

from .competitors.v1_router import router as competitors_v1_router
from .competitors.scan_router import products_router as competitor_product_settings_v1_router
from .competitors.scan_router import router as competitor_scans_v1_router

v1_router.include_router(competitors_v1_router)
v1_router.include_router(competitor_scans_v1_router)
v1_router.include_router(competitor_product_settings_v1_router)
from .scale.v1_router import register_scale_v1_problem_handlers
from .scale.v1_router import router as scale_v1_router
from .scale.platform_router import platform_scale_root

v1_router.include_router(scale_v1_router)
register_scale_v1_problem_handlers(app)
app.include_router(platform_scale_root)

app.include_router(v1_router)
# Authenticated entitlement routes -- inside DelegatedIdentityMiddleware scope.
app.include_router(v1_entitlement_router)


# ---------------- providers ----------------
@app.get("/api/providers")
def api_providers(_context: TenantContext = Depends(request_tenant_context)):
    return providers_info()


# ---------------- products ----------------
@app.get("/api/products")
def api_products(context: TenantContext = Depends(request_tenant_context)):
    return [p.model_dump() for p in store.list_products(context)]


@app.post("/api/products")
def api_product_create(body: ProductCreate, context: TenantContext = Depends(request_tenant_context)):
    p = Product(**body.model_dump())
    store.save_product(context, p)
    return p.model_dump()


@app.get("/api/products/{pid}")
def api_product_get(pid: str, context: TenantContext = Depends(request_tenant_context)):
    p = store.get_product(context, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    return p.model_dump()


@app.put("/api/products/{pid}")
def api_product_update(
    pid: str,
    body: ProductCreate,
    context: TenantContext = Depends(request_tenant_context),
):
    p = store.get_product(context, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    p = p.model_copy(update=body.model_dump())
    store.save_product(context, p)
    return p.model_dump()


@app.post("/api/products/{pid}/refs")
async def api_upload_ref(
    pid: str,
    files: list[UploadFile] = File(...),
    context: TenantContext = Depends(request_tenant_context),
):
    p = store.get_product(context, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    saved = []
    for f in files[:8]:
        raw = await f.read()
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            raise HTTPException(400, f"无法识别的图片: {f.filename}")
        name = f"ref_{len(p.ref_images) + len(saved) + 1:02d}.jpg"
        img.thumbnail((2048, 2048))
        img.save(store.refs_dir(pid) / name, "JPEG", quality=92)
        saved.append(name)
    p.ref_images += saved
    store.save_product(context, p)
    return p.model_dump()


@app.delete("/api/products/{pid}/refs/{name}")
def api_delete_ref(
    pid: str, name: str, context: TenantContext = Depends(request_tenant_context)
):
    p = store.get_product(context, pid)
    if not p or name not in p.ref_images:
        raise HTTPException(404, "参考图不存在")
    (store.refs_dir(pid) / name).unlink(missing_ok=True)
    p.ref_images = [x for x in p.ref_images if x != name]
    store.save_product(context, p)
    return p.model_dump()


@app.get("/files/refs/{pid}/{name}")
def file_ref(pid: str, name: str, context: TenantContext = Depends(request_tenant_context)):
    if not store.get_product(context, pid):
        raise HTTPException(404)
    path = store.refs_dir(pid) / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ---------------- angles ----------------
@app.post("/api/products/{pid}/angles/generate")
async def api_angles_generate(pid: str, context: TenantContext = Depends(request_tenant_context)):
    p = store.get_product(context, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    angles = await generate_dimensions(p)
    store.save_angles(context, angles)
    return angles.model_dump()


@app.get("/api/products/{pid}/angles")
def api_angles_get(pid: str, context: TenantContext = Depends(request_tenant_context)):
    a = store.get_angles(context, pid)
    return a.model_dump() if a else None


@app.put("/api/products/{pid}/angles")
def api_angles_put(
    pid: str, body: AngleSchema, context: TenantContext = Depends(request_tenant_context)
):
    body.product_id = pid
    body.source = "manual" if body.source == "manual" else body.source
    store.save_angles(context, body)
    return body.model_dump()


# ---------------- plan（组合） ----------------
@app.post("/api/products/{pid}/plan")
def api_plan(
    pid: str, body: PlanRequest, context: TenantContext = Depends(request_tenant_context)
):
    angles = store.get_angles(context, pid)
    if not angles:
        raise HTTPException(400, "请先生成/编辑角度维度")
    combos, total_full = build_combos(angles, body)
    if not combos:
        raise HTTPException(400, "请至少为每个参与维度勾选一个取值")
    plan = Plan(product_id=pid, mode=body.mode, cap=body.cap,
                total_full=total_full, combos=combos)
    store.save_plan(context, plan)
    return plan.model_dump()


@app.get("/api/products/{pid}/plan")
def api_plan_get(pid: str, context: TenantContext = Depends(request_tenant_context)):
    plan = store.get_plan(context, pid)
    return plan.model_dump() if plan else None


# ---------------- runs ----------------
@app.post("/api/runs")
async def api_run_start(body: GenRequest, context: TenantContext = Depends(request_tenant_context)):
    p = store.get_product(context, body.product_id)
    if not p:
        raise HTTPException(404, "产品不存在")
    angles = store.get_angles(context, body.product_id)
    plan = store.get_plan(context, body.product_id)
    if not angles or not plan:
        raise HTTPException(400, "请先完成角度与组合两步")
    try:
        provider = get_provider(body.provider)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not provider.available():
        raise HTTPException(400, f"{provider.label} 未配置 API key（.env）")
    try:
        run = manager.start(context, p, angles, plan, body)
    except store.CostLimitExceeded as exc:
        raise HTTPException(409, {
            "code": "cost_limit_exceeded",
            "scope": exc.scope,
            "attempted": str(exc.attempted),
            "limit": str(exc.limit),
            "currency": exc.currency,
        }) from exc
    return run.model_dump()


@app.get("/api/runs/{run_id}")
def api_run_get(
    run_id: str, pid: str, context: TenantContext = Depends(request_tenant_context)
):
    run = store.get_run(context, pid, run_id)
    if not run:
        raise HTTPException(404, "run 不存在")
    return run.model_dump()


@app.post("/api/runs/{run_id}/cancel")
def api_run_cancel(run_id: str, context: TenantContext = Depends(request_tenant_context)):
    if not manager.cancel(context, run_id):
        raise HTTPException(400, "run 不在进行中")
    return {"ok": True}


@app.get("/api/products/{pid}/runs")
def api_runs_list(pid: str, context: TenantContext = Depends(request_tenant_context)):
    runs = store.list_runs(context, pid)
    return [
        run.model_dump()
        for run in sorted(runs, key=lambda row: row.created_at, reverse=True)
    ]


# ---------------- gallery ----------------
@app.get("/api/products/{pid}/assets")
def api_assets(pid: str, context: TenantContext = Depends(request_tenant_context)):
    """聚合所有 run 的素材 + 评分，给画廊用。"""
    quality = store.get_quality(context, pid)
    verdicts = store.get_verdicts(context, pid)
    _back = {"approved": 1, "needs_changes": 2, "rejected": -1}
    out = []
    for run in api_runs_list_objects(context, pid):
        for a in run.assets:
            d = a.model_dump()
            d["quality"] = quality.get(a.id)                    # 质量审核（人）
            d["rating"] = _back.get(d["quality"], 0)            # 兼容旧字段
            vd = verdicts.get(a.id) or {}
            d["verdict"] = vd.get("verdict")                    # 投放判定（数据）
            d["verdict_context"] = vd.get("context") or {}
            d["url"] = f"/files/runs/{pid}/{run.id}/{a.filename}" if a.status == "ok" else None
            out.append(d)
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def api_runs_list_objects(context: TenantContext, pid: str):
    return store.list_runs(context, pid)


QUALITY_SET = {"approved", "needs_changes", "rejected"}
VERDICT_SET = {"win", "learn", "lose"}


@app.post("/api/products/{pid}/assets/{asset_id}/quality")
def api_quality(
    pid: str,
    asset_id: str,
    body: dict,
    context: TenantContext = Depends(request_tenant_context),
):
    """质量审核（人判"能不能用"）。quality 传 null/"" = 清除。"""
    q = body.get("quality") or None
    if q is not None and q not in QUALITY_SET:
        raise HTTPException(400, f"quality 只能是 {sorted(QUALITY_SET)} 或 null")
    try:
        store.set_quality(context, pid, asset_id, q)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "quality": q}


@app.post("/api/products/{pid}/assets/{asset_id}/verdict")
def api_verdict(
    pid: str,
    asset_id: str,
    body: dict,
    _context: TenantContext = Depends(request_tenant_context),
):
    """Deprecated: production verdicts may only come from frozen experiment snapshots."""
    del pid, asset_id, body
    raise HTTPException(409, {
        "code": "direct_verdict_disabled",
        "message": "禁止直接改投放判定；请创建并冻结 Experiment，再调用 /api/experiments/{id}/judge",
    })


@app.post("/api/products/{pid}/assets/{asset_id}/rate")
def api_rate(
    pid: str,
    asset_id: str,
    body: dict,
    context: TenantContext = Depends(request_tenant_context),
):
    """【兼容旧前端】整数评分 → 质量审核：1=approved 2=needs_changes -1=rejected 0=清除。"""
    rating = int(body.get("rating", 0))
    if rating not in (-1, 0, 1, 2):
        raise HTTPException(400, "rating 只能是 -1/0/1/2")
    try:
        store.set_rating(context, pid, asset_id, rating)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "rating": rating}


@app.get("/files/runs/{pid}/{run_id}/{name}")
def file_run_image(
    pid: str,
    run_id: str,
    name: str,
    context: TenantContext = Depends(request_tenant_context),
):
    if not store.get_run(context, pid, run_id):
        raise HTTPException(404)
    path = store.product_dir(pid) / "runs" / run_id / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ---------------- export ----------------
@app.get("/api/products/{pid}/export")
def api_export(
    pid: str,
    scope: str = "approved",
    context: TenantContext = Depends(request_tenant_context),
):
    """导出素材包（zip + manifest）。scope：
    approved = 质量审核 ✅（默认，可进投放）；winner = 投放判定 🏆（数据验证，本地化/放量对象）；all = 全部成功。
    manifest 带 quality + verdict 双字段——拿到包的人不用问就知道每张图两套状态各是什么。"""
    p = store.get_product(context, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    quality = store.get_quality(context, pid)
    verdicts = store.get_verdicts(context, pid)

    def _wanted(aid: str) -> bool:
        if scope == "all":
            return True
        if scope == "winner":
            return (verdicts.get(aid) or {}).get("verdict") == "win"
        return quality.get(aid) == "approved"

    approved = []
    for run in api_runs_list_objects(context, pid):
        for a in run.assets:
            if a.status == "ok" and _wanted(a.id):
                approved.append((run.id, a))
    if not approved:
        raise HTTPException(400, {
            "winner": "还没有投放判定 🏆 Winner——先跑投放实验，由数据选出赢家",
            "approved": "还没有 ✅ Approved 素材——先在画廊完成质量审核",
        }.get(scope, "该范围内没有素材"))

    buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for run_id, a in approved:
            src = store.product_dir(pid) / "runs" / run_id / a.filename
            if not src.exists():
                continue
            tag_str = "_".join(f"{k}-{v}" for k, v in a.tags.items())
            arcname = f"{p.name}_{a.id}_{a.combo_id}_{tag_str}_{a.provider}.png".replace("/", "-").replace(" ", "")
            z.write(src, arcname)
            manifest.append({"file": arcname, "combo_id": a.combo_id, "combo_code": a.combo_code,
                             "tags": a.tags, "provider": a.provider, "model": a.model,
                             "quality": quality.get(a.id),
                             "verdict": (verdicts.get(a.id) or {}).get("verdict"),
                             "verdict_context": (verdicts.get(a.id) or {}).get("context") or {},
                             "prompt": a.prompt, "asset_id": a.id})
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    buf.seek(0)
    record_event(
        context,
        event_type="product.exported",
        resource_type="product",
        resource_id=pid,
        details={"scope": scope, "asset_count": len(manifest)},
    )
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{pid}_{scope}.zip"'})


# ---------------- 静态页面 ----------------
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
