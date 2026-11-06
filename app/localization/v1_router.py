"""Versioned Localization HTTP interface.

Commands delegate to the existing Issue #24 batch service; reads delegate to
``queries`` so the browser receives persisted Truth, QC, cost, and lineage
evidence without reconstructing domain state.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.adapters.persistence.database import tenant_session
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.platform.tenancy import TenantContext

from . import batch as batch_module
from . import queries
from .batch import BatchMarketTargetInput, BatchSourceInput
from .claims import ClaimDecisionInput
from .copy import TranslationRecipe
from .evidence_tier import tier_of_experiment
from .response_models import (
    AdoptIsolatedOutputResult,
    ApproveCopyResult,
    AuthorizeIsolatedQCResult,
    BatchDraftResult,
    BatchOutcome,
    CancelResult,
    ConfirmResult,
    LocalizationBatchDetail,
    LocalizationBatchPage,
    LocalizationItemDetail,
    LocalizationWorkspace,
    ResumeItemResult,
    RetryItemResult,
    StartPreparationResult,
    SubmitClaimDecisionsResult,
)
from .routes import (
    ApproveCopyInput,
    ConfirmInput,
    CreateDraftInput,
    StartPreparationInput,
    SubmitClaimDecisionsInput,
    _actor_id,
    _build_admission,
)

router = APIRouter(prefix="/localization", tags=["localization"])


def _idempotency_key(
    key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return key


def _page(items: list, ctx: TenantContext, filters_key: str,
          cursor: str | None, limit: int) -> dict:
    start = 0
    if cursor:
        try:
            decoded = decode_cursor(cursor, ctx.workspace_id, filters_key)
        except InvalidCursorError as exc:
            from fastapi import HTTPException
            raise HTTPException(400, {"code": "invalid_cursor", "message": str(exc)}) from exc
        start = int(decoded[0]) if decoded else 0
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        next_cursor = encode_cursor(ctx.workspace_id, filters_key, (start + limit,))
    return {"items": page, "next_cursor": next_cursor}


@router.get("/workspace", operation_id="get_localization_workspace",
            response_model=LocalizationWorkspace)
def get_localization_workspace(
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return queries.get_workspace(session, context)


@router.get("/batches", operation_id="list_localization_batches",
            response_model=LocalizationBatchPage)
def list_localization_batches(
    phase: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        items = queries.list_batches(session, context, phase=phase)
    return _page(items, context, f"phase={phase or ''}", cursor, limit)


@router.get("/batches/{batch_id}", operation_id="get_localization_batch",
            response_model=LocalizationBatchDetail)
def get_localization_batch(
    batch_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        result = queries.get_batch(session, context, batch_id)
    set_etag(response, result["revision"])
    return result


@router.get("/batches/{batch_id}/items/{item_id}",
            operation_id="get_localization_item", response_model=LocalizationItemDetail)
def get_localization_item(
    batch_id: str,
    item_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        result = queries.get_item(session, context, batch_id, item_id)
    set_etag(response, result["revision"])
    return result


@router.get("/batches/{batch_id}/outcome", operation_id="get_localization_batch_outcome",
            response_model=BatchOutcome)
def get_localization_batch_outcome(
    batch_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        batch = queries.get_batch(session, context, batch_id)
    return {"batch_id": batch["id"], "phase": batch["phase"],
            "outcome": batch["outcome"], "revision": batch["revision"],
            "item_count": batch["item_count"],
            "item_state_counts": batch["item_state_counts"]}


@router.post("/batches", status_code=201, operation_id="create_localization_batch",
             response_model=BatchDraftResult,
             openapi_extra={"x-marketai-idempotent": True})
def create_localization_batch(
    body: CreateDraftInput,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.create_draft(
            session, context,
            product_version_id=body.product_version_id,
            offer_version_id=body.offer_version_id,
            truth_data_mode=body.truth_data_mode,
            provider_execution_mode=body.provider_execution_mode,
            sources=[BatchSourceInput(**source.model_dump()) for source in body.sources],
            market_targets=[BatchMarketTargetInput(**target.model_dump())
                            for target in body.market_targets],
            requested_evidence_tier=body.requested_evidence_tier,
            actor_id=_actor_id(context),
        ))


@router.post("/batches/{batch_id}/start-preparation",
             operation_id="start_localization_preparation",
             response_model=StartPreparationResult,
             openapi_extra={"x-marketai-idempotent": True})
def start_localization_preparation(
    batch_id: str,
    body: StartPreparationInput,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.start_preparation(
            session, context, batch_id=batch_id,
            truth_command=None,
            truth_resolution_policy_version_id=(
                body.truth_resolution_policy_version_id
            ),
            data_source_version_ids=tuple(body.data_source_version_ids),
            manual_fact_set_version_id=body.manual_fact_set_version_id,
            analysis_recipe_hash=body.analysis_recipe_hash,
            analysis_provider=body.analysis_provider,
            analysis_model=body.analysis_model,
            analysis_adapter_version=body.analysis_adapter_version,
            actor_id=_actor_id(context), admission=_build_admission(),
        ))


@router.post("/batches/{batch_id}/claim-decisions",
             operation_id="submit_localization_claim_decisions",
             response_model=SubmitClaimDecisionsResult,
             openapi_extra={"x-marketai-idempotent": True})
def submit_localization_claim_decisions(
    batch_id: str,
    body: SubmitClaimDecisionsInput,
    request: Request,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    recipe = TranslationRecipe(**body.translation_recipe.model_dump()) \
        if body.translation_recipe is not None else None
    with tenant_session(context) as session:
        return asdict(batch_module.submit_claim_decisions(
            session, context, batch_id=batch_id,
            expected_revision=extract_if_match(request),
            asset_version_id=body.asset_version_id,
            target_market=body.target_market,
            analysis_recipe_hash=body.analysis_recipe_hash,
            decisions=[ClaimDecisionInput(**decision.model_dump()) for decision in body.decisions],
            copy_policy_snapshot=body.copy_policy_snapshot,
            source_locale=body.source_locale, target_locale=body.target_locale,
            translation_recipe=recipe,
            requested_evidence_tier=tier_of_experiment(body.requested_evidence_tier),
            actor_id=_actor_id(context), role=context.role, admission=_build_admission(),
        ))


@router.post("/batches/{batch_id}/approve-copy",
             operation_id="approve_localization_copy",
             response_model=ApproveCopyResult,
             openapi_extra={"x-marketai-idempotent": True})
def approve_localization_copy(
    batch_id: str,
    body: ApproveCopyInput,
    request: Request,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.approve_copy(
            session, context, batch_id=batch_id,
            expected_revision=extract_if_match(request),
            copy_version_id=body.copy_version_id,
            actor_id=_actor_id(context), role=context.role,
        ))


@router.post("/batches/{batch_id}/confirm", operation_id="confirm_localization_batch",
             response_model=ConfirmResult,
             openapi_extra={"x-marketai-idempotent": True})
def confirm_localization_batch(
    batch_id: str,
    body: ConfirmInput,
    request: Request,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.confirm(
            session, context, batch_id=batch_id,
            expected_revision=extract_if_match(request),
            edit_provider=body.edit_provider, edit_model=body.edit_model,
            edit_adapter_version=body.edit_adapter_version,
            external_qc_steps=tuple(body.external_qc_steps), max_attempts=body.max_attempts,
            actor_id=_actor_id(context), role=context.role, admission=_build_admission(),
        ))


@router.post("/batches/{batch_id}/items/{item_id}/retry",
             operation_id="retry_localization_item", response_model=RetryItemResult,
             openapi_extra={"x-marketai-idempotent": True})
def retry_localization_item(
    batch_id: str,
    item_id: str,
    request: Request,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.retry_item(
            session, context, batch_id=batch_id, item_id=item_id,
            expected_item_revision=extract_if_match(request),
            actor_id=_actor_id(context), role=context.role, admission=_build_admission(),
        ))


@router.post("/batches/{batch_id}/items/{item_id}/resume",
             operation_id="resume_localization_item", response_model=ResumeItemResult,
             openapi_extra={"x-marketai-idempotent": True})
def resume_localization_item(
    batch_id: str,
    item_id: str,
    request: Request,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.resume_item(
            session, context, batch_id=batch_id, item_id=item_id,
            expected_item_revision=extract_if_match(request),
            actor_id=_actor_id(context),
        ))


@router.post(
    "/batches/{batch_id}/items/{item_id}/attempts/{attempt_id}/authorize-isolated-qc",
    operation_id="authorize_localization_isolated_qc",
    response_model=AuthorizeIsolatedQCResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def authorize_localization_isolated_qc(
    batch_id: str,
    item_id: str,
    attempt_id: str,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    """Admit only the frozen external QC steps a preserved isolated output
    still needs (spec §2E/§2H). Owner or Member, exactly like the other
    recovery commands -- and, as with them, the role that reaches the domain
    service is the one signed into the delegated identity token, never a
    client-supplied value."""
    with tenant_session(context) as session:
        return asdict(batch_module.authorize_isolated_qc(
            session, context, batch_id=batch_id, item_id=item_id,
            attempt_id=attempt_id, actor_id=_actor_id(context), role=context.role,
            admission=_build_admission(),
        ))


@router.post(
    "/batches/{batch_id}/items/{item_id}/attempts/{attempt_id}/adopt",
    operation_id="adopt_localization_isolated_output",
    response_model=AdoptIsolatedOutputResult,
    openapi_extra={"x-marketai-idempotent": True},
)
def adopt_localization_isolated_output(
    batch_id: str,
    item_id: str,
    attempt_id: str,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    """Recheck and publish an isolated output whose entire frozen QC plan
    has passed (spec §2E/§2H). Refused while any frozen check is missing --
    the caller must run `authorize_localization_isolated_qc` first and wait
    for it."""
    with tenant_session(context) as session:
        return asdict(batch_module.adopt_isolated_output(
            session, context, batch_id=batch_id, item_id=item_id,
            attempt_id=attempt_id, actor_id=_actor_id(context), role=context.role,
        ))


@router.post("/batches/{batch_id}/cancel", operation_id="cancel_localization_batch",
             response_model=CancelResult,
             openapi_extra={"x-marketai-idempotent": True})
def cancel_localization_batch(
    batch_id: str,
    request: Request,
    _key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        return asdict(batch_module.cancel(
            session, context, batch_id=batch_id,
            expected_revision=extract_if_match(request), actor_id=_actor_id(context),
        ))
