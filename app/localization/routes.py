"""Localization batch command interface (Issue #24, spec §2E, §15, §16.13).

Thin HTTP adapter over the batch module (Task 14) and the eligibility
module -- every route follows ``app.experiments.api``'s pattern for
identity/request-body handling: Workspace and actor identity come only
from ``request_tenant_context`` (never a request body field), a Pydantic
``BaseModel`` validates the request body, and ``tenant_session`` opens the
one transaction the command runs in.

Errors follow ``app.generation.api``'s pattern instead of
``app.experiments.api``'s: routes let the batch module's typed errors
propagate uncaught, and :func:`register_problem_handlers` (called from
``app/main.py``, mirroring ``register_generation_problem_handlers``)
registers one ``@app.exception_handler`` per error type, each building a
full RFC 9457 body directly. This -- rather than wrapping every error in
``HTTPException(status, {"code": ..., "message": ...})`` -- matters
because ``app.library.api``'s shared ``HTTPException`` handler (registered
app-wide) only forwards ``code``/``message``/``retryable``/``field_errors``
from ``exc.detail``; a ``findings`` list (validation) or
``record``/``record_id``/``expected_revision`` (stale revision) would be
silently dropped before reaching the client if raised through that path.

Permissions (spec §15): every command in this router is Member+
(Owner/Member/system are the only roles ``TenantContext`` admits -- there
is no role below Member -- so no per-route role gate is needed here).
Owner-only actions (publish/revoke DataSourceVersion, OfferVersion,
manual Facts, BrandLocalizationPolicyVersion; credential management) live
in ``app.library.service``/``app.library.datasources``, not in this
router -- out of Task 18's scope per the brief.

Idempotency/CAS: ``create_draft`` requires an ``Idempotency-Key`` header
(following ``app.generation.v1_router``'s pattern) even though batch.py's
own replay is anchored on a deterministic semantic-key hash over business
inputs, never the header itself -- the header keeps this router's contract
consistent with every other command router in the app. ``confirm``,
``retry_item`` and ``cancel`` are the three commands that take an
optimistic-concurrency ``expected_revision``/``expected_item_revision`` in
batch.py; those three read it from ``If-Match`` (``app.platform.etag.
extract_if_match``) exactly like ``app.generation.v1_router``.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .. import config
from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    LocalizationBatchMarketTargetRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationItemRecord,
)
from ..entitlements.admission import PaidOperationAdmission
from ..entitlements.policy import EntitlementPolicy
from ..entitlements.quote_engine import CostQuoteEngine
from ..entitlements.reservation_book import ReservationBook
from ..platform.etag import extract_if_match, set_etag
from ..platform.http_identity import request_tenant_context
from ..platform.tenancy import TenantContext, new_request_id

from . import batch as batch_module
from . import eligibility as eligibility_module
from .queries import LocalizationNotFoundError
from .batch import (
    AttemptExhaustedError,
    BatchError,
    BatchMarketTargetInput,
    BatchNotTerminalError,
    BatchSourceInput,
    BatchStateError,
    BatchValidationError,
    FrozenBatchError,
    ItemNotRetryableError,
    StaleRevisionError,
)
from .claims import ClaimDecisionInput
from .copy import TranslationRecipe
from .evidence_tier import tier_of_experiment
from .truth import TruthResolutionError

router = APIRouter(prefix="/api/localization", tags=["localization"])


# ---------------------------------------------------------------------------
# Admission -- mirrors app.store._build_run_admission's construction
# (EntitlementPolicy/CostQuoteEngine/ReservationBook), built fresh per
# request since PaidOperationAdmission is stateless.
# ---------------------------------------------------------------------------


class _WildcardCostQuoteEngine(CostQuoteEngine):
    """A CostQuoteEngine that falls back to a ``*`` model key when the
    exact provider/model pair is not priced -- mirrors
    ``app.store._WildcardCostQuoteEngine`` -- and, unlike Generation's
    fixed provider vocabulary (``config.COST_PER_IMAGE``), further falls
    back to a zero unit price for a provider this router has no price for
    at all. Localization's edit/analysis/translation providers are
    caller-supplied per BrandLocalizationPolicy/DataSource, not a closed
    enum a static price table can enumerate; defaulting an unpriced
    provider to zero (rather than raising) keeps sandbox/mock execution
    unblocked pending a real price table for this domain.
    """

    def _lookup(self, provider, model):
        provider_prices = self._registry.get(provider)
        if provider_prices is None:
            return Decimal("0")
        if model in provider_prices:
            return provider_prices[model]
        if "*" in provider_prices:
            return provider_prices["*"]
        return Decimal("0")


def _build_admission() -> PaidOperationAdmission:
    registry = {provider: {"*": price} for provider, price in config.COST_PER_IMAGE.items()}
    return PaidOperationAdmission(
        policy=EntitlementPolicy(),
        quote_engine=_WildcardCostQuoteEngine(price_registry=registry, price_rule_version="v1"),
        reservation_book=ReservationBook(),
    )


def _actor_id(context: TenantContext) -> str:
    return context.initiator_id or context.executor


# ---------------------------------------------------------------------------
# Idempotency-Key -- required on create_draft (see module docstring).
# ---------------------------------------------------------------------------


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


# ---------------------------------------------------------------------------
# Error translation -- RFC 9457 Problem Details, registered per-exception-
# type (see module docstring for why this router does not use
# app.library.api's shared HTTPException-dict path).
# ---------------------------------------------------------------------------


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


def register_problem_handlers(app: FastAPI) -> None:
    """RFC 9457 exception handlers for this router's batch-module errors,
    mirroring ``app.generation.api.register_problem_handlers``. Called
    once from ``app/main.py``.

    Each handler is registered on the specific error class it handles
    (not a shared ``HTTPException``), so it wins the FastAPI/Starlette
    handler lookup (which walks ``type(exc).__mro__``) over any generic
    handler, and every structured field this module wants to surface --
    ``findings`` on a 422, ``record``/``record_id``/``expected_revision``
    on a 409 stale-revision -- reaches the client.
    """

    @app.exception_handler(BatchValidationError)
    async def _batch_validation_handler(request: Request, exc: BatchValidationError):
        if exc.findings in (("batch_not_found",), ("item_not_found",)):
            return _request_problem(request, 404, "localization_not_found", str(exc))
        return _request_problem(
            request, 422, "batch_validation_failed", str(exc), findings=list(exc.findings),
        )

    @app.exception_handler(LocalizationNotFoundError)
    async def _localization_not_found_handler(request: Request, exc: LocalizationNotFoundError):
        return _request_problem(request, 404, "localization_not_found", str(exc))

    @app.exception_handler(TruthResolutionError)
    async def _truth_resolution_error_handler(
        request: Request, exc: TruthResolutionError
    ):
        return _request_problem(
            request, 422, "truth_configuration_invalid", str(exc)
        )

    @app.exception_handler(FrozenBatchError)
    async def _frozen_batch_handler(request: Request, exc: FrozenBatchError):
        return _request_problem(request, 409, "batch_frozen_conflict", str(exc))

    @app.exception_handler(StaleRevisionError)
    async def _stale_revision_handler(request: Request, exc: StaleRevisionError):
        # 409, matching the codebase-wide CAS-conflict convention
        # (app.platform.etag.check_revision_match, app.generation.
        # v1_router's Run commands, app.generation.api's own
        # StaleRevisionError handler) -- not 412, which this router used
        # in an earlier revision.
        return _request_problem(
            request, 409, "stale_revision", str(exc),
            record=exc.record, record_id=exc.record_id,
            expected_revision=exc.expected_revision,
        )

    @app.exception_handler(AttemptExhaustedError)
    async def _attempt_exhausted_handler(request: Request, exc: AttemptExhaustedError):
        return _request_problem(request, 409, "attempt_exhausted", str(exc))

    @app.exception_handler(ItemNotRetryableError)
    async def _item_not_retryable_handler(request: Request, exc: ItemNotRetryableError):
        return _request_problem(request, 409, "item_not_retryable", str(exc))

    @app.exception_handler(BatchNotTerminalError)
    async def _batch_not_terminal_handler(request: Request, exc: BatchNotTerminalError):
        return _request_problem(request, 409, "batch_not_terminal", str(exc))

    @app.exception_handler(BatchStateError)
    async def _batch_state_handler(request: Request, exc: BatchStateError):
        return _request_problem(request, 409, "batch_state_conflict", str(exc))

    @app.exception_handler(BatchError)
    async def _batch_error_handler(request: Request, exc: BatchError):
        # Fallback for any BatchError not more specifically handled above.
        return _request_problem(request, 400, "batch_error", str(exc))


# ---------------------------------------------------------------------------
# Request models -- create_draft
# ---------------------------------------------------------------------------


class BatchSourceInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_version_id: str
    decision_id: str


class BatchMarketTargetInputModel(BaseModel):
    market_policy_version_id: str
    market: str


class CreateDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_version_id: str
    offer_version_id: str
    truth_data_mode: Literal["real", "mock"]
    provider_execution_mode: Literal["byok", "sandbox", "mock"]
    sources: list[BatchSourceInputModel] = Field(min_length=1)
    market_targets: list[BatchMarketTargetInputModel] = Field(min_length=1)
    requested_evidence_tier: str | None = None


# ---------------------------------------------------------------------------
# Request models -- start_preparation
# ---------------------------------------------------------------------------

class StartPreparationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    truth_resolution_policy_version_id: str
    data_source_version_ids: list[str] = Field(min_length=1)
    manual_fact_set_version_id: str | None = None
    analysis_recipe_hash: str
    analysis_provider: str
    analysis_model: str
    analysis_adapter_version: str


# ---------------------------------------------------------------------------
# Request models -- submit_claim_decisions / approve_copy / confirm
# ---------------------------------------------------------------------------


class ClaimDecisionInputModel(BaseModel):
    claim_index: int
    action: Literal["replace", "remove", "preserve"]
    replacement_fact_id: str | None = None
    removal_strategy: dict | None = None
    rationale: str | None = None


class TranslationRecipeInput(BaseModel):
    execution_mode: Literal["byok", "sandbox", "mock"]
    provider: str
    model: str
    adapter_version: str


class SubmitClaimDecisionsInput(BaseModel):
    asset_version_id: str
    target_market: str
    analysis_recipe_hash: str
    decisions: list[ClaimDecisionInputModel] = Field(min_length=1)
    copy_policy_snapshot: dict = Field(default_factory=dict)
    source_locale: str
    target_locale: str
    translation_recipe: TranslationRecipeInput | None = None
    requested_evidence_tier: Literal["mock", "sandbox", "production"]


class ApproveCopyInput(BaseModel):
    copy_version_id: str


class ConfirmInput(BaseModel):
    edit_provider: str
    edit_model: str
    edit_adapter_version: str
    external_qc_steps: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=3, ge=1, le=3)


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------


@router.post("/batches", status_code=201)
def create_draft(
    body: CreateDraftInput,
    idempotency_key: str = Depends(_idempotency_key),
    context: TenantContext = Depends(request_tenant_context),
):
    del idempotency_key  # required for router-contract consistency; batch.py
    # dedupes on its own semantic-key hash over business inputs (see module
    # docstring), never on this header.

    with tenant_session(context) as session:
        result = batch_module.create_draft(
            session,
            context,
            product_version_id=body.product_version_id,
            offer_version_id=body.offer_version_id,
            truth_data_mode=body.truth_data_mode,
            provider_execution_mode=body.provider_execution_mode,
            sources=[BatchSourceInput(**s.model_dump()) for s in body.sources],
            market_targets=[
                BatchMarketTargetInput(**t.model_dump()) for t in body.market_targets
            ],
            requested_evidence_tier=body.requested_evidence_tier,
            actor_id=_actor_id(context),
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# get_batch
# ---------------------------------------------------------------------------


def _load_batch_projection(session, context: TenantContext, batch_id: str) -> tuple[dict, int]:
    batch = session.scalar(
        select(LocalizationBatchRecord).where(
            LocalizationBatchRecord.workspace_id == context.workspace_id,
            LocalizationBatchRecord.id == batch_id,
        )
    )
    if batch is None:
        raise HTTPException(
            404, {"code": "localization_not_found", "message": f"batch {batch_id} not found"}
        )
    sources = session.scalars(
        select(LocalizationBatchSourceRecord).where(
            LocalizationBatchSourceRecord.workspace_id == context.workspace_id,
            LocalizationBatchSourceRecord.batch_id == batch_id,
        )
    ).all()
    targets = session.scalars(
        select(LocalizationBatchMarketTargetRecord).where(
            LocalizationBatchMarketTargetRecord.workspace_id == context.workspace_id,
            LocalizationBatchMarketTargetRecord.batch_id == batch_id,
        )
    ).all()
    items = session.scalars(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == context.workspace_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    ).all()
    data = {
        "id": batch.id,
        "phase": batch.phase,
        "preparation_stage": batch.preparation_stage,
        "outcome": batch.outcome,
        "truth_freshness": batch.truth_freshness,
        "product_version_id": batch.product_version_id,
        "offer_version_id": batch.offer_version_id,
        "source_market_snapshot": batch.source_market_snapshot_json,
        "truth_package_id": batch.truth_package_id,
        "truth_data_mode": batch.truth_data_mode,
        "provider_execution_mode": batch.provider_execution_mode,
        "evidence_tier": batch.evidence_tier,
        "max_attempts": batch.max_attempts,
        "revision": batch.revision,
        "frozen_at": batch.frozen_at.isoformat() if batch.frozen_at else None,
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
        "sources": [
            {
                "asset_version_id": s.asset_version_id,
                "decision_id": s.decision_id,
                "source_generation_mode": s.source_generation_mode,
                "experiment_evidence_mode": s.experiment_evidence_mode,
            }
            for s in sources
        ],
        "market_targets": [
            {"market": t.market, "market_policy_version_id": t.market_policy_version_id}
            for t in targets
        ],
        "items": [
            {
                "id": i.id,
                "state": i.state,
                "target_market": i.target_market,
                "source_asset_version_id": i.source_asset_version_id,
                "attempt_count": i.attempt_count,
                "reason_code": i.reason_code,
                "reason_detail": i.reason_detail,
                "recoverable": i.recoverable,
                "next_action": i.next_action,
                "revision": i.revision,
            }
            for i in items
        ],
    }
    return data, batch.revision


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        data, revision = _load_batch_projection(session, context, batch_id)
    set_etag(response, revision)
    return data


# ---------------------------------------------------------------------------
# start_preparation
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/start-preparation")
def start_preparation(
    batch_id: str,
    body: StartPreparationInput,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        result = batch_module.start_preparation(
            session,
            context,
            batch_id=batch_id,
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
            actor_id=_actor_id(context),
            admission=_build_admission(),
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# submit_claim_decisions
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/claim-decisions")
def submit_claim_decisions(
    batch_id: str,
    body: SubmitClaimDecisionsInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
):
    translation_recipe = None
    if body.translation_recipe is not None:
        translation_recipe = TranslationRecipe(**body.translation_recipe.model_dump())
    tier = tier_of_experiment(body.requested_evidence_tier)

    with tenant_session(context) as session:
        result = batch_module.submit_claim_decisions(
            session,
            context,
            batch_id=batch_id,
            expected_revision=extract_if_match(request),
            asset_version_id=body.asset_version_id,
            target_market=body.target_market,
            analysis_recipe_hash=body.analysis_recipe_hash,
            decisions=[ClaimDecisionInput(**d.model_dump()) for d in body.decisions],
            copy_policy_snapshot=body.copy_policy_snapshot,
            source_locale=body.source_locale,
            target_locale=body.target_locale,
            translation_recipe=translation_recipe,
            requested_evidence_tier=tier,
            actor_id=_actor_id(context),
            role=context.role,
            admission=_build_admission(),
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# approve_copy
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/approve-copy")
def approve_copy(
    batch_id: str,
    body: ApproveCopyInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        result = batch_module.approve_copy(
            session,
            context,
            batch_id=batch_id,
            expected_revision=extract_if_match(request),
            copy_version_id=body.copy_version_id,
            actor_id=_actor_id(context),
            role=context.role,
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/confirm")
def confirm(
    batch_id: str,
    body: ConfirmInput,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
):
    expected_revision = extract_if_match(request)

    with tenant_session(context) as session:
        result = batch_module.confirm(
            session,
            context,
            batch_id=batch_id,
            expected_revision=expected_revision,
            edit_provider=body.edit_provider,
            edit_model=body.edit_model,
            edit_adapter_version=body.edit_adapter_version,
            external_qc_steps=tuple(body.external_qc_steps),
            max_attempts=body.max_attempts,
            actor_id=_actor_id(context),
            role=context.role,
            admission=_build_admission(),
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# retry_item
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/items/{item_id}/retry")
def retry_item(
    batch_id: str,
    item_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
):
    expected_item_revision = extract_if_match(request)

    with tenant_session(context) as session:
        result = batch_module.retry_item(
            session,
            context,
            batch_id=batch_id,
            item_id=item_id,
            expected_item_revision=expected_item_revision,
            actor_id=_actor_id(context),
            role=context.role,
            admission=_build_admission(),
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/cancel")
def cancel(
    batch_id: str,
    request: Request,
    context: TenantContext = Depends(request_tenant_context),
):
    expected_revision = extract_if_match(request)

    with tenant_session(context) as session:
        result = batch_module.cancel(
            session,
            context,
            batch_id=batch_id,
            expected_revision=expected_revision,
            actor_id=_actor_id(context),
        )
        return asdict(result)


# ---------------------------------------------------------------------------
# get_batch_outcome
# ---------------------------------------------------------------------------


@router.get("/batches/{batch_id}/outcome")
def get_batch_outcome(
    batch_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        batch = session.scalar(
            select(LocalizationBatchRecord).where(
                LocalizationBatchRecord.workspace_id == context.workspace_id,
                LocalizationBatchRecord.id == batch_id,
            )
        )
        if batch is None:
            raise HTTPException(
                404, {"code": "localization_not_found", "message": f"batch {batch_id} not found"}
            )
        items = session.scalars(
            select(LocalizationItemRecord).where(
                LocalizationItemRecord.workspace_id == context.workspace_id,
                LocalizationItemRecord.batch_id == batch_id,
            )
        ).all()
        state_counts: dict[str, int] = {}
        for item in items:
            state_counts[item.state] = state_counts.get(item.state, 0) + 1
        return {
            "batch_id": batch.id,
            "phase": batch.phase,
            "outcome": batch.outcome,
            "revision": batch.revision,
            "item_count": len(items),
            "item_state_counts": state_counts,
        }


# ---------------------------------------------------------------------------
# list_eligible_sources
# ---------------------------------------------------------------------------


@router.get("/eligible-sources")
def list_eligible_sources(
    requested_evidence_tier: str | None = None,
    context: TenantContext = Depends(request_tenant_context),
):
    with tenant_session(context) as session:
        winners = eligibility_module.list_eligible_winners(
            session, context, requested_evidence_tier=requested_evidence_tier
        )
        return {"items": [asdict(w) for w in winners]}
