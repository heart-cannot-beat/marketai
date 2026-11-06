"""Authenticated, idempotent v1 adapter for marketing planning."""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import Field
from sqlalchemy import select, and_, or_, func
from datetime import datetime
from app.platform.pagination import encode_cursor, decode_cursor, InvalidCursorError

from app.adapters.persistence.database import tenant_session
from app.generation.v1_router import _resource_command, _idempotency_key, _blob_store
from app.generation.thumbnails import attach_thumbnail_links
from app.generation.api import _request_problem
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from . import service
from .contracts import Document, Understanding, TestPlan, MarketingProjectDetail, MarketingProjectPage, PlanningCallSummary, PlannerCapabilities
from .models import MarketingProjectRecord, PlanningCallRecord

router = APIRouter(prefix="/marketing", tags=["marketing"])

from . import recovery
from .recovery import ResolveCall, ResolutionSummary
from app.platform.operations import get_operation


@router.post('/calls/{call_id}/resolve', operation_id='resolve_marketing_call',
             response_model=ResolutionSummary, status_code=202,
             openapi_extra={'x-marketai-idempotent': True})
def resolve_call(call_id: str, body: ResolveCall, request: Request,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    with tenant_session(ctx) as session:
        op = _resource_command(session, ctx, request, key, 'marketing.resolve_call', body.model_dump(mode='json'),
            lambda s: recovery.admit(s, ctx, call_id, body), lambda s, resource: get_operation(s, resource))
        return {'id': op.id, 'operation_id': op.id, 'call_id': call_id, 'state': op.state}


class CreateProject(Document):
    source_text: str = Field(default="", max_length=16000)
    product_id: str | None = Field(default=None, max_length=64)


class EditProject(Document):
    command: Literal["save_understanding", "confirm_understanding", "save_plan", "confirm_plan", "revise_source"]
    understanding: Understanding | None = None
    plan: TestPlan | None = None
    source_text: str | None = Field(default=None, max_length=16000)


class PlanRequest(Document):
    kind: Literal["understanding", "plan"]
    ad_language: str | None = Field(default=None, min_length=2, max_length=35, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    direction: Literal["screen_angles", "match_expressions", "confirm_combination", "optimize_elements"] | None = None
    decision_question: str = Field(default="", max_length=1200)
    execution_mode: Literal["byok", "mock"]
    quote_hash: str = Field(default="", max_length=64)


from .product_references import ProductReferenceSelection


class PrepareGeneration(Document):
    product_references: ProductReferenceSelection = Field(default_factory=ProductReferenceSelection)
    provider: Literal["gemini", "openai", "seedream", "mock"]
    model: str = Field(min_length=1, max_length=200)
    aspect_ratio: Literal["1:1", "4:5", "9:16", "16:9"] = "4:5"
    image_size: Literal["1K", "2K"] = "1K"
    # Omit to generate each combination's planned executions; a value overrides every combination (pre-#225 clients).
    variants: int | None = Field(default=None, ge=1, le=4)


def register_handlers(app):
    @app.exception_handler(service.MarketingError)
    async def marketing_error(request, exc):
        return _request_problem(request, exc.status, exc.code, exc.detail, params=exc.params,
                                **({"field_errors": exc.field_errors} if exc.field_errors else {}))


@router.get("/capabilities", operation_id="get_marketing_capabilities", response_model=PlannerCapabilities,
            response_model_exclude_unset=True)
def capabilities(ctx: TenantContext = Depends(request_tenant_context), include_reporting: bool = False):
    with tenant_session(ctx) as session:
        result = {"paid": service.planner_capability(session, ctx), "mock_available": True}
        if include_reporting:
            from .reporting import MEDIA_CURRENCIES, ATTRIBUTION_WINDOWS, MIN_ATTRIBUTION_LAG
            result.update(media_currencies=MEDIA_CURRENCIES, attribution_windows=ATTRIBUTION_WINDOWS,
                          attribution_min_lag_days=MIN_ATTRIBUTION_LAG)
        return result


@router.get("/projects", operation_id="list_marketing_projects", response_model=MarketingProjectPage,
            response_model_exclude_unset=True)
def list_projects(request: Request, ctx: TenantContext = Depends(request_tenant_context),
                  limit: int = Query(default=50, ge=1, le=100),
                  cursor: str | None = Query(default=None, max_length=2048),
                  q: str = Query(default='', max_length=200)):
    search = q.strip().lower()
    filters = 'marketing-projects-created-v1:' + search
    with tenant_session(ctx) as session:
        cls = MarketingProjectRecord
        query = select(cls).where(cls.workspace_id == ctx.workspace_id)
        if search:
            query = query.where(or_(func.lower(cls.source_text).contains(search, autoescape=True),
                func.lower(cls.understanding_json['product_name'].as_string()).contains(search, autoescape=True),
                func.lower(cls.id).contains(search, autoescape=True)))
        if cursor:
            try:
                position = decode_cursor(cursor, ctx.workspace_id, filters)
                if len(position) != 2 or not all(isinstance(v, str) for v in position) or not 1 <= len(position[1]) <= 64:
                    raise ValueError('invalid position')
                stamp = datetime.fromisoformat(position[0])
                query = query.where(or_(cls.created_at < stamp, and_(cls.created_at == stamp, cls.id < position[1])))
            except (InvalidCursorError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise service.MarketingError('invalid_cursor', '列表位置无效或搜索条件已变化，请从第一页重新加载。', 400) from None
        rows = session.scalars(query.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit + 1)).all()
        more, rows = len(rows) > limit, rows[:limit]
        result = {"items": [{"id": row.id, "revision": row.revision, "state": row.state,
                           "product_name": (row.understanding_json or {}).get("product_name", row.source_text[:80])}
                          for row in rows]}
        # Old BFF releases validate exact top-level fields. Opt into the new
        # projection via pagination/search parameters to permit rolling deploys.
        if any(key in request.query_params for key in ('limit', 'cursor', 'q')):
            result['next_cursor'] = encode_cursor(ctx.workspace_id, filters,
                (rows[-1].created_at.isoformat(), rows[-1].id)) if more else None
        return result


@router.get("/projects/{project_id}", operation_id="get_marketing_project", response_model_exclude_unset=True, response_model=MarketingProjectDetail)
def get_project(project_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context), include_recovery: bool = False, include_languages: bool = False, include_plan_review: bool = False, include_brand_style: bool = False, include_execution_plan: bool = False, include_competitor_review: bool = False):
    with tenant_session(ctx) as session:
        row = service.get_project(session, ctx, project_id)
        set_etag(response, row.revision)
        return service.project_dict(session, ctx, row, include_recovery=include_recovery, include_languages=include_languages, include_plan_review=include_plan_review, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_competitor_review=include_competitor_review)


@router.post("/projects", operation_id="create_marketing_project", response_model_exclude_unset=True, response_model=MarketingProjectDetail,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def create_project(body: CreateProject, request: Request, ctx: TenantContext = Depends(request_tenant_context),
                   key: str = Depends(_idempotency_key), include_languages: bool = False, include_plan_review: bool = False,
                   include_brand_style: bool = False, include_execution_plan: bool = False, include_competitor_review: bool = False):
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.create", body.model_dump(mode="json"),
            lambda s: service.create_project(s, ctx, body.source_text, body.product_id),
            lambda s, resource: service.get_project(s, ctx, resource))
        return service.project_dict(session, ctx, row, include_languages=include_languages, include_plan_review=include_plan_review, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_competitor_review=include_competitor_review)


@router.patch("/projects/{project_id}", operation_id="edit_marketing_project", response_model_exclude_unset=True, response_model=MarketingProjectDetail,
              openapi_extra={"x-marketai-idempotent": True})
def edit_project(project_id: str, body: EditProject, request: Request, response: Response,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                 if_match: str | None = Header(default=None, alias="If-Match"), include_languages: bool = False, include_plan_review: bool = False,
                 include_brand_style: bool = False, include_execution_plan: bool = False, include_competitor_review: bool = False):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.edit", body.model_dump(mode="json"),
            lambda s: service.edit_project(s, ctx, service.get_project(s, ctx, project_id), revision,
                body.command, body.understanding, body.plan, body.source_text), lambda s, resource: service.get_project(s, ctx, resource))
        set_etag(response, row.revision)
        return service.project_dict(session, ctx, row, include_languages=include_languages, include_plan_review=include_plan_review, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_competitor_review=include_competitor_review)


