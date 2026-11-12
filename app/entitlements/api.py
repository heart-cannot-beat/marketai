"""Entitlement API routes: internal inbox + authenticated v1 endpoints.

Internal:
  POST /api/internal/domain/workspaces/{workspace_id}/entitlement-inbox
  Service-token auth, outside DelegatedIdentityMiddleware.

Authenticated v1 (inside DelegatedIdentityMiddleware):
  POST   /api/v1/credentials
  POST   /api/v1/credentials/{credential_key}/rotate
  POST   /api/v1/credentials/{credential_version_id}/revoke
  GET    /api/v1/credentials
  PUT    /api/v1/credentials/{credential_version_id}/bindings/{scope}
  GET    /api/v1/spend-guardrail
  POST   /api/v1/spend-guardrail/preview
  POST   /api/v1/spend-guardrail/confirm
  GET    /api/v1/usage
  GET    /api/v1/cost-summary
"""
from __future__ import annotations

import base64
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_
from sqlalchemy import func as sa_func

from app import config
from app.adapters.persistence.database import tenant_session
from app.entitlements.cost_models import ProviderCostLedgerEntryRecord
from app.entitlements.types import InboxOutcome
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

# ---------------------------------------------------------------------------
# Mutation policy: determines which operations are allowed under which
# entitlement lease conditions.
# ---------------------------------------------------------------------------

MUTATION_POLICY = {
    "create_credential": "LEASE_REQUIRED",
    "rotate_credential": "LEASE_REQUIRED",
    "bind_credential": "LEASE_REQUIRED",
    "raise_guardrail": "LEASE_REQUIRED",
    "read": "READ_ONLY",
    "revoke_credential": "RECOVERY_ALLOWED",
    "lower_guardrail": "RECOVERY_ALLOWED",
}

# ===================================================================
# Internal router -- service-token auth, outside identity middleware
# ===================================================================

internal_router = APIRouter()


def _verify_service_token(request: Request) -> None:
    """Validate the service Bearer token against the configured secret.

    Uses ``hmac.compare_digest`` for constant-time comparison.
    """
    token = config.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN
    if not token:
        raise HTTPException(
            status_code=503,
            detail="entitlement inbox is not configured",
        )
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="missing Bearer token")
    provided = auth[len("Bearer "):]
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=403, detail="invalid service token")


