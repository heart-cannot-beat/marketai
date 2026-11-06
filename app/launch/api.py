"""HTTP interface for guarded Launch canary."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import service
from .models import (
    LaunchCandidateRecord,
    LaunchParityCheckRecord,
    LaunchProvisioningRecord,
    LaunchReconciliationRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from .recovery import RecoveryError, create_recovery_operation
from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import new_id
from ..platform.http_identity import request_tenant_context
from ..platform import jobs
from ..platform.canonical import canonical_hash
from ..platform.idempotency import IdempotencyKeyReusedError, create_or_replay_operation
from ..platform.meta_mode_auth import MetaModeNotPermitted, require_meta_mode
from ..platform.operations import OperationNotFoundError, get_operation
from ..platform.tenancy import TenantContext, TenantScopeError


router = APIRouter(prefix="/api/launch", tags=["launch-canary"])


class PreviewInput(BaseModel):
    experiment_id: str
    entry_id: str
    ad_account_id: str
    page_id: str
    pixel_id: str = ""
    landing_page_url: str
    message: str = Field(min_length=1, max_length=2200)
    headline: str = Field(min_length=1, max_length=255)
    country: str
    daily_budget_minor: int
    call_to_action: str = "LEARN_MORE"
    special_ad_categories: list[str] = Field(default_factory=list)


class ConfirmInput(BaseModel):
    preview_hash: str = Field(min_length=64, max_length=64)


class ExecuteInput(BaseModel):
    confirmation_token: str


def _translate(call):
    try:
        return call()
    except service.LaunchNotFound as exc:
        raise HTTPException(404, _launch_detail(exc)) from exc
    except service.LaunchConflict as exc:
        raise HTTPException(409, _launch_detail(exc)) from exc
    except service.LaunchForbidden as exc:
        raise HTTPException(403, _launch_detail(exc)) from exc
    except service.LaunchError as exc:
        raise HTTPException(503 if "配置" in str(exc) or "禁用" in str(exc) else 400,
                            _launch_detail(exc)) from exc


def _launch_detail(exc: service.LaunchError) -> dict:
    return {"code": exc.code, "message": str(exc), "params": exc.params}


@router.post("/previews", status_code=201)
def preview(body: PreviewInput, context: TenantContext = Depends(request_tenant_context)):
    return _translate(lambda: service.create_preview(context, **body.model_dump()))


@router.post("/previews/{preview_id}/confirm")
def confirm(
    preview_id: str,
    body: ConfirmInput,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(lambda: service.confirm_preview(
        context, preview_id, body.preview_hash
    ))


@router.post("/previews/{preview_id}/execute", status_code=202)
def execute(
    preview_id: str,
    body: ExecuteInput,
    context: TenantContext = Depends(request_tenant_context),
):
    operation_id = _translate(lambda: service.start_operation(
        context, preview_id, body.confirmation_token
    ))
    return {"id": operation_id, "state": "queued"}


@router.get("/operations/{operation_id}")
def operation(operation_id: str, context: TenantContext = Depends(request_tenant_context)):
    return _translate(lambda: service.get_operation(context, operation_id))


# ---------------------------------------------------------------------------
# Multi-candidate Launch (Task 16, spec §3.15-§5.7): preview / confirm /
# execute / provisioning status / recovery dispatch. Separate router
# (`/api/v1/launch`) from the single-candidate canary `router` above
# (`/api/launch`), which stays untouched. Every route opens its own
# `tenant_session(context)` and calls the Task 12-15 service/saga/recovery
# functions with the raw `session`, matching the pattern documented on
# `service.create_multi_candidate_preview`.
# ---------------------------------------------------------------------------

router_v1 = APIRouter(prefix="/api/v1/launch", tags=["launch-domain"])


class CandidateInput(BaseModel):
    experiment_version_candidate_id: str
    asset_version_id: str
    creative_config: dict = Field(default_factory=dict)


class MultiCandidatePreviewInput(BaseModel):
    experiment_id: str
    experiment_version_id: str
    connection_id: str
    meta_mode: str
    ad_account_id: str
    candidates: list[CandidateInput]
    daily_budget_minor: int
    currency: str
    campaign_config: dict = Field(default_factory=dict)


class MultiCandidateConfirmInput(BaseModel):
    meta_mode: str
    preview_hash: str = Field(min_length=64, max_length=64)


class MultiCandidateExecuteInput(BaseModel):
    meta_mode: str
    confirmation_token: str = Field(min_length=32)


class LaunchCandidateResponse(BaseModel):
    id: str
    candidate_index: int
    experiment_version_candidate_id: str
    asset_version_id: str
    creative_config: dict = Field(default_factory=dict)


class MultiCandidatePreviewResponse(BaseModel):
    id: str
    workspace_id: str
    meta_mode: str
    meta_connection_id: str
    meta_binding_id: str
    experiment_id: str
    experiment_version_id: str
    candidate_count: int
    campaign_config: dict = Field(default_factory=dict)
    daily_budget_minor: int
    total_daily_cost_minor: int
    currency: str
    preview_hash: str
    state: str
    confirmed_at: datetime | None = None
    expires_at: datetime
    created_at: datetime
    candidates: list[LaunchCandidateResponse] = Field(default_factory=list)


class LaunchProvisioningAccepted(BaseModel):
    id: str
    operation_id: str
    operation_state: str
    provisioning_state: str


class MultiCandidateConfirmationResponse(MultiCandidatePreviewResponse):
    confirmation_token: str
    confirmation_expires_at: datetime
    confirmation_context_hash: str


#: Saga steps (``saga.py``). The interface words each one
#: (``experiments:launch.step-<step_type>``,
#: ``web/__tests__/locales/state-codes.spec.ts``).
LaunchStepType = Literal[
    "create_campaign", "create_adset", "create_ad",
    "readback_campaign", "readback_adset", "readback_ad",
]


class LaunchProvisioningStepResponse(BaseModel):
    step_type: LaunchStepType
    state: str
    remote_object_id: str | None = None


class LaunchParityCheckResponse(BaseModel):
    passed: bool
    all_candidates_present: bool
    budget_parity_verified: bool
    config_parity_verified: bool
    status_parity_verified: bool


class LaunchProvisioningResponse(BaseModel):
    id: str
    operation_id: str
    operation_state: str
    provisioning_state: str
    activation_state: str
    remote_campaign_id: str | None = None
    fencing_token: int
    steps: list[LaunchProvisioningStepResponse] = Field(default_factory=list)
    parity_check: LaunchParityCheckResponse | None = None


class LaunchRecoveryResponse(BaseModel):
    id: str
    original_operation_id: str
    recovery_operation_id: str
    state: str
    resolution: str | None = None


class MetaModeRequest(BaseModel):
    """Body for POST routes that otherwise carry no payload but still need
    to bind the request to a Meta mode (see `_effective_context` below)."""

    meta_mode: str


def _effective_context(context: TenantContext, meta_mode: str) -> TenantContext:
    """Bind `meta_mode` onto the verified `TenantContext` for this request.

    `request_tenant_context()` never populates `meta_mode` -- it comes from
    verified BFF/delegated-identity claims (spec §3), which say nothing
    about which Meta mode a given request targets (spec §2: a Workspace may
    operate connections in more than one mode). Every multi-candidate Launch
    route therefore accepts `meta_mode` explicitly and binds it here.

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


