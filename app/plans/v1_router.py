"""``/api/v1/test-plans`` and ``/api/v1/generation/defaults`` (Issue #163).

Thin HTTP adapters over :mod:`app.plans.service` and
:mod:`app.plans.defaults`. Workspace and actor come only from the verified
identity (``request_tenant_context``); another Workspace's plan id answers
``404`` exactly like an unknown one. Every command requires an
``Idempotency-Key``; the defaults ``PUT`` also requires
``If-Match: "<revision>"`` (``"0"`` for the first save).

Issue #337 froze the legacy flow: ``revise`` and ``runs`` answer ``410
legacy_test_plans_frozen`` before reading the plan, so they write, enqueue,
reserve and audit nothing, for any caller and any plan id. Issue #343 froze
``create`` the same way, before reading the product. Reads are unchanged.
``service.create_plan``, ``service.revise_plan`` and ``runs.start_run`` stay
as domain functions for tests that build history.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext

from . import defaults as defaults_module
from . import service

router = APIRouter(tags=["test-plans"])
logger = logging.getLogger(__name__)

#: The answer every frozen legacy command gives (#337), in the contract.
FROZEN_RESPONSES: dict[int | str, dict[str, Any]] = {
    410: {"description": "The legacy test flow is frozen (`legacy_test_plans_frozen`); existing plans stay readable."},
}


def _idempotency_key(
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> str:
    return idempotency_key


def _problem(exc: service.PlanProblem | defaults_module.DefaultsProblem) -> HTTPException:
    return HTTPException(
        exc.status,
        {"code": exc.code, "message": exc.message, "retryable": getattr(exc, "retryable", False)},
    )


def _frozen(operation_id: str, context: TenantContext, plan_id: str | None = None) -> HTTPException:
    """The ``410`` a frozen legacy command answers, whoever asks and whatever
    the plan or product: it never loads either, so no Workspace learns whether
    another's exists. ``plan_id`` is ``None`` for ``create``, which has none."""
    logger.warning(
        "legacy_test_plans_frozen",
        extra={"operation_id": operation_id, "workspace_id": context.workspace_id, "plan_id": plan_id},
    )
    return _problem(service.PlanProblem(
        410, "legacy_test_plans_frozen", "旧测试流程已停用，只能查看；新测试请到「营销测试」",
    ))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

Scale = Literal["sample", "small", "full"]


class TestPlanCreateRequest(BaseModel):
    """The three questions (contract §3.2), each with a default: ``goal``
    ``explore``; ``audience_card_index`` the product's first audience card
    (or ``audience_text`` to word it yourself); ``scale`` ``sample``.
    ``optimize`` needs 1-10 ``seed_asset_version_ids`` (Approved assets or
    Winners of this Workspace)."""

    __test__ = False
    model_config = {"extra": "forbid"}

    product_id: str = Field(min_length=1, max_length=64)
    goal: Literal["explore", "optimize"] = "explore"
    audience_text: str | None = Field(default=None, max_length=500)
    audience_card_index: int | None = Field(default=None, ge=0, le=20)
    scale: Scale = "sample"
    seed_asset_version_ids: list[str] = Field(default_factory=list, max_length=10)
    #: "按结论生成下一轮" (Issue #166): seed the draft with this learnings
    #: snapshot's value verdicts (goal must be ``optimize``; replaces the
    #: seed-asset requirement).
    from_learnings_snapshot_id: str | None = Field(default=None, max_length=64)


class LearningsBasis(BaseModel):
    """The learnings a plan was drafted with (Issue #166): labels of the
    winning / losing / not-different values, the snapshot and its evidence
    partition; ``explicit`` is true for "按结论生成下一轮"."""

    snapshot_id: str
    evidence_mode: str
    explicit: bool = False
    winners: list[str] = Field(default_factory=list)
    losers: list[str] = Field(default_factory=list)
    no_difference: list[str] = Field(default_factory=list)


class MaxCost(BaseModel):
    amount: str
    currency: str


class UntestedSummaryDirection(BaseModel):
    direction_id: str
    title: str
    untested_count: int