@internal_router.post(
    "/api/internal/domain/workspaces/{workspace_id}/entitlement-inbox",
    operation_id="post_entitlement_inbox",
)
async def post_entitlement_inbox(
    workspace_id: str,
    request: Request,
) -> Response:
    """Receive a signed entitlement envelope from the control plane."""
    _verify_service_token(request)

    # Size gate
    max_bytes = config.ENTITLEMENT_INBOX_MAX_BODY_BYTES
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail="body exceeds 64KB limit")

    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="body exceeds 64KB limit")

    # Lazy import: the projection module may not be available in all
    # deployment configurations; the import is deferred so the router
    # itself can always be registered.
    from app.entitlements.projection import EntitlementProjectionModule
    from app.adapters.persistence.database import SessionLocal

    verification_key_ring = config.parse_entitlement_key_ring()

    module = EntitlementProjectionModule(
        session_factory=SessionLocal,
        verification_key_ring=verification_key_ring,
    )

    try:
        outcome = await module.ingest(
            raw,
            delivery_kind="push",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({"outcome": outcome.value})


# ===================================================================
# Authenticated v1 router -- inside DelegatedIdentityMiddleware
# ===================================================================

v1_entitlement_router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateCredentialRequest(BaseModel):
    model_config = {"extra": "forbid"}
    credential_key: str
    provider: str
    plaintext_b64: str  # base64-encoded
    display_label: str | None = None


class RotateCredentialRequest(BaseModel):
    model_config = {"extra": "forbid"}
    new_plaintext_b64: str


class RevokeCredentialRequest(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str


class BindCredentialRequest(BaseModel):
    model_config = {"extra": "forbid"}
    provider: str


class GuardrailPreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    per_operation_limit: str
    rolling_24h_limit: str
    period_limit: str
    outstanding_exposure_limit: str
    expected_revision: int
    mfa_evidence_ref: str | None = None


class GuardrailConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}
    preview_token: str
    expected_revision: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_owner(tenant: TenantContext) -> None:
    if tenant.role != "owner" and tenant.initiator_role != "owner":
        raise HTTPException(
            status_code=403,
            detail={"code": "owner_role_required", "message": "owner role required"},
        )


def _get_protector():
    from app.entitlements.credential_protector import LocalKmsAdapter

    secret = config.MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET
    if not secret:
        secret = "dev-only-insecure-placeholder-key!!"
    return LocalKmsAdapter(secret.encode() if isinstance(secret, str) else secret)


def _credential_metadata_to_dict(
    meta,
    *,
    bindings: list[dict[str, str]] | None = None,
    last_check: dict | None = None,
) -> dict:
    """One credential version as the Settings page reads it.

    ``bindings`` lists the ``(binding_scope, provider)`` pairs pointing at
    this version; ``last_check`` is the newest key check of a market-data
    key (``app.market_data.credentials``), ``None`` for every other provider.
    """
    return {
        "credential_version_id": meta.credential_version_id,
        "workspace_id": meta.workspace_id,
        "credential_key": meta.credential_key,
        "version_no": meta.version_no,
        "provider": meta.provider,
        "display_label": meta.display_label,
        "state": meta.state,
        "value_sha256": meta.value_sha256,
        "created_by": meta.created_by,
        "created_at": meta.created_at.isoformat(),
        "rotated_at": meta.rotated_at.isoformat() if meta.rotated_at else None,
        "revoked_at": meta.revoked_at.isoformat() if meta.revoked_at else None,
        "bindings": bindings or [],
        "last_check": last_check,
    }


def _record_stored_check(session, tenant: TenantContext, meta, check) -> dict | None:
    """Evidence of the check that let ``meta``'s version through, read back."""
    if check is None:
        return None
    from app.market_data import credentials as market_data

    market_data.record_check(
        session, provider=meta.provider, check=check,
        credential_version_id=meta.credential_version_id,
    )
    session.flush()
    return market_data.latest_checks(
        session, tenant, [meta.credential_version_id],
    ).get(meta.credential_version_id)


async def _check_market_data_key(
    tenant: TenantContext, provider: str, credential_key: str, plaintext: bytes,
):
    """Check a market-data key before it is stored; refuse it if it fails.

    Runs outside any transaction (it is a network call). A refused key
    leaves its evidence on the lineage it was meant for and nothing else.
    """
    from app.market_data import credentials as market_data

    check = await market_data.check_key(provider, plaintext)
    refused = market_data.refusal(check)
    if refused is not None:
        with tenant_session(tenant) as session:
            market_data.record_check(
                session, provider=provider, check=check, credential_key=credential_key,
            )
        raise _coded(refused.status, refused.code, str(refused))
    return check


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    """An HTTPException the app-wide Problem Details handler renders with ``code``.

    Every credential command reaches FastAPI through the web BFF proxy, which
    only forwards the statuses the operation declares (``200`` plus the shared
    ``401/403/409/422/500``) or an undeclared status that carries a
    well-formed ``application/problem+json`` body -- a bare ``detail`` string
    would surface in the browser as ``invalid_domain_response``.
    """
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _admin_problem(status_code: int, exc: ValueError) -> HTTPException:
    """A refused guardrail command, rendered by its ``GuardrailAdminError`` code."""
    return HTTPException(status_code=status_code, detail={
        "code": getattr(exc, "code", None) or "request_invalid",
        "message": str(exc),
        "params": getattr(exc, "params", {}),
    })


# ---------------------------------------------------------------------------
# Credential endpoints
# ---------------------------------------------------------------------------

@v1_entitlement_router.post("/credentials", operation_id="create_credential")
async def create_credential(
    body: CreateCredentialRequest,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.credential_admin import CredentialAdmin
    _require_owner(tenant)
    try:
        plaintext = base64.b64decode(body.plaintext_b64, validate=True)
    except Exception:
        raise _coded(422, "invalid_plaintext", "plaintext_b64 is not valid base64")
    if not plaintext.strip():
        raise _coded(422, "invalid_plaintext", "the credential value is empty")

    from app.market_data.credentials import is_market_data_provider

    protector = _get_protector()
    admin = CredentialAdmin(protector)
    check = None
    if is_market_data_provider(body.provider):
        # Refuse a duplicate before spending a vendor round trip on it.
        with tenant_session(tenant) as session:
            if admin.active_version_id(session, tenant, body.credential_key) is not None:
                raise _coded(
                    409, "credential_key_active",
                    f"active credential already exists for {body.credential_key!r}",
                )
        check = await _check_market_data_key(
            tenant, body.provider, body.credential_key, plaintext,
        )
    with tenant_session(tenant) as session:
        try:
            meta = admin.create(
                session, tenant,
                credential_key=body.credential_key,
                provider=body.provider,
                plaintext=plaintext,
                display_label=body.display_label,
            )
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        except ValueError as exc:
            raise _coded(409, "credential_key_active", str(exc))
        last_check = _record_stored_check(session, tenant, meta, check)
    # 200, not 201: the OpenAPI contract (and the BFF's generated operation
    # registry, which fails closed on an undeclared status) declares 200.
    return JSONResponse(content=_credential_metadata_to_dict(meta, last_check=last_check))


@v1_entitlement_router.post(
    "/credentials/{credential_key}/rotate",
    operation_id="rotate_credential",
)
async def rotate_credential(
    credential_key: str,
    body: RotateCredentialRequest,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.credential_admin import CredentialAdmin

    _require_owner(tenant)
    try:
        new_plaintext = base64.b64decode(body.new_plaintext_b64, validate=True)
    except Exception:
        raise _coded(422, "invalid_plaintext", "new_plaintext_b64 is not valid base64")
    if not new_plaintext.strip():
        raise _coded(422, "invalid_plaintext", "the credential value is empty")

    from app.market_data.credentials import is_market_data_provider

    protector = _get_protector()
    admin = CredentialAdmin(protector)
    with tenant_session(tenant) as session:
        provider = admin.active_provider(session, tenant, credential_key)
    if provider is None:
        raise _coded(404, "credential_not_found", f"no active credential for {credential_key!r}")
    # A replacement key is checked like a new one; refused, the current
    # version stays active and bound.
    check = None
    if is_market_data_provider(provider):
        check = await _check_market_data_key(tenant, provider, credential_key, new_plaintext)
    with tenant_session(tenant) as session:
        try:
            meta = admin.rotate(
                session, tenant,
                credential_key=credential_key,
                new_plaintext=new_plaintext,
            )
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        except ValueError as exc:
            raise _coded(404, "credential_not_found", str(exc))
        last_check = _record_stored_check(session, tenant, meta, check)
    return JSONResponse(_credential_metadata_to_dict(meta, last_check=last_check))


@v1_entitlement_router.post(
    "/credentials/{credential_version_id}/revoke",
    operation_id="revoke_credential",
)
async def revoke_credential(
    credential_version_id: str,
    body: RevokeCredentialRequest,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.credential_admin import CredentialAdmin
    from app.entitlements.credentials import CredentialVersionRecord

    _require_owner(tenant)
    protector = _get_protector()
    admin = CredentialAdmin(protector)
    with tenant_session(tenant) as session:
        from sqlalchemy import select

        # Column-level read: the API role never sees the ciphertext columns.
        credential_key = session.execute(
            select(CredentialVersionRecord.credential_key).where(
                CredentialVersionRecord.id == credential_version_id,
                CredentialVersionRecord.workspace_id == tenant.workspace_id,
            )
        ).scalar_one_or_none()
        if credential_key is None:
            raise _coded(404, "credential_not_found", "credential version not found")
        try:
            admin.revoke(
                session, tenant,
                credential_key=credential_key,
                reason=body.reason,
            )
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        except ValueError as exc:
            raise _coded(404, "credential_not_found", str(exc))
    return JSONResponse({"ok": True})


@v1_entitlement_router.get("/credentials", operation_id="list_credentials")
async def list_credentials(
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.credential_admin import CredentialAdmin
    from app.market_data.credentials import is_market_data_provider, latest_checks
    _require_owner(tenant)
    protector = _get_protector()
    admin = CredentialAdmin(protector)
    with tenant_session(tenant) as session:
        try:
            items = admin.list_metadata(session, tenant)
            bindings = admin.bindings_by_version(session, tenant)
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        checks = latest_checks(session, tenant, [
            m.credential_version_id for m in items if is_market_data_provider(m.provider)
        ])
    return JSONResponse([
        _credential_metadata_to_dict(
            m,
            bindings=bindings.get(m.credential_version_id),
            last_check=checks.get(m.credential_version_id),
        )
        for m in items
    ])


@v1_entitlement_router.put(
    "/credentials/{credential_version_id}/bindings/{scope}",
    operation_id="bind_credential",
)
async def bind_credential(
    credential_version_id: str,
    scope: str,
    body: BindCredentialRequest,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.credential_admin import (
        CredentialAdmin,
        CredentialNotFound,
        CredentialScopeMismatch,
    )
    _require_owner(tenant)
    protector = _get_protector()
    admin = CredentialAdmin(protector)
    with tenant_session(tenant) as session:
        try:
            result = admin.bind(
                session, tenant,
                binding_scope=scope,
                provider=body.provider,
                credential_version_id=credential_version_id,
            )
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        except CredentialNotFound as exc:
            # Missing and another Workspace's version answer alike.
            raise _coded(404, "credential_not_found", str(exc))
        except CredentialScopeMismatch as exc:
            raise _coded(409, "credential_scope_provider_mismatch", str(exc))
        except ValueError as exc:
            # A rotated/revoked version, or one for another provider:
            # the binding would never resolve, so refuse it up front.
            raise _coded(409, "credential_not_bindable", str(exc))
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Spend guardrail endpoints
# ---------------------------------------------------------------------------

@v1_entitlement_router.get("/spend-guardrail", operation_id="get_spend_guardrail")
async def get_spend_guardrail(
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.guardrail_admin import GuardrailAdmin

    admin = GuardrailAdmin()
    with tenant_session(tenant) as session:
        try:
            data = admin.get(session, tenant)
        except ValueError as exc:
            raise _admin_problem(404, exc)
    # Serialize Decimals
    serialized = {}
    for k, v in data.items():
        serialized[k] = str(v) if isinstance(v, Decimal) else v
    return JSONResponse(serialized)


@v1_entitlement_router.post("/spend-guardrail/preview", operation_id="preview_spend_guardrail")
async def preview_spend_guardrail(
    body: GuardrailPreviewRequest,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.guardrail_admin import GuardrailAdmin

    _require_owner(tenant)
    admin = GuardrailAdmin()
    new_limits = {
        "per_operation_limit": body.per_operation_limit,
        "rolling_24h_limit": body.rolling_24h_limit,
        "period_limit": body.period_limit,
        "outstanding_exposure_limit": body.outstanding_exposure_limit,
    }
    with tenant_session(tenant) as session:
        try:
            result = admin.preview_update(
                session, tenant,
                new_limits=new_limits,
                expected_revision=body.expected_revision,
                mfa_evidence_ref=body.mfa_evidence_ref,
            )
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        except ValueError as exc:
            raise _admin_problem(400, exc)
    # Serialize Decimals in proposed_limits, and the expiry
    serialized = dict(result)
    if "proposed_limits" in serialized:
        serialized["proposed_limits"] = {
            k: str(v) for k, v in serialized["proposed_limits"].items()
        }
    serialized["expires_at"] = serialized["expires_at"].isoformat()
    return JSONResponse(serialized)


@v1_entitlement_router.post("/spend-guardrail/confirm", operation_id="confirm_spend_guardrail")
async def confirm_spend_guardrail(
    body: GuardrailConfirmRequest,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    from app.entitlements.guardrail_admin import GuardrailAdmin

    _require_owner(tenant)
    admin = GuardrailAdmin()
    with tenant_session(tenant) as session:
        try:
            result = admin.confirm_update(
                session, tenant,
                preview_token=body.preview_token,
                expected_revision=body.expected_revision,
            )
        except PermissionError as exc:
            raise _coded(403, "owner_role_required", str(exc))
        except ValueError as exc:
            raise _admin_problem(400, exc)
    serialized = {}
    for k, v in result.items():
        serialized[k] = str(v) if isinstance(v, Decimal) else v
    return JSONResponse(serialized)


# ---------------------------------------------------------------------------
# Usage endpoint
# ---------------------------------------------------------------------------

@v1_entitlement_router.get("/usage", operation_id="get_usage")
async def get_usage(
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    # No docstring: FastAPI would publish it in the OpenAPI description.
    # The metric's registered aggregation folds the window
    # (``UsageQuery.summarize``): a delta metric's ``total`` sums the real
    # buckets; a gauge's (``storage_bytes``) is its latest real event, and
    # each bucket its latest event. Mock (controlled Mock included) is
    # reported in ``by_execution_mode["mock"]`` and never in ``total``.
    from app.entitlements.usage import PROVIDER_CALLS, UsageQuery

    metric_key = request.query_params.get("metric_key", PROVIDER_CALLS.key)
    window_start_str = request.query_params.get("window_start")
    window_end_str = request.query_params.get("window_end")

    now = datetime.now(timezone.utc)
    if window_start_str:
        try:
            window_start = datetime.fromisoformat(window_start_str)
        except ValueError:
            raise _coded(400, "usage_window_invalid", "invalid window_start format")
    else:
        window_start = now - timedelta(hours=24)

    if window_end_str:
        try:
            window_end = datetime.fromisoformat(window_end_str)
        except ValueError:
            raise _coded(400, "usage_window_invalid", "invalid window_end format")
    else:
        window_end = now

    query = UsageQuery()
    with tenant_session(tenant) as session:
        summary = query.summarize(
            session, tenant,
            metric_key=metric_key,
            window_start=window_start,
            window_end=window_end,
        )

    return JSONResponse({
        "workspace_id": tenant.workspace_id,
        "metric_key": metric_key,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "total": str(summary.total),
        "by_execution_mode": {
            mode: str(quantity) for mode, quantity in summary.by_execution_mode.items()
        },
    })


# ---------------------------------------------------------------------------
# Cost summary endpoint
# ---------------------------------------------------------------------------

@v1_entitlement_router.get("/cost-summary", operation_id="get_cost_summary")
async def get_cost_summary(
    request: Request,
    tenant: TenantContext = Depends(request_tenant_context),
) -> Response:
    window_start_str = request.query_params.get("window_start")
    window_end_str = request.query_params.get("window_end")

    now = datetime.now(timezone.utc)
    if window_start_str:
        try:
            window_start = datetime.fromisoformat(window_start_str)
        except ValueError:
            raise _coded(400, "usage_window_invalid", "invalid window_start format")
    else:
        window_start = now - timedelta(hours=24)

    if window_end_str:
        try:
            window_end = datetime.fromisoformat(window_end_str)
        except ValueError:
            raise _coded(400, "usage_window_invalid", "invalid window_end format")
    else:
        window_end = now

    with tenant_session(tenant) as session:
        # Aggregate reported costs from the provider cost ledger
        rows = (
            session.query(
                ProviderCostLedgerEntryRecord.entry_type,
                ProviderCostLedgerEntryRecord.budget_currency,
                sa_func.coalesce(
                    sa_func.sum(ProviderCostLedgerEntryRecord.budget_amount),
                    Decimal("0"),
                ).label("total"),
            )
            .filter(
                and_(
                    ProviderCostLedgerEntryRecord.workspace_id == tenant.workspace_id,
                    ProviderCostLedgerEntryRecord.created_at >= window_start,
                    ProviderCostLedgerEntryRecord.created_at < window_end,
                )
            )
            .group_by(
                ProviderCostLedgerEntryRecord.entry_type,
                ProviderCostLedgerEntryRecord.budget_currency,
            )
            .all()
        )

    entries = [
        {
            "entry_type": row.entry_type,
            "budget_currency": row.budget_currency,
            "total": str(row.total),
        }
        for row in rows
    ]

    return JSONResponse({
        "workspace_id": tenant.workspace_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "entries": entries,
    })
