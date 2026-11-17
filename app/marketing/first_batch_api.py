"""Authenticated v1 endpoints of the ads-first first batch (#193, ADR 0018; design doc §5.1, §5.2, §8).

The quote is the start button's maximum; starting is the one confirmation of it. Reading the batch
shows its step, why it stopped and what continuing would cost; continuing confirms that cost. Owner and
Member may do all four, as for the product intake they build on.
"""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.adapters.persistence.database import tenant_session
from app.generation.v1_router import _idempotency_key, _resource_command
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from . import first_batch, service
from .ideas_contracts import FirstBatchDetail, FirstBatchQuote, ResumeFirstBatch, StartFirstBatch

router = APIRouter(prefix="/marketing/first-batch", tags=["marketing"])
#: The quote sits beside the batches, not under them: the BFF allowlist matches routes in operation-id
#: order, and ``/first-batch/quote`` would be read as the batch whose id is "quote".
quote_router = APIRouter(prefix="/marketing", tags=["marketing"])


def _member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise service.MarketingError("first_batch_forbidden", "只有工作区成员可以开始。", 403)


@quote_router.get("/first-batch-quote", operation_id="get_marketing_first_batch_quote", response_model=FirstBatchQuote)
def get_quote(ctx: TenantContext = Depends(request_tenant_context),
              execution_mode: Literal["byok", "mock"] = Query(default="byok")):
    """The start button's maximum: read, understand, draft the ideas (and once more if refused), six images."""
    _member(ctx)
    with tenant_session(ctx) as session:
        return first_batch.quote(session, ctx, execution_mode)


@router.post("", operation_id="start_marketing_first_batch", response_model=FirstBatchDetail, status_code=201,
             openapi_extra={"x-marketai-idempotent": True})
def start(body: StartFirstBatch, request: Request,
          ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Start the first batch; the same input started again within 24 hours returns the same batch."""
    _member(ctx)
    with tenant_session(ctx) as session:
        batch = _resource_command(session, ctx, request, key, "marketing.first_batch.start", body.model_dump(mode="json"),
            lambda s: first_batch.start(s, ctx, url=body.url, upload_intent_ids=body.upload_intent_ids, text=body.text,
                                        market=body.market, ad_language=body.ad_language,
                                        strategy_language=body.strategy_language, execution_mode=body.execution_mode,
                                        quote_hash=body.quote_hash),
            lambda s, resource: first_batch.get(s, ctx, resource))
        # No Location header: the web proxy answers 502 to one its contract policy does not declare, and
        # the body already carries the batch id (#216).
        return first_batch.detail(session, ctx, batch)


@router.get("/{first_batch_id}", operation_id="get_marketing_first_batch", response_model=FirstBatchDetail)
def get_batch(first_batch_id: str, ctx: TenantContext = Depends(request_tenant_context)):
    _member(ctx)
    with tenant_session(ctx) as session:
        return first_batch.detail(session, ctx, first_batch.get(session, ctx, first_batch_id))


@router.post("/{first_batch_id}/resume", operation_id="resume_marketing_first_batch", response_model=FirstBatchDetail,
             openapi_extra={"x-marketai-idempotent": True})
def resume(first_batch_id: str, body: ResumeFirstBatch, request: Request,
           ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Continue after a stop: choose the model, add material, or accept the new maximum."""
    _member(ctx)
    with tenant_session(ctx) as session:
        batch = _resource_command(session, ctx, request, key, "marketing.first_batch.resume", body.model_dump(mode="json"),
            lambda s: first_batch.resume(s, ctx, first_batch.get(s, ctx, first_batch_id, lock=True),
                                         quote_hash=body.quote_hash, selected_variant=body.selected_variant,
                                         additional_text=body.additional_text),
            lambda s, resource: first_batch.get(s, ctx, resource))
        return first_batch.detail(session, ctx, batch)