def _multi_launch_error_status(exc: service.LaunchError) -> int:
    if isinstance(exc, service.LaunchNotFound):
        return 404
    if isinstance(exc, service.LaunchConflict):
        return 409
    if isinstance(exc, service.LaunchForbidden):
        return 403
    return 400


def _multi_preview_dict(preview: MetaLaunchPreviewRecord, candidates: list[LaunchCandidateRecord]) -> dict:
    """Spec §3.15: ``total_daily_cost_minor`` is the per-candidate budget
    multiplied out across the whole frozen candidate set, plus binding
    evidence (``meta_binding_id``)."""
    return {
        "id": preview.id,
        "workspace_id": preview.workspace_id,
        "meta_mode": preview.meta_mode,
        "meta_connection_id": preview.meta_connection_id,
        "meta_binding_id": preview.meta_binding_id,
        "experiment_id": preview.experiment_id,
        "experiment_version_id": preview.experiment_version_id,
        "candidate_count": preview.candidate_count,
        "campaign_config": preview.campaign_config,
        "daily_budget_minor": preview.daily_budget_minor,
        "total_daily_cost_minor": preview.daily_budget_minor * preview.candidate_count,
        "currency": preview.currency,
        "preview_hash": preview.preview_hash,
        "state": preview.state,
        "confirmed_at": preview.confirmed_at.isoformat() if preview.confirmed_at else None,
        "expires_at": preview.expires_at.isoformat(),
        "created_at": preview.created_at.isoformat(),
        "candidates": [
            {
                "id": row.id,
                "candidate_index": row.candidate_index,
                "experiment_version_candidate_id": row.experiment_version_candidate_id,
                "asset_version_id": row.asset_version_id,
                "creative_config": row.creative_config,
            }
            for row in sorted(candidates, key=lambda row: row.candidate_index)
        ],
    }