@router.post("/projects/{project_id}/planning", operation_id="start_marketing_planning", response_model_exclude_unset=True, response_model=PlanningCallSummary,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def start_planning(project_id: str, body: PlanRequest, request: Request,
                   ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                   if_match: str | None = Header(default=None, alias="If-Match")):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def replay(s, resource):
            call = s.scalar(select(PlanningCallRecord).where(PlanningCallRecord.id == resource,
                                                           PlanningCallRecord.workspace_id == ctx.workspace_id))
            if call is None:
                raise service.MarketingError("planning_not_found", "找不到策划任务。", 404)
            return call
        call = _resource_command(session, ctx, request, key, "marketing.plan", body.model_dump(mode="json"),
            lambda s: service.start_planning(s, ctx, service.get_project(s, ctx, project_id), revision,
                                             body.kind, body.execution_mode, body.quote_hash, body.direction, body.decision_question, body.ad_language, body.strategy_language), replay)
        return service.call_dict(call)


@router.post("/projects/{project_id}/generation-drafts", operation_id="prepare_marketing_generation",
             response_model_exclude_unset=True, response_model=MarketingProjectDetail, status_code=201, openapi_extra={"x-marketai-idempotent": True})
def prepare_generation(project_id: str, body: PrepareGeneration, request: Request,
                       ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                       if_match: str | None = Header(default=None, alias="If-Match"), include_languages: bool = False, include_plan_review: bool = False,
                       include_brand_style: bool = False, include_execution_plan: bool = False, include_competitor_review: bool = False):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def create(s):
            row = service.get_project(s, ctx, project_id)
            service.generation_drafts(s, ctx, row, revision, body.provider, body.model,
                {"aspect_ratio": body.aspect_ratio, "image_size": body.image_size, "variants": body.variants},
                body.product_references.model_dump(mode="json"))
            return row
        row = _resource_command(session, ctx, request, key, "marketing.prepare_generation", body.model_dump(mode="json"),
            create, lambda s, resource: service.get_project(s, ctx, resource))
        return service.project_dict(session, ctx, row, include_languages=include_languages, include_plan_review=include_plan_review, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_competitor_review=include_competitor_review)

from . import brand_kits
from .contracts import BrandKitInput, BrandKitList, ProductBrandKit


@router.get("/brand-kits", operation_id="list_marketing_brand_kits", response_model=BrandKitList)
def list_brand_kits(ctx: TenantContext = Depends(request_tenant_context)):
    """Each product's current brand baseline, for choosing the exact version a plan freezes."""
    with tenant_session(ctx) as session:
        return brand_kits.list_current(session, ctx)


@router.get("/products/{product_id}/brand-kit", operation_id="get_product_brand_kit", response_model=ProductBrandKit,
            response_model_exclude_unset=True)
def get_brand_kit(product_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    with tenant_session(ctx) as session:
        result = brand_kits.get(session, ctx, product_id)
        set_etag(response, result["revision"])
        return result


@router.put("/products/{product_id}/brand-kit", operation_id="save_product_brand_kit", response_model=ProductBrandKit,
            response_model_exclude_unset=True, openapi_extra={"x-marketai-idempotent": True})
def save_brand_kit(product_id: str, body: BrandKitInput, request: Request, response: Response,
                   ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                   if_match: str | None = Header(default=None, alias="If-Match")):
    """Append the next immutable version (If-Match: the current version, 0 for the first).

    Plans, recipes and QC keep the exact version they reference; this never rewrites them."""
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        _resource_command(session, ctx, request, key, "marketing.brand_kit", body.model_dump(mode="json"),
            lambda s: brand_kits.save(s, ctx, product_id, revision, body), lambda s, resource: brand_kits.get_row(s, ctx, resource))
        result = brand_kits.get(session, ctx, product_id)
        set_etag(response, result["revision"])
        return result


# Execution endpoints share tenant, idempotency and optimistic concurrency seams.
from . import workflow, metrics
from .workflow_contracts import (WorkflowEnvelope, ResourceEnvelope, FreezeRound, ReviewCreative,
    CreateHandoff, BindDelivery, PreviewMetrics, BatchCommand, CloseRound)
from app.generation.response_models import DownloadIntentResponse


def _board_thumbnail_refs(session, ctx, round_data, wanted):
    """Opt-in like the other strict-BFF flags: without it the board carries no `thumbnail` key and nothing is signed."""
    return workflow.board_thumbnail_refs(session, ctx, round_data["board"]) if wanted and round_data and "board" in round_data else None


async def _sign_board_thumbnails(round_data, refs):
    """Sign each generated execution's 512px derivative once the session has closed; the rest get `thumbnail: null`."""
    if refs is not None:
        from .board import execution_nodes
        await attach_thumbnail_links(_blob_store, execution_nodes(round_data["board"]), refs, version_id_key="asset_version_id")


@router.get("/projects/{project_id}/workflow", operation_id="get_marketing_workflow", response_model=WorkflowEnvelope, response_model_exclude_unset=True)
async def get_workflow(project_id: str, ctx: TenantContext = Depends(request_tenant_context), include_polling: bool = False, include_image_edits: bool = False, include_version_history: bool = False, include_languages: bool = False, include_plan_review: bool = False, include_board: bool = False, include_brand_style: bool = False, include_execution_plan: bool = False, include_qc_kinds: bool = False, include_board_thumbnails: bool = False, include_text_language: bool = False, include_qc_evidence: bool = False):
    def load():
        with tenant_session(ctx) as session:
            result = workflow.workflow_dict(session, ctx, project_id, include_image_edits=include_image_edits, include_version_history=include_version_history, include_languages=include_languages, include_plan_review=include_plan_review, include_board=include_board, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_qc_kinds=include_qc_kinds, include_text_language=include_text_language, include_qc_evidence=include_qc_evidence)
            return result, _board_thumbnail_refs(session, ctx, result["current"], include_board_thumbnails)
    result, refs = await run_in_threadpool(load)
    await _sign_board_thumbnails(result["current"], refs)
    if not include_polling:
        result.pop("poll_after_ms", None)
    return result


@router.get("/rounds/{round_id}", operation_id="get_marketing_round", response_model=ResourceEnvelope, response_model_exclude_unset=True)
async def get_round(round_id: str, response: Response, include_image_edits: bool = False, include_version_history: bool = False, include_languages: bool = False, include_plan_review: bool = False, include_board: bool = False, include_brand_style: bool = False, include_execution_plan: bool = False, include_qc_kinds: bool = False, include_board_thumbnails: bool = False, include_text_language: bool = False, include_qc_evidence: bool = False, ctx: TenantContext = Depends(request_tenant_context)):
    def load():
        with tenant_session(ctx) as session:
            row = workflow.get_round(session, ctx, round_id)
            data = workflow.round_dict(session, ctx, row, include_image_edits=include_image_edits, include_version_history=include_version_history, include_languages=include_languages, include_plan_review=include_plan_review, include_board=include_board, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_qc_kinds=include_qc_kinds, include_text_language=include_text_language, include_qc_evidence=include_qc_evidence)
            return row.id, row.revision, data, _board_thumbnail_refs(session, ctx, data, include_board_thumbnails)
    row_id, revision, data, refs = await run_in_threadpool(load)
    set_etag(response, revision)
    await _sign_board_thumbnails(data, refs)
    return {"id": row_id, "data": data}


@router.post("/projects/{project_id}/rounds", operation_id="freeze_marketing_round", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def freeze_round(project_id: str, body: FreezeRound, request: Request, response: Response,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                 if_match: str | None = Header(default=None, alias="If-Match")):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.freeze", body.model_dump(mode="json"),
            lambda s: workflow.freeze(s, ctx, service.get_project(s, ctx, project_id), revision, body),
            lambda s, rid: workflow.get_round(s, ctx, rid))
        set_etag(response, row.revision)
        return {"id": row.id, "data": workflow.round_dict(session, ctx, row)}


CLOSED_EVIDENCE_COMMANDS = frozenset({"marketing.bind", "marketing.metrics_preview", "marketing.metrics_confirm",
    "marketing.evaluate", "marketing.explanation"})


def execution_command(round_id, body, request, response, ctx, key, name, action, *, include_image_edits=False):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def run(s):
            row = workflow.get_round(s, ctx, round_id)
            workflow.mutate(s, ctx, row, revision, allow_closed_evidence=name in CLOSED_EVIDENCE_COMMANDS)
            action(s, ctx, row, body) if body is not None else action(s, ctx, row)
            s.flush()
            return row
        row = _resource_command(session, ctx, request, key, name,
            body.model_dump(mode="json") if body else None, run, lambda s, rid: workflow.get_round(s, ctx, rid))
        set_etag(response, row.revision)
        return {"id": row.id, "data": workflow.round_dict(session, ctx, row, include_image_edits=include_image_edits)}


from .reuse import ReuseCreative, attach as attach_creative
from . import feedback, explanation
from .feedback import PrepareFeedback
from .explanation import RequestExplanation


@router.post("/rounds/{round_id}/feedback-drafts", operation_id="prepare_marketing_feedback",
             response_model=ResourceEnvelope, response_model_exclude_unset=True, status_code=201, openapi_extra={"x-marketai-idempotent": True})
def prepare_feedback(round_id: str, body: PrepareFeedback, request: Request, response: Response,
                     ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                     if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.feedback", feedback.prepare, include_image_edits=body.image_tool is not None)


@router.post("/rounds/{round_id}/explanation", operation_id="start_marketing_explanation",
             response_model=ResourceEnvelope, response_model_exclude_unset=True, status_code=202, openapi_extra={"x-marketai-idempotent": True})
def start_explanation(round_id: str, body: RequestExplanation, request: Request, response: Response,
                      ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                      if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.explanation", explanation.start)


@router.post("/rounds/{round_id}/creatives/reuse", operation_id="reuse_marketing_creative", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def reuse_creative(round_id: str, body: ReuseCreative, request: Request, response: Response,
                  ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                  if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.reuse", attach_creative)


@router.post("/rounds/{round_id}/reviews", operation_id="review_marketing_creative", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def review_creative(round_id: str, body: ReviewCreative, request: Request, response: Response,
                    ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                    if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.review", workflow.review)


@router.post("/rounds/{round_id}/handoff", operation_id="create_marketing_handoff", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def create_handoff(round_id: str, body: CreateHandoff, request: Request, response: Response,
                   ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                   if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.handoff", workflow.create_handoff)


@router.post("/rounds/{round_id}/handoff/retry", operation_id="retry_marketing_handoff", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def retry_handoff(round_id: str, request: Request, response: Response,
                   ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                   if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, None, request, response, ctx, key, "marketing.retry_handoff", workflow.retry_handoff)


@router.post("/rounds/{round_id}/bindings", operation_id="bind_marketing_delivery", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def bind_delivery(round_id: str, body: BindDelivery, request: Request, response: Response,
                  ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                  if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.bind", workflow.bind)


@router.post("/rounds/{round_id}/metrics/preview", operation_id="preview_marketing_metrics", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def preview_metrics(round_id: str, body: PreviewMetrics, request: Request, response: Response,
                    ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                    if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.metrics_preview", metrics.preview)


@router.post("/rounds/{round_id}/metrics/confirm", operation_id="confirm_marketing_metrics", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             openapi_extra={"x-marketai-idempotent": True})
def confirm_metrics(round_id: str, body: BatchCommand, request: Request, response: Response,
                    ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                    if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.metrics_confirm", metrics.confirm)


@router.post("/rounds/{round_id}/evaluate", operation_id="evaluate_marketing_round", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             openapi_extra={"x-marketai-idempotent": True})
def evaluate_round(round_id: str, request: Request, response: Response,
                   ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                   if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, None, request, response, ctx, key, "marketing.evaluate", workflow.evaluate)


@router.post("/rounds/{round_id}/close", operation_id="close_marketing_round", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             openapi_extra={"x-marketai-idempotent": True})
def close_round(round_id: str, body: CloseRound, request: Request, response: Response,
                ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.close", workflow.close)


@router.post("/rounds/{round_id}/download-intent", operation_id="download_marketing_handoff", response_model=DownloadIntentResponse,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
async def download_handoff(round_id: str, request: Request, ctx: TenantContext = Depends(request_tenant_context),
                           key: str = Depends(_idempotency_key)):
    from app.platform.download_intents import create_download_intent_record, get_download_intent
    from app.platform.blob_store import CommittedBlobRef, DownloadSpec
    from app.platform.blob_store_factory import build_blob_store
    from app.adapters.persistence.models import new_id, utcnow
    from app.generation.preview import _as_aware
    with tenant_session(ctx) as session:
        row = workflow.get_round(session, ctx, round_id)
        handoffs = workflow.scoped(session, ctx, workflow.Handoff, round_id=row.id, state="ready")
        if not handoffs:
            raise service.MarketingError("handoff_not_ready", "导出包尚未就绪。", 409)
        handoff = handoffs[0]
        if not workflow.handoff_quality_current(session, ctx, row, handoff):
            raise service.MarketingError("handoff_quality_changed", "导出素材的审核或参考资格已变化，暂不能获取新下载链接；原包保留为历史，请先复核。", 409)
        ref = CommittedBlobRef(**handoff.blob_json)
        record = _resource_command(session, ctx, request, key, "marketing.download", None,
            lambda s: create_download_intent_record(s, intent_id=new_id("di"), owner_kind="marketing_handoff", owner_id=handoff.id,
                committed_blob_ref_key=ref.committed_key, committed_blob_ref_version=ref.storage_version_id),
            lambda s, rid: get_download_intent(s, rid))
        expiry = _as_aware(record.expires_at)
        remaining = int((expiry - utcnow()).total_seconds())
        if remaining <= 0:
            raise service.MarketingError("download_expired", "下载链接已过期，请重新获取。", 410)
    grant = await build_blob_store().issue_download(ref, DownloadSpec(filename="MarketAI-Meta-handoff.zip", expires_in_seconds=min(remaining, 300)))
    return {"intent_id": record.id, "expires_at": expiry.isoformat(), "download_grant": {
        "url": grant.url, "expires_at": grant.expires_at.isoformat(), "content_disposition": grant.content_disposition}}


from . import ai_qc
from .ai_qc_contracts import AIQCQuote, StartAIQC, ConfirmAIQC, QCKind, StrategyLanguage


@router.get("/rounds/{round_id}/ai-qc/quote", operation_id="get_marketing_ai_qc_quote", response_model=AIQCQuote, response_model_exclude_unset=True)
def get_ai_qc_quote(round_id: str, kind: QCKind | None = None, strategy_language: StrategyLanguage | None = None,
                    ctx: TenantContext = Depends(request_tenant_context)):
    """Preview one QC kind's cost. Without `kind` (clients from before the split) this is the single-image quote.
    `strategy_language` is the requester's interface language, used only when the round's plan has none (J-43)."""
    with tenant_session(ctx) as session:
        row = workflow.get_round(session, ctx, round_id)
        quote = ai_qc.quote_and_inputs(session, ctx, row, kind or "single", strategy_language)[0]
        return quote if kind else {k: v for k, v in quote.items() if k not in ("kind", "waiting")}


@router.post("/rounds/{round_id}/ai-qc", operation_id="start_marketing_ai_qc", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def start_ai_qc(round_id: str, body: StartAIQC, request: Request, response: Response,
                ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.start_ai_qc", ai_qc.start)


@router.post("/rounds/{round_id}/ai-qc/confirm", operation_id="confirm_marketing_ai_qc", response_model=ResourceEnvelope, response_model_exclude_unset=True,
             openapi_extra={"x-marketai-idempotent": True})
def confirm_ai_qc(round_id: str, body: ConfirmAIQC, request: Request, response: Response,
                  ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                  if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.confirm_ai_qc", ai_qc.confirm)


from . import evidence_handoff
from .handoff_contracts import EvidenceHandoffEnvelope, CreatedEvidenceHandoffEnvelope
from .evidence_handoff import CreateEvidenceHandoff


@router.get("/rounds/{round_id}/evidence-handoff", operation_id="get_marketing_evidence_handoff", response_model=EvidenceHandoffEnvelope)
def get_evidence_handoff(round_id: str, ctx: TenantContext = Depends(request_tenant_context)):
    with tenant_session(ctx) as session:
        row = workflow.get_round(session, ctx, round_id)
        return {"id": row.id, "data": evidence_handoff.options(session, ctx, row)}


@router.post("/rounds/{round_id}/evidence-handoff", operation_id="create_marketing_evidence_handoff", response_model=CreatedEvidenceHandoffEnvelope,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def create_evidence_handoff(round_id: str, body: CreateEvidenceHandoff, request: Request, response: Response,
                            ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def create(s):
            row = workflow.get_round(s, ctx, round_id)
            workflow.serialize_workspace(s, ctx)
            s.refresh(row)
            if row.revision != revision:
                raise service.MarketingError("stale_round_revision", "测试内容已更新，请刷新后重试。", 409)
            return evidence_handoff.create(s, ctx, row, body)
        from app.adapters.persistence.models import ExperimentDecisionRecord
        def replay(s, rid):
            decision = s.scalar(select(ExperimentDecisionRecord).where(ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
                ExperimentDecisionRecord.id == rid))
            if not decision:
                raise service.MarketingError("handoff_not_found", "找不到这次证据交接。", 404)
            return decision
        decision = _resource_command(session, ctx, request, key, "marketing.evidence_handoff", body.model_dump(mode="json"), create, replay)
        return {"id": decision.id, "data": {"decision_id": decision.id, "evidence_origin": "marketing_business_target", "no_meta_writes": True}}


from . import image_editing
from .image_editing import CreateImageEdit

@router.post("/rounds/{round_id}/image-edits", operation_id="create_marketing_image_edit",
             response_model=ResourceEnvelope, response_model_exclude_unset=True, status_code=202, openapi_extra={"x-marketai-idempotent": True})
def create_image_edit(round_id: str, body: CreateImageEdit, request: Request, response: Response,
                      ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                      if_match: str | None = Header(default=None, alias="If-Match")):
    return execution_command(round_id, body, request, response, ctx, key, "marketing.image_edit", image_editing.prepare, include_image_edits=True)