class UntestedSummary(BaseModel):
    """Combinations a scale does not generate (shown on the plan card)."""

    count: int
    space_count: int
    constraint_excluded_count: int
    by_direction: list[UntestedSummaryDirection] = Field(default_factory=list)
    examples: list[dict[str, str]] = Field(default_factory=list)
    summary: str


class StageCounts(BaseModel):
    """One scale's planner output priced with the current defaults:
    ``images = combinations x variants``; ``max_cost`` is 0 under Mock."""

    scale: str
    cap: int | None = None
    combinations: int
    variants: int
    images: int
    max_cost: MaxCost
    untested: UntestedSummary


class Expansion(BaseModel):
    """What ``POST /runs {stage: "full"}`` would generate now: the plan's
    small/full selection minus combinations the sample Run already produced
    (``reused_combinations``, never paid twice)."""

    stage: str
    scale: str
    combinations: int
    reused_combinations: int
    variants: int
    images: int
    max_cost: MaxCost
    sample_run_in_progress: bool = False
    already_run: bool = False


class FactorValueView(BaseModel):
    id: str
    label: str
    proposed_by: str | None = None


class FactorView(BaseModel):
    key: str
    label: str
    values: list[FactorValueView] = Field(default_factory=list)


class DirectionCopy(BaseModel):
    headline: str = ""
    primary_text: str = ""


class DirectionView(BaseModel):
    """A direction card. ``direction_id`` is the stable plan-local id used
    by ``revise``; ``factors`` is the folded "factors and values" section;
    ``selected_in`` counts this direction's combinations per scale."""

    direction_id: str
    ordinal: int
    title: str
    what_it_tests: str
    hypothesis: str
    scene_description: str
    ad_copy: DirectionCopy = Field(alias="copy")
    factors: list[FactorView] = Field(default_factory=list)
    factor_values: dict[str, list[str]] = Field(default_factory=dict)
    combination_count: int
    combination_signatures: list[str] = Field(default_factory=list)
    selected_in: dict[str, int] = Field(default_factory=dict)
    removed: bool
    user_added: bool


class AudienceView(BaseModel):
    text: str
    card_index: int | None = None
    card: dict[str, Any] | None = None


class PlanFailure(BaseModel):
    code: str
    message: str
    detail: str | None = None
    retryable: bool = True


class GenerationDefaultsView(BaseModel):
    """Effective「生成默认值」. ``source`` is ``workspace`` once an Owner
    saved them (``revision`` >= 1), else ``computed`` (``revision`` 0).
    ``warnings``: ``mock_mode`` (show the bind-a-key banner),
    ``byok_credential_missing``."""

    provider: str
    model: str
    execution_mode: str
    aspect_ratio: str
    variants: dict[str, int]
    source: str
    revision: int
    credential_bound: bool
    warnings: list[str] = Field(default_factory=list)


class RunFailure(BaseModel):
    code: str | None = None
    detail: str | None = None


class TestPlanRunView(BaseModel):
    """A plan stage's Run. ``status``: ``materializing`` -> ``submitted``
    (or ``failed``, retryable by replaying the stage). ``run_status`` and
    ``items_*`` mirror the GenerationRun; poll ``operation_id`` through
    ``GET /operations/{id}`` or the Run through ``GET /generation/runs/{id}``."""

    __test__ = False

    stage: str
    target_scale: str
    status: str
    plan_revision: int
    run_id: str | None = None
    operation_id: str | None = None
    run_status: str | None = None
    items_total: int = 0
    items_done: int = 0
    items_failed: int = 0
    counts: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    template_entry_id: str | None = None
    template_version_id: str | None = None
    generation_draft_id: str | None = None
    preview_id: str | None = None
    failure: RunFailure | None = None
    created_at: str | None = None
    updated_at: str | None = None