def _get_multi_preview(session, context: TenantContext, preview_id: str) -> MetaLaunchPreviewRecord:
    preview = session.execute(
        select(MetaLaunchPreviewRecord).where(
            MetaLaunchPreviewRecord.id == preview_id,
            MetaLaunchPreviewRecord.workspace_id == context.workspace_id,
            MetaLaunchPreviewRecord.meta_mode == context.meta_mode,
        )
    ).scalar_one_or_none()
    if preview is None:
        raise HTTPException(404, {"code": "launch_not_found", "message": "Preview not found"})
    return preview


def _list_multi_candidates(session, preview_id: str) -> list[LaunchCandidateRecord]:
    return list(session.execute(
        select(LaunchCandidateRecord)
        .where(LaunchCandidateRecord.preview_id == preview_id)
        .order_by(LaunchCandidateRecord.candidate_index)
    ).scalars().all())


@router_v1.post(
    "/previews",
    status_code=201,
    operation_id="create_meta_launch_preview",
    response_model=MultiCandidatePreviewResponse,
)
def create_multi_candidate_preview_v1(
    body: MultiCandidatePreviewInput, context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    with tenant_session(effective) as session:
        try:
            preview, candidates = service.create_multi_candidate_preview(
                session, effective,
                experiment_id=body.experiment_id,
                experiment_version_id=body.experiment_version_id,
                connection_id=body.connection_id,
                ad_account_id=body.ad_account_id,
                candidates=[candidate.model_dump() for candidate in body.candidates],
                daily_budget_minor=body.daily_budget_minor,
                currency=body.currency,
                campaign_config=body.campaign_config,
            )
        except service.LaunchError as exc:
            raise HTTPException(
                _multi_launch_error_status(exc), _launch_detail(exc),
            ) from exc
        return _multi_preview_dict(preview, candidates)


@router_v1.get(
    "/previews/{preview_id}",
    operation_id="get_meta_launch_preview",
    response_model=MultiCandidatePreviewResponse,
)
def get_multi_candidate_preview_v1(
    preview_id: str,
    meta_mode: str = Query(...),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        preview = _get_multi_preview(session, effective, preview_id)
        candidates = _list_multi_candidates(session, preview_id)
        return _multi_preview_dict(preview, candidates)


@router_v1.post(
    "/previews/{preview_id}/confirm",
    operation_id="confirm_meta_launch_preview",
    response_model=MultiCandidateConfirmationResponse,
)
def confirm_multi_candidate_preview_v1(
    preview_id: str,
    body: MultiCandidateConfirmInput,
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, body.meta_mode)
    with tenant_session(effective) as session:
        try:
            issued = service.confirm_multi_candidate_preview(
                session, effective, preview_id, body.preview_hash,
            )
        except service.LaunchError as exc:
            raise HTTPException(
                _multi_launch_error_status(exc), _launch_detail(exc),
            ) from exc
        candidates = _list_multi_candidates(session, preview_id)
        return {
            **_multi_preview_dict(issued.preview, candidates),
            "confirmation_token": issued.confirmation_token,
            "confirmation_expires_at": issued.confirmation.expires_at,
            "confirmation_context_hash": issued.confirmation.context_hash,
        }


@router_v1.post(
    "/previews/{preview_id}/execute",
    status_code=202,
    operation_id="execute_meta_launch_preview",
    response_model=LaunchProvisioningAccepted,
    openapi_extra={"x-marketai-idempotent": True},
)
def execute_multi_candidate_preview_v1(
    preview_id: str,
    body: MultiCandidateExecuteInput,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=128,
    ),
):
    """Consume a fresh confirmation and enqueue the durable PAUSED saga."""
    effective = _effective_context(context, body.meta_mode)
    if effective.meta_mode not in ("customer_sandbox", "controlled_mock"):
        raise HTTPException(
            403,
            {
                "code": "launch_mode_forbidden",
                "message": (
                    "PAUSED provisioning is available only in "
                    "customer_sandbox and controlled_mock modes"
                ),
            },
        )
    with tenant_session(effective) as session:
        preview = _get_multi_preview(session, effective, preview_id)
        if preview.state != "confirmed":
            raise HTTPException(
                409, {"code": "launch_conflict", "message": f"Preview is {preview.state}, not confirmed"},
            )
        try:
            service.validate_multi_candidate_preview_integrity(session, effective, preview)
        except service.LaunchError as exc:
            raise HTTPException(
                _multi_launch_error_status(exc),
                _launch_detail(exc),
            ) from exc

        fingerprint = canonical_hash({
            "command": "launch_provisioning",
            "preview_id": preview.id,
            "preview_hash": preview.preview_hash,
            "meta_mode": body.meta_mode,
            "confirmation_token_hash": canonical_hash(body.confirmation_token),
        })
        try:
            operation = create_or_replay_operation(
                session,
                idempotency_key=idempotency_key,
                command_type="launch_provisioning",
                fingerprint_hash=fingerprint,
                actor=effective.executor,
            )
        except IdempotencyKeyReusedError as exc:
            raise HTTPException(
                409,
                {"code": "idempotency_key_reused", "message": str(exc)},
            ) from exc
        existing = session.scalar(select(LaunchProvisioningRecord).where(
            LaunchProvisioningRecord.workspace_id == effective.workspace_id,
            LaunchProvisioningRecord.meta_mode == effective.meta_mode,
            LaunchProvisioningRecord.operation_id == operation.id,
        ))
        if existing is not None:
            return {
                "id": existing.id,
                "operation_id": existing.operation_id,
                "operation_state": operation.state,
                "provisioning_state": existing.provisioning_state,
            }

        try:
            confirmation = service.consume_multi_candidate_confirmation(
                session, effective, preview, body.confirmation_token,
            )
        except service.LaunchError as exc:
            raise HTTPException(
                _multi_launch_error_status(exc),
                _launch_detail(exc),
            ) from exc

        provisioning = LaunchProvisioningRecord(
            id=new_id("lprov"), workspace_id=effective.workspace_id,
            meta_mode=effective.meta_mode,
            meta_connection_id=preview.meta_connection_id, preview_id=preview.id,
            confirmation_id=confirmation.id, operation_id=operation.id,
            campaign_idempotency_key=f"campaign:{preview.id}",
            provisioning_state="pending",
        )
        session.add(provisioning)
        session.flush()
        operation.result_resource_type = "launch_provisioning"
        operation.result_resource_id = provisioning.id
        jobs.enqueue_in_session(
            session,
            context=effective,
            job_type="launch_provisioning",
            payload={
                "operation_id": operation.id,
                "provisioning_id": provisioning.id,
                "meta_mode": effective.meta_mode,
            },
            idempotency_key=f"launch-provisioning:{operation.id}",
            resource_type="launch_provisioning",
            resource_id=provisioning.id,
            safe_to_retry_after_crash=False,
            max_attempts=1,
        )

        return {
            "id": provisioning.id,
            "operation_id": operation.id,
            "operation_state": operation.state,
            "provisioning_state": provisioning.provisioning_state,
        }


@router_v1.get(
    "/operations/{provisioning_id}",
    operation_id="get_meta_launch_provisioning",
    response_model=LaunchProvisioningResponse,
)
def get_provisioning_status_v1(
    provisioning_id: str,
    meta_mode: str = Query(...),
    context: TenantContext = Depends(request_tenant_context),
):
    effective = _effective_context(context, meta_mode)
    with tenant_session(effective) as session:
        prov = session.execute(
            select(LaunchProvisioningRecord).where(
                LaunchProvisioningRecord.id == provisioning_id,
                LaunchProvisioningRecord.workspace_id == effective.workspace_id,
                LaunchProvisioningRecord.meta_mode == effective.meta_mode,
            )
        ).scalar_one_or_none()
        if prov is None:
            raise HTTPException(404, {"code": "launch_not_found", "message": "Provisioning not found"})
        steps = session.execute(
            select(LaunchSagaStepAttemptRecord)
            .where(LaunchSagaStepAttemptRecord.provisioning_id == provisioning_id)
            .order_by(LaunchSagaStepAttemptRecord.created_at)
        ).scalars().all()
        parity = session.execute(
            select(LaunchParityCheckRecord)
            .where(LaunchParityCheckRecord.provisioning_id == provisioning_id)
            .order_by(LaunchParityCheckRecord.created_at.desc())
        ).scalars().first()
        return {
            "id": prov.id,
            "operation_id": prov.operation_id,
            "operation_state": get_operation(session, prov.operation_id).state,
            "provisioning_state": prov.provisioning_state,
            "activation_state": prov.activation_state,
            "remote_campaign_id": prov.remote_campaign_id,
            "fencing_token": prov.fencing_token,
            "steps": [
                {"step_type": row.step_type, "state": row.state, "remote_object_id": row.remote_object_id}
                for row in steps
            ],
            "parity_check": None if parity is None else {
                "passed": parity.passed,
                "all_candidates_present": parity.all_candidates_present,
                "budget_parity_verified": parity.budget_parity_verified,
                "config_parity_verified": parity.config_parity_verified,
                "status_parity_verified": parity.status_parity_verified,
            },
        }


@router_v1.post(
    "/operations/{operation_id}/recovery",
    status_code=202,
    operation_id="recover_meta_launch_operation",
    response_model=LaunchRecoveryResponse,
    openapi_extra={"x-marketai-idempotent": True},
)
def recover_operation_v1(
    operation_id: str,
    body: MetaModeRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=128,
    ),
):
    """Create/replay and enqueue recovery for an uncertain Operation."""
    effective = _effective_context(context, body.meta_mode)
    with tenant_session(effective) as session:
        try:
            fingerprint = canonical_hash({
                "command": "launch_recovery",
                "original_operation_id": operation_id,
                "meta_mode": body.meta_mode,
            })
            recovery_operation = create_or_replay_operation(
                session,
                idempotency_key=idempotency_key,
                command_type="launch_recovery",
                fingerprint_hash=fingerprint,
                actor=effective.executor,
            )
            reconciliation = session.scalar(select(LaunchReconciliationRecord).where(
                LaunchReconciliationRecord.workspace_id == effective.workspace_id,
                LaunchReconciliationRecord.meta_mode == effective.meta_mode,
                LaunchReconciliationRecord.recovery_operation_id == recovery_operation.id,
            ))
            if reconciliation is None:
                reconciliation = create_recovery_operation(
                    session,
                    context=effective,
                    original_operation_id=operation_id,
                    recovery_operation=recovery_operation,
                )
                jobs.enqueue_in_session(
                    session,
                    context=effective,
                    job_type="launch_recovery",
                    payload={
                        "reconciliation_id": reconciliation.id,
                        "meta_mode": effective.meta_mode,
                    },
                    idempotency_key=f"launch-recovery:{recovery_operation.id}",
                    resource_type="launch_reconciliation",
                    resource_id=reconciliation.id,
                    safe_to_retry_after_crash=True,
                    max_attempts=3,
                )
        except OperationNotFoundError as exc:
            raise HTTPException(404, {"code": "launch_not_found", "message": str(exc)}) from exc
        except IdempotencyKeyReusedError as exc:
            raise HTTPException(
                409, {"code": "idempotency_key_reused", "message": str(exc)},
            ) from exc
        except RecoveryError as exc:
            raise HTTPException(409, {"code": "recovery_conflict", "message": str(exc)}) from exc
        return {
            "id": reconciliation.id,
            "original_operation_id": reconciliation.original_operation_id,
            "recovery_operation_id": reconciliation.recovery_operation_id,
            "state": reconciliation.state,
            "resolution": reconciliation.resolution,
        }
