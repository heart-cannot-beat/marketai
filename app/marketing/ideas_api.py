"""Authenticated, idempotent v1 endpoints for ads-first ideas (ADR 0018, #189).

The plan-first endpoints in ``api.py`` never return these projects, and these never return plan-first ones.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import and_, or_, select

from app.adapters.persistence.database import tenant_session
from app.generation.v1_router import _idempotency_key, _resource_command
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.platform.tenancy import TenantContext
from . import ideas, service
from .ideas_contracts import (CreateIdeas, IdeasCallSummary, IdeasGenerationDrafts, IdeasProjectDetail, IdeasProjectPage,
                              PrepareIdeasGeneration, SetKept, StartIdeasPlanning)
from .models import MarketingProjectRecord, PlanningCallRecord

router = APIRouter(prefix="/marketing/ideas", tags=["marketing"])


@router.post("", operation_id="create_marketing_ideas", response_model=IdeasProjectDetail, status_code=201,
             openapi_extra={"x-marketai-idempotent": True})
def create_ideas(body: CreateIdeas, request: Request, response: Response,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Open ads-first ideas for a product of this workspace; drafting starts later (S2)."""
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.create", body.model_dump(mode="json"),
            lambda s: ideas.create_ideas_project(s, ctx, product_id=body.product_id, source_text=body.source_text,
                                                 market=body.market, ad_language=body.ad_language),
            lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.get("", operation_id="list_marketing_ideas", response_model=IdeasProjectPage)
def list_ideas(ctx: TenantContext = Depends(request_tenant_context), limit: int = Query(default=50, ge=1, le=100),
               cursor: str | None = Query(default=None, max_length=2048)):
    """This workspace's ads-first ideas, newest first."""
    filters = "marketing-ideas-created-v1"
    cls = MarketingProjectRecord
    with tenant_session(ctx) as session:
        query = select(cls).where(cls.workspace_id == ctx.workspace_id, cls.flow == ideas.FLOW)
        if cursor:
            try:
                position = decode_cursor(cursor, ctx.workspace_id, filters)
                if len(position) != 2 or not all(isinstance(v, str) for v in position) or not 1 <= len(position[1]) <= 64:
                    raise ValueError("invalid position")
                stamp = datetime.fromisoformat(position[0])
                query = query.where(or_(cls.created_at < stamp, and_(cls.created_at == stamp, cls.id < position[1])))
            except (InvalidCursorError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise service.MarketingError("invalid_cursor", "列表位置无效，请从第一页重新加载。", 400) from None
        rows = session.scalars(query.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit + 1)).all()
        more, rows = len(rows) > limit, rows[:limit]
        return {"items": ideas.ideas_summaries(session, ctx, rows),
                "next_cursor": encode_cursor(ctx.workspace_id, filters, (rows[-1].created_at.isoformat(), rows[-1].id))
                if more else None}


@router.get("/{project_id}", operation_id="get_marketing_ideas", response_model=IdeasProjectDetail)
def get_ideas(project_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    with tenant_session(ctx) as session:
        row = ideas.get_ideas_project(session, ctx, project_id)
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.put("/{project_id}/ads/{asset_version_id}/kept", operation_id="set_marketing_ad_kept",
            response_model=IdeasProjectDetail, openapi_extra={"x-marketai-idempotent": True})
def set_ad_kept(project_id: str, asset_version_id: str, body: SetKept, request: Request, response: Response,
                ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Keep an ad of these ideas, or stop keeping it; each change is audited."""
    def keep(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas.set_kept(s, ctx, row, asset_version_id, body.kept)
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.keep", body.model_dump(mode="json"), keep,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/planning", operation_id="start_marketing_ideas_planning", response_model=IdeasCallSummary,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def start_ideas_planning(project_id: str, body: StartIdeasPlanning, request: Request,
                         ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                         if_match: str | None = Header(default=None, alias="If-Match")):
    """Draft the product understanding, or the ideas from it; no fact needs checking first (ADR 0018, #190)."""
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def replay(s, resource):
            call = s.scalar(select(PlanningCallRecord).where(PlanningCallRecord.id == resource,
                                                           PlanningCallRecord.workspace_id == ctx.workspace_id))
            if call is None:
                raise service.MarketingError("planning_not_found", "找不到策划任务。", 404)
            return call
        call = _resource_command(session, ctx, request, key, "marketing.ideas.plan", body.model_dump(mode="json"),
            lambda s: ideas.start_ideas_planning(s, ctx, ideas.get_ideas_project(s, ctx, project_id), revision, body.kind,
                                                 body.execution_mode, body.quote_hash, body.strategy_language), replay)
        return service.call_dict(call, include_maximum_cost=True)


@router.post("/{project_id}/generation-drafts", operation_id="prepare_marketing_ideas_generation",
             response_model=IdeasGenerationDrafts, status_code=201, openapi_extra={"x-marketai-idempotent": True})
def prepare_ideas_generation(project_id: str, body: PrepareIdeasGeneration, request: Request,
                             ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                             if_match: str | None = Header(default=None, alias="If-Match")):
    """One generation draft per ad of these ideas; no fact or plan needs confirming first (ADR 0018, #191).

    Preview and submit each draft with ``create_generation_preview`` and ``submit_generation_run``; the
    images then appear under the ideas' ``ads`` (``pending`` while they are being made)."""
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def prepare(s):
            row = ideas.get_ideas_project(s, ctx, project_id)
            ideas.generation_drafts(s, ctx, row, revision, body.provider, body.model,
                                    {"aspect_ratio": body.aspect_ratio, "image_size": body.image_size, "variants": body.variants})
            return row
        row = _resource_command(session, ctx, request, key, "marketing.ideas.prepare_generation", body.model_dump(mode="json"),
                                prepare, lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        return {"project_id": row.id, "revision": revision, "drafts": ideas.prepared_drafts(session, ctx, row, revision)}