class TestPlanDetail(BaseModel):
    """``status``: ``drafting`` (poll ``operation_id``) -> ``ready`` or
    ``failed``. ``stages`` has ``sample`` / ``small`` / ``full`` counts and
    max cost; ``selected_stage`` is the plan's own scale; ``expansion`` is
    what "展开完整测试" would cost now; ``runs`` are the linked Runs."""

    __test__ = False

    plan_id: str
    operation_id: str | None = None
    status: str
    revision: int
    product_id: str
    product_entry_id: str
    product_version_id: str
    product_name: str
    goal: str
    audience: AudienceView
    scale: str
    seed_asset_version_ids: list[str] = Field(default_factory=list)
    learnings_basis: LearningsBasis | None = None
    failure: PlanFailure | None = None
    factors: list[FactorView] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    directions: list[DirectionView] = Field(default_factory=list)
    stages: dict[str, StageCounts] = Field(default_factory=dict)
    selected_stage: StageCounts | None = None
    expansion: Expansion | None = None
    defaults: GenerationDefaultsView
    runs: list[TestPlanRunView] = Field(default_factory=list)
    drafting: dict[str, Any] = Field(default_factory=dict)
    planner_version: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    drafted_at: str | None = None
    replayed: bool = False


class TestPlanAccepted(BaseModel):
    """202: poll ``GET /operations/{operation_id}`` or ``GET /test-plans/{plan_id}``."""

    __test__ = False

    plan_id: str
    operation_id: str | None = None
    status: str
    revision: int
    replayed: bool = False


class TestPlanSummary(BaseModel):
    __test__ = False

    plan_id: str
    status: str
    revision: int
    product_id: str
    product_name: str
    goal: str
    scale: str
    direction_count: int
    run_stages: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class TestPlanList(BaseModel):
    __test__ = False

    items: list[TestPlanSummary] = Field(default_factory=list)


class FactorValueRef(BaseModel):
    """An existing value (``value_id``) or a new one worded by the user (``label``)."""

    model_config = {"extra": "forbid"}

    value_id: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=80)


class CopyEdit(BaseModel):
    model_config = {"extra": "forbid"}

    headline: str | None = Field(default=None, max_length=200)
    primary_text: str | None = Field(default=None, max_length=1000)


class DirectionEdit(BaseModel):
    """Edits are limited to the card's text and its factor values."""

    model_config = {"extra": "forbid"}

    direction_id: str = Field(min_length=1, max_length=32)
    title: str | None = Field(default=None, max_length=120)
    what_it_tests: str | None = Field(default=None, max_length=1000)
    hypothesis: str | None = Field(default=None, max_length=1000)
    scene_description: str | None = Field(default=None, max_length=1000)
    ad_copy: CopyEdit | None = Field(default=None, alias="copy")
    factor_values: dict[str, list[FactorValueRef]] | None = None


class DirectionAdd(BaseModel):
    """The user's own idea; omitted factors take each factor's first value."""

    model_config = {"extra": "forbid"}

    title: str = Field(min_length=1, max_length=120)
    what_it_tests: str | None = Field(default=None, max_length=1000)
    hypothesis: str | None = Field(default=None, max_length=1000)
    scene_description: str | None = Field(default=None, max_length=1000)
    ad_copy: CopyEdit | None = Field(default=None, alias="copy")
    factor_values: dict[str, list[FactorValueRef]] | None = None


class TestPlanReviseRequest(BaseModel):
    """Remove / restore / edit / add directions and optionally change the
    scale; the plan is re-planned and its counts and cost updated."""

    __test__ = False
    model_config = {"extra": "forbid"}

    remove: list[str] = Field(default_factory=list, max_length=8)
    restore: list[str] = Field(default_factory=list, max_length=8)
    edit: list[DirectionEdit] = Field(default_factory=list, max_length=8)
    add: list[DirectionAdd] = Field(default_factory=list, max_length=8)
    scale: Scale | None = None


class TestPlanRunRequest(BaseModel):
    """``sample``: one combination per direction. ``full``: the plan's
    small/full selection (``full`` when the plan's scale is ``sample``),
    skipping combinations the sample Run already produced."""

    __test__ = False
    model_config = {"extra": "forbid"}

    stage: Literal["sample", "full"]


class TestPlanRunResult(TestPlanRunView):
    plan_id: str
    replayed: bool = False


class GenerationDefaultsUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["mock", "byok"]
    aspect_ratio: str = Field(min_length=1, max_length=16)
    variants: dict[Literal["sample", "small", "full"], int]


# ---------------------------------------------------------------------------
# Test plans
# ---------------------------------------------------------------------------


@router.post(
    "/test-plans",
    operation_id="create_test_plan",
    response_model=TestPlanAccepted,
    status_code=202,
    responses=FROZEN_RESPONSES,
    deprecated=True,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_test_plan(
    body: TestPlanCreateRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Frozen (#343): always ``410 legacy_test_plans_frozen``, also on an
    Idempotency-Key replay. A plan and its drafting Job were admitted here."""
    raise _frozen("create_test_plan", context)


@router.get("/test-plans", operation_id="list_test_plans", response_model=TestPlanList)
def list_test_plans(
    product_id: str | None = Query(default=None, max_length=64),
    run_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    context: TenantContext = Depends(request_tenant_context),
):
    """Newest plans first, optionally for one product, or the plan one of
    whose stages materialised into the generation Run ``run_id`` (the Run
    and Assets pages group that Run's images by direction)."""
    try:
        return service.list_plans(context, product_id=product_id, run_id=run_id, limit=limit)
    except service.PlanProblem as exc:
        raise _problem(exc) from exc


@router.get("/test-plans/{plan_id}", operation_id="get_test_plan", response_model=TestPlanDetail)
def get_test_plan(
    plan_id: str,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    """Plan, directions, per-scale counts and max cost, untested summary, runs."""
    try:
        view = service.get_plan(context, plan_id)
    except service.PlanProblem as exc:
        raise _problem(exc) from exc
    set_etag(response, view["revision"])
    return view


@router.post(
    "/test-plans/{plan_id}/revise",
    operation_id="revise_test_plan",
    response_model=TestPlanDetail,
    responses=FROZEN_RESPONSES,
    deprecated=True,
    openapi_extra={"x-marketai-idempotent": True},
)
def revise_test_plan(
    plan_id: str,
    body: TestPlanReviseRequest = Body(...),
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Frozen (#337): always ``410 legacy_test_plans_frozen``. Directions
    were removed / restored / edited / added here (If-Match revision)."""
    raise _frozen("revise_test_plan", context, plan_id)


@router.post(
    "/test-plans/{plan_id}/runs",
    operation_id="create_test_plan_run",
    response_model=TestPlanRunResult,
    status_code=202,
    responses=FROZEN_RESPONSES,
    deprecated=True,
    openapi_extra={"x-marketai-idempotent": True},
)
def create_test_plan_run(
    plan_id: str,
    body: TestPlanRunRequest,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Frozen (#337): always ``410 legacy_test_plans_frozen``. A stage was
    materialised here into a template, draft, preview and submitted Run."""
    raise _frozen("create_test_plan_run", context, plan_id)


# ---------------------------------------------------------------------------
# Generation defaults
# ---------------------------------------------------------------------------


@router.get(
    "/generation/defaults",
    operation_id="get_generation_defaults",
    response_model=GenerationDefaultsView,
)
def get_generation_defaults(
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
):
    """The Workspace's effective generation defaults (any member)."""
    try:
        view = defaults_module.get_defaults(context)
    except defaults_module.DefaultsProblem as exc:
        raise _problem(exc) from exc
    set_etag(response, view["revision"])
    return view


@router.put(
    "/generation/defaults",
    operation_id="update_generation_defaults",
    response_model=GenerationDefaultsView,
    openapi_extra={"x-marketai-idempotent": True},
)
def update_generation_defaults(
    body: GenerationDefaultsUpdate,
    request: Request,
    response: Response,
    context: TenantContext = Depends(request_tenant_context),
    idempotency_key: str = Depends(_idempotency_key),
):
    """Owner-only; ``If-Match: "0"`` for the first save."""
    expected_revision = extract_if_match(request)
    variants = {str(key): value for key, value in body.variants.items()}
    if set(variants) != {"sample", "small", "full"}:
        raise HTTPException(422, {"code": "variants_invalid", "message": "variants 需要 sample、small、full 三项"})
    try:
        view = defaults_module.update_defaults(
            context,
            expected_revision=expected_revision,
            provider=body.provider,
            model=body.model,
            execution_mode=body.execution_mode,
            aspect_ratio=body.aspect_ratio,
            variants=variants,
        )
    except defaults_module.DefaultsProblem as exc:
        raise _problem(exc) from exc
    set_etag(response, view["revision"])
    return view
