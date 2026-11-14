"""Test-plan commands and views: create, read, list, revise (Issue #163).

Front-flow contract §3.2: the user answers at most three questions (goal,
audience, scale), the AI drafts 3-5 direction cards, the user removes,
rewords or adds directions, and the page shows the image count and maximum
cost for every scale before anything is generated.

Authorization
-------------
Plans are Workspace-scoped and follow the generation draft rules: any
member (Owner or Member) may create, read, revise and run a plan -- exactly
who may create, preview and submit a generation draft today. Spend stays
bounded by the Owner's Spend Guardrail at submission and by the Owner-only
generation defaults (:mod:`app.plans.defaults`). Another Workspace's plan
id, product id or asset id answers ``404`` like an unknown one (no
existence oracle).

Idempotency and concurrency
---------------------------
* ``POST /test-plans`` -- the ``Idempotency-Key`` replays the same plan; the
  same key with a different body is ``409 idempotency_key_reused``.
* ``POST /test-plans/{id}/revise`` -- ``If-Match: "<revision>"``; a stale
  revision is ``409 revision_conflict`` (the draft-style CAS other drafts
  use), so two tabs can never silently overwrite each other.
* Runs are idempotent per (plan, stage); see :mod:`app.plans.runs`.

Provider neutrality (§5.1): the recipe never stores a provider, model or
prompt. It holds the frozen product facts, the factor catalog with English
fragments and the plan constraints; a Run binds the image provider later.
Only the plan's *evidence* names the drafting LLM call (its quote at
admission, its attempt, usage and cost after drafting).

Drafting spend (§6, Issue #164)
-------------------------------
A Workspace with its own OpenAI key bound drafts ``byok`` through the
governed LLM drafter: ``create_plan`` reserves the call's maximum cost in the
admission transaction (a guardrail breach or a missing entitlement refuses
the command with Problem Details and creates nothing); the Job settles it.
Without a key the deterministic mock drafter runs and nothing is reserved.
``revise`` (remove / edit / add a direction) never calls the LLM: an added
direction is the user's own wording, mapped deterministically.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AdTestPlanDirectionRecord,
    AdTestPlanRecord,
    AdTestPlanRunRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    new_id,
    utcnow,
)
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.planner import (
    PLAN_SCALES,
    TEST_PLANNER_VERSION,
    PlanDirectionSpace,
    PlannerError,
    StagePlan,
    enumerate_plan_space,
    plan_all_stages,
)
from app.platform import audit
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, create_operation
from app.platform.tenancy import TenantContext
from app.providers.llm_chat import LlmChatError, reserve_llm_chat, resolve_llm_execution_mode

from . import drafting
from .defaults import GenerationDefaults, effective_defaults

JOB_TYPE = "test_plans.draft"
COMMAND_TYPE = "test_plan.create"
RESOURCE_TYPE = "test_plan"

MAX_SEED_ASSETS = 10
#: RunItem states whose combination already produced an image (paid for).
GENERATED_ITEM_STATES = frozenset({"succeeded", "reused", "qc_pending"})
#: RunItem states that are still on their way to a terminal state.
IN_FLIGHT_ITEM_STATES = frozenset({"pending", "queued", "running", "paused", "uncertain"})

#: Failure code -> (user copy, suggested action). The actions are product
#: intake's vocabulary: ``bind_openai_key``, ``adjust_spend_guardrail``,
#: ``retry`` (create the plan again).
FAILURE_COPY: dict[str, tuple[str, str]] = {
    "drafting_failed": ("AI 没能起草出测试方案，请重试。", "retry"),
    "draft_invalid": ("AI 起草的方案未通过检查，请重新起草。", "retry"),
    "internal_error": ("起草测试方案时出错，请重试。", "retry"),
    "credential_unavailable": ("工作区的 OpenAI 密钥不可用，请在设置中重新绑定后重试。", "bind_openai_key"),
    "credential_rejected": ("OpenAI 拒绝了工作区的密钥，请在设置中重新绑定后重试。", "bind_openai_key"),
    "guardrail_exceeded": ("起草方案会超出花费护栏，请调整护栏后重试。", "adjust_spend_guardrail"),
    "rate_limited": ("OpenAI 暂时限流，请稍后重试。", "retry"),
    "quota_exhausted": ("工作区 OpenAI 密钥所在账户的余额或额度已用完，请充值或换一个密钥后重试。", "bind_openai_key"),
    "invalid_json": ("AI 返回的结果格式不正确，请重试。", "retry"),
    "schema_validation_failed": ("AI 返回的结果格式不正确，请重试。", "retry"),
    "model_refusal": ("AI 拒绝起草这个方案，请调整产品描述或客群后重试。", "retry"),
}


def failure_action(code: str | None) -> str:
    """The suggested action for a drafting failure ``code``."""
    return FAILURE_COPY.get(code or "", FAILURE_COPY["drafting_failed"])[1]


@dataclass
class PlanProblem(Exception):
    """A deterministic refusal the router turns into Problem Details."""

    status: int
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise PlanProblem(403, "test_plan_forbidden", "只有工作区成员可以创建和修改测试方案")


def load_plan(session, ctx: TenantContext, plan_id: str, *, for_update: bool = False) -> AdTestPlanRecord:
    statement = select(AdTestPlanRecord).where(
        AdTestPlanRecord.id == plan_id,
        AdTestPlanRecord.workspace_id == ctx.workspace_id,
    )
    if for_update:
        statement = statement.with_for_update()
    plan = session.scalars(statement).one_or_none()
    if plan is None:
        raise PlanProblem(404, "test_plan_not_found", "测试方案不存在")
    return plan


def load_directions(session, ctx: TenantContext, plan_id: str) -> list[AdTestPlanDirectionRecord]:
    return list(session.scalars(
        select(AdTestPlanDirectionRecord)
        .where(
            AdTestPlanDirectionRecord.workspace_id == ctx.workspace_id,
            AdTestPlanDirectionRecord.plan_id == plan_id,
        )
        .order_by(AdTestPlanDirectionRecord.ordinal)
    ))


def load_runs(session, ctx: TenantContext, plan_id: str) -> dict[str, AdTestPlanRunRecord]:
    rows = session.scalars(
        select(AdTestPlanRunRecord).where(
            AdTestPlanRunRecord.workspace_id == ctx.workspace_id,
            AdTestPlanRunRecord.plan_id == plan_id,
        )
    )
    return {row.stage: row for row in rows}


# ---------------------------------------------------------------------------
# Planning over the stored directions
# ---------------------------------------------------------------------------


def active_directions(directions: list[AdTestPlanDirectionRecord]) -> list[AdTestPlanDirectionRecord]:
    return [direction for direction in directions if not direction.removed]


def direction_spaces(directions: list[AdTestPlanDirectionRecord]) -> list[PlanDirectionSpace]:
    return [
        PlanDirectionSpace(
            key=direction.direction_key,
            factor_values={key: list(values) for key, values in (direction.factor_values_json or {}).items()},
        )
        for direction in active_directions(directions)
    ]


def compute_stages(plan: AdTestPlanRecord, directions: list[AdTestPlanDirectionRecord]) -> dict[str, StagePlan]:
    try:
        return plan_all_stages(direction_spaces(directions), (plan.recipe_json or {}).get("constraints") or [])
    except PlannerError as exc:
        raise PlanProblem(422, exc.code, exc.detail) from exc


def expansion_scale(plan_scale: str) -> str:
    """The scale ``stage: "full"`` materialises: the plan's own small/full
    test, or the full test when the plan started with samples only."""
    return plan_scale if plan_scale in ("small", "full") else "full"


def generated_signatures(session, ctx: TenantContext, run_row: AdTestPlanRunRecord | None) -> tuple[set[str], bool]:
    """(plan signatures that already produced an image, any item in flight)."""
    if run_row is None or not run_row.generation_run_id:
        return set(), False
    by_fingerprint = dict((run_row.selection_json or {}).get("fingerprints") or {})
    rows = session.execute(
        select(RunItemRecord.combination_fingerprint, RunItemRecord.state).where(
            RunItemRecord.workspace_id == ctx.workspace_id,
            RunItemRecord.generation_run_id == run_row.generation_run_id,
        )
    ).all()
    generated = {by_fingerprint[fp] for fp, state in rows if state in GENERATED_ITEM_STATES and fp in by_fingerprint}
    in_flight = any(state in IN_FLIGHT_ITEM_STATES for _fp, state in rows)
    return generated, in_flight


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _resolve_product(session, ctx: TenantContext, product_id: str) -> tuple[ProductRecord, LibraryVersionRecord]:
    product = session.scalars(
        select(ProductRecord).where(
            ProductRecord.workspace_id == ctx.workspace_id,
            (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id),
        )
    ).first()
    if product is None:
        raise PlanProblem(404, "product_not_found", "产品不存在")
    entry = None
    if product.product_profile_entry_id:
        entry = session.scalars(
            select(LibraryEntryRecord).where(
                LibraryEntryRecord.workspace_id == ctx.workspace_id,
                LibraryEntryRecord.id == product.product_profile_entry_id,
                LibraryEntryRecord.entry_type == "product",
            )
        ).one_or_none()
    if entry is None or not entry.current_version_id:
        raise PlanProblem(409, "product_not_published", "请先确认产品卡（发布产品版本）后再设置广告测试")
    if entry.lifecycle != "active":
        raise PlanProblem(409, "product_archived", "该产品已归档")
    version = session.scalars(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == entry.current_version_id,
        )
    ).one()
    return product, version


def _product_facts(product: ProductRecord, version: LibraryVersionRecord) -> drafting.ProductFacts:
    content = dict(version.content_json or {})
    return drafting.ProductFacts(
        product_id=product.id,
        entry_id=version.entry_id,
        version_id=version.id,
        name=str(content.get("name") or product.name or ""),
        description=str(content.get("description") or ""),
        selling_points=[str(item) for item in content.get("selling_points") or [] if str(item).strip()],
        audiences=[dict(item) for item in content.get("audiences") or [] if isinstance(item, dict)],
        must_match=[dict(item) for item in content.get("must_match") or [] if isinstance(item, dict)],
        specs={str(k): str(v) for k, v in dict(content.get("specs") or {}).items()},
        reference_version_ids=[
            str(item.get("reference_version_id"))
            for item in content.get("reference_bindings") or []
            if isinstance(item, dict) and item.get("reference_version_id")
        ],
    )


def _audience(facts: drafting.ProductFacts, version: LibraryVersionRecord, text: str | None,
              card_index: int | None) -> drafting.AudienceChoice:
    cards = facts.audiences
    if card_index is not None:
        if not 0 <= card_index < len(cards):
            raise PlanProblem(422, "audience_card_out_of_range", "所选客群卡不存在")
    elif not (text or "").strip() and cards:
        card_index = 0
    card = cards[card_index] if card_index is not None else None
    chosen = drafting.clean_text(text, 500)
    if not chosen and card is not None:
        who = drafting.clean_text(card.get("who"), 200)
        why = drafting.clean_text(card.get("why"), 300)
        chosen = f"{who}：{why}" if why else who
    if not chosen:
        chosen = drafting.clean_text((version.content_json or {}).get("audience"), 500)
    return drafting.AudienceChoice(text=chosen, card_index=card_index, card=card)


def _seeds(session, ctx: TenantContext, ids: list[str]) -> drafting.WinnersLosers:
    if not ids:
        return drafting.WinnersLosers()
    found = set(session.scalars(
        select(AssetVersionRecord.id).where(
            AssetVersionRecord.workspace_id == ctx.workspace_id,
            AssetVersionRecord.id.in_(ids),
        )
    ))
    missing = [item for item in ids if item not in found]
    if missing:
        raise PlanProblem(404, "seed_asset_not_found", f"素材 {missing[0]} 不存在")
    tags: dict[str, dict] = {}
    rows = session.execute(
        select(RunItemAssetVersionBindingRecord.asset_version_id, RunItemRecord.combination_identity_json)
        .join(RunItemRecord, RunItemRecord.id == RunItemAssetVersionBindingRecord.run_item_id)
        .where(
            RunItemAssetVersionBindingRecord.workspace_id == ctx.workspace_id,
            RunItemRecord.workspace_id == ctx.workspace_id,
            RunItemAssetVersionBindingRecord.asset_version_id.in_(ids),
        )
    ).all()
    for asset_version_id, identity in rows:
        dims = (identity or {}).get("dimensions") if isinstance(identity, dict) else None
        if isinstance(dims, dict):
            tags[asset_version_id] = {str(k): str(v) for k, v in dims.items()}
    names = _dimension_names(session, ctx, tags.values())
    # External creatives (Issue #168) have no RunItem: their confirmed tags
    # are the labels the drafter reads ("优化已有广告" with the user's own ads).
    from app.external_creatives.identity import latest_confirmations, tag_labels

    external = {
        version_id: tag_labels(confirmation)
        for version_id, confirmation in latest_confirmations(
            session, ctx, [item for item in ids if item not in tags],
        ).items()
    }
    return drafting.WinnersLosers(
        winners=[
            drafting.SeedAsset(
                asset_version_id=item,
                verdict="seed",
                dimension_tags=tags.get(item, {}),
                dimension_labels=(
                    _labels_for(tags.get(item, {}), names) if item in tags else external.get(item, {})
                ),
            )
            for item in ids
        ],
    )


def _dimension_names(session, ctx: TenantContext, tag_maps) -> dict[tuple[str, str], tuple[str, dict[str, str]]]:
    """``(template entry id, dimension id) -> (dimension name, {value id: value name})``
    for the dimension templates the seed assets' combination tags name
    (``<template entry id>:<dimension id>`` keys), latest version winning."""
    entry_ids = sorted({
        key.rsplit(":", 1)[0] for tag_map in tag_maps for key in tag_map if ":" in key
    })
    if not entry_ids:
        return {}
    names: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
    versions = session.scalars(
        select(LibraryVersionRecord)
        .where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.entry_id.in_(entry_ids),
        )
        .order_by(LibraryVersionRecord.version_no)
    )
    for version in versions:
        for dimension in (version.content_json or {}).get("dimensions") or []:
            if not isinstance(dimension, dict) or not dimension.get("id"):
                continue
            key = (version.entry_id, str(dimension["id"]))
            values = dict(names.get(key, ("", {}))[1])
            for value in dimension.get("values") or []:
                if isinstance(value, dict) and value.get("id"):
                    values[str(value["id"])] = str(value.get("name") or value["id"])
            names[key] = (str(dimension.get("name") or dimension["id"]), values)
    return names


def _labels_for(tag_map: dict[str, str], names: dict) -> dict[str, str]:
    """A seed asset's tags as ``{dimension name: value name}`` (what the
    drafter prompts with); tags that resolve to nothing are left out."""
    labels: dict[str, str] = {}
    for key, value_id in tag_map.items():
        if ":" not in key:
            continue
        entry_id, dimension_id = key.rsplit(":", 1)
        resolved = names.get((entry_id, dimension_id))
        if resolved is None:
            continue
        dimension_name, values = resolved
        labels[dimension_name] = values.get(value_id, value_id)
    return labels


#: ``reserve_llm_chat`` refusal -> HTTP status of the create command.
_RESERVATION_STATUS = {"guardrail_exceeded": 409, "credential_unavailable": 409, "entitlement_unavailable": 403}
_RESERVATION_COPY = {
    "guardrail_exceeded": "起草方案会超出工作区的花费护栏，请先调整花费护栏",
    "credential_unavailable": "工作区的 OpenAI 密钥不可用，请在设置中重新绑定",
    "entitlement_unavailable": "当前工作区的订阅不允许调用 AI 起草",
}


def _reserve_drafting_call(
    session,
    ctx: TenantContext,
    *,
    operation_id: str,
    job_id: str,
    facts: drafting.ProductFacts,
    goal: str,
    audience: drafting.AudienceChoice,
    scale: str,
    winners_losers: drafting.WinnersLosers,
) -> dict:
    """Reserve the drafting call's maximum cost in the admission transaction.

    The request is built exactly as the Worker will send it (deterministic in
    the frozen recipe), so the reservation covers its true input ceiling plus
    ``max_output_tokens`` (and the one schema retry). A refusal rolls the
    whole command back and answers Problem Details.
    """
    from .llm_drafter import CALL_NAME, build_request

    request = build_request(facts, goal, audience, scale, winners_losers)
    try:
        reservation = reserve_llm_chat(
            session, ctx,
            operation_id=operation_id,
            job_id=job_id,
            call_name=CALL_NAME,
            execution_mode="byok",
            request=request,
        )
    except LlmChatError as error:
        raise PlanProblem(
            _RESERVATION_STATUS.get(error.code, 422), error.code,
            _RESERVATION_COPY.get(error.code, error.detail),
        ) from error
    quote = reservation.quote
    return {
        "drafting_mode": "byok",
        "reserved": True,
        "reservation_id": reservation.reservation_id,
        "call_name": CALL_NAME,
        "quote": {
            "provider": quote.provider,
            "model": quote.model,
            "input_tokens": quote.input_tokens,
            "max_output_tokens": quote.max_output_tokens,
            "max_amount": format(quote.amount, "f"),
            "currency": quote.currency,
            "price_rule_version": quote.price_rule_version,
        },
    }


def _with_learnings(
    session, ctx: TenantContext, product_id: str, winners_losers: drafting.WinnersLosers,
    snapshot_id: str | None,
) -> tuple[drafting.WinnersLosers, dict | None]:
    """Add the product's learned value verdicts (Issue #166) for the drafter."""
    from dataclasses import replace

    from app.learnings import service as learnings

    try:
        verdicts, basis = learnings.value_verdicts(session, ctx, product_id, snapshot_id)
    except learnings.LearningsProblem as problem:
        raise PlanProblem(problem.status, problem.code, problem.message) from problem
    if snapshot_id and not verdicts:
        raise PlanProblem(409, "learnings_inconclusive", "这份结论里还没有任何可用的判断（样本不足），先补充投放数据")
    return replace(
        winners_losers,
        value_verdicts=[drafting.ValueVerdict(**verdict) for verdict in verdicts],
    ), basis


def create_plan(
    ctx: TenantContext,
    *,
    product_id: str,
    goal: str,
    audience_text: str | None,
    audience_card_index: int | None,
    scale: str,
    seed_asset_version_ids: list[str] | None,
    idempotency_key: str,
    from_learnings_snapshot_id: str | None = None,
) -> dict:
    """Admit a plan: row + Operation + drafting Job, one transaction.

    ``from_learnings_snapshot_id`` (Issue #166, "按结论生成下一轮") seeds the
    draft with that learnings snapshot's value verdicts; it requires goal
    ``optimize`` and replaces the seed-asset requirement. Without it, the
    product's latest *production* learnings (if any) still reach the drafter.
    """
    require_member(ctx)
    if goal not in drafting.GOALS:
        raise PlanProblem(422, "goal_invalid", "目的必须是 explore 或 optimize")
    if scale not in PLAN_SCALES:
        raise PlanProblem(422, "scale_invalid", "规模必须是 sample、small 或 full")
    seeds = list(dict.fromkeys(item.strip() for item in seed_asset_version_ids or [] if item and item.strip()))
    from_learnings_snapshot_id = (from_learnings_snapshot_id or "").strip() or None
    if len(seeds) > MAX_SEED_ASSETS:
        raise PlanProblem(422, "too_many_seed_assets", f"最多选择 {MAX_SEED_ASSETS} 个已有素材")
    if goal == "explore" and seeds:
        raise PlanProblem(422, "seed_assets_not_allowed", "探索新方向不需要选择已有素材")
    if goal == "explore" and from_learnings_snapshot_id:
        raise PlanProblem(422, "learnings_require_optimize", "按结论生成下一轮的目的必须是 optimize")
    if goal == "optimize" and not seeds and not from_learnings_snapshot_id:
        raise PlanProblem(422, "seed_assets_required", "优化已有广告需要至少选择一个已有素材")

    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    fingerprint_input = {
        "product_id": product_id, "goal": goal, "audience_text": audience_text,
        "audience_card_index": audience_card_index, "scale": scale, "seeds": seeds,
    }
    if from_learnings_snapshot_id:
        fingerprint_input["from_learnings_snapshot_id"] = from_learnings_snapshot_id
    request_fingerprint = _sha(fingerprint_input)
    with tenant_session(ctx) as session:
        replay = session.scalars(
            select(AdTestPlanRecord).where(
                AdTestPlanRecord.workspace_id == ctx.workspace_id,
                AdTestPlanRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if replay is not None:
            if replay.request_fingerprint != request_fingerprint:
                raise PlanProblem(409, "idempotency_key_reused", "Idempotency-Key 已用于另一次创建请求")
            return {**plan_view(session, ctx, replay), "replayed": True}

        product, version = _resolve_product(session, ctx, product_id)
        facts = _product_facts(product, version)
        audience = _audience(facts, version, audience_text, audience_card_index)
        winners_losers = _seeds(session, ctx, seeds)
        winners_losers, learnings_basis = _with_learnings(
            session, ctx, product.id, winners_losers, from_learnings_snapshot_id,
        )
        # byok when the Workspace bound its own OpenAI key, else the mock
        # drafter (contract §4/§6); frozen here, never switched by the Job.
        mode = resolve_llm_execution_mode(session, ctx)

        plan_id = new_id("atp")
        now = utcnow()
        plan = AdTestPlanRecord(
            id=plan_id,
            workspace_id=ctx.workspace_id,
            product_id=product.id,
            product_entry_id=version.entry_id,
            product_version_id=version.id,
            goal=goal,
            audience_text=audience.text,
            audience_card_index=audience.card_index,
            scale=scale,
            status="drafting",
            revision=1,
            recipe_json={
                "schema": "test-plan-recipe/v1",
                "planner_version": TEST_PLANNER_VERSION,
                "product": facts.as_dict(),
                "audience": {"text": audience.text, "card_index": audience.card_index, "card": audience.card},
                "goal": goal,
                "seeds": winners_losers.as_dict(),
                "learnings": learnings_basis,
                "factors": [],
                "constraints": [],
            },
            seed_asset_version_ids=seeds,
            evidence_json={"admission": {"drafting_mode": mode, "reserved": False}},
            idempotency_key_hash=key_hash,
            request_fingerprint=request_fingerprint,
            created_by=ctx.initiator_id,
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()
        operation = create_operation(
            session,
            command_type=COMMAND_TYPE,
            idempotency_key_hash=key_hash,
            initial_state=OperationState.ADMITTED,
            admission_contract_version=1,
            result_ref=OperationResultRef(resource_type=RESOURCE_TYPE, resource_id=plan_id),
            reason="test plan admitted",
        )
        job = enqueue_in_session(
            session,
            context=ctx,
            job_type=JOB_TYPE,
            payload={"plan_id": plan_id, "operation_id": operation.id},
            idempotency_key=f"test_plan_draft:{plan_id}",
            resource_type=RESOURCE_TYPE,
            resource_id=plan_id,
            max_attempts=3,
        )
        job.operation_id = operation.id
        plan.operation_id = operation.id
        plan.job_id = job.id
        if mode == "byok":
            plan.evidence_json = {
                "admission": _reserve_drafting_call(
                    session, ctx, operation_id=operation.id, job_id=job.id,
                    facts=facts, goal=goal, audience=audience, scale=scale, winners_losers=winners_losers,
                ),
            }
        audit.append_event(
            session,
            event_type="test_plan.created",
            resource_type=RESOURCE_TYPE,
            resource_id=plan_id,
            details={
                "product_id": product.id,
                "product_version_id": version.id,
                "goal": goal,
                "scale": scale,
                "audience_card_index": audience.card_index,
                "seed_count": len(seeds),
                "learnings_snapshot_id": (learnings_basis or {}).get("snapshot_id"),
                "learned_verdicts": len(winners_losers.value_verdicts),
                "drafting_mode": mode,
                "operation_id": operation.id,
                "job_id": job.id,
            },
        )
        session.flush()
        return {**plan_view(session, ctx, plan), "replayed": False}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _stage_view(
    stage: StagePlan, defaults: GenerationDefaults, titles: dict[str, str], references: int = 0,
    qc_references: int = 0,
) -> dict:
    variants = defaults.variants_for(stage.scale)
    images = len(stage.selected) * variants
    return {
        "scale": stage.scale,
        "cap": stage.cap,
        "combinations": len(stage.selected),
        "variants": variants,
        "images": images,
        "max_cost": defaults.max_cost(images, reference_images=references, qc_references=qc_references),
        "untested": stage.untested_summary(titles),
    }


def _run_view(session, ctx: TenantContext, row: AdTestPlanRunRecord) -> dict:
    run = None
    if row.generation_run_id:
        run = session.scalars(
            select(GenerationRunRecord).where(
                GenerationRunRecord.workspace_id == ctx.workspace_id,
                GenerationRunRecord.id == row.generation_run_id,
            )
        ).one_or_none()
    selection = dict(row.selection_json or {})
    return {
        "stage": row.stage,
        "target_scale": row.target_scale,
        "status": row.status,
        "plan_revision": row.plan_revision,
        "run_id": row.generation_run_id,
        "operation_id": row.operation_id,
        "run_status": run.status if run is not None else None,
        "items_total": int(run.total) if run is not None else 0,
        "items_done": int(run.done) if run is not None else 0,
        "items_failed": int(run.failed) if run is not None else 0,
        "counts": dict(selection.get("counts") or {}),
        "defaults": dict(row.defaults_json or {}),
        "template_entry_id": row.template_entry_id,
        "template_version_id": row.template_version_id,
        "generation_draft_id": row.generation_draft_id,
        "preview_id": row.preview_id,
        "failure": (
            {"code": row.failure_code, "detail": row.failure_detail}
            if row.status == "failed" else None
        ),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _factor_label_map(recipe: dict) -> dict[str, dict]:
    return {factor["key"]: factor for factor in recipe.get("factors") or []}


def _direction_view(direction: AdTestPlanDirectionRecord, factors: dict[str, dict],
                    stages: dict[str, StagePlan] | None) -> dict:
    folded = []
    for key, value_ids in (direction.factor_values_json or {}).items():
        factor = factors.get(key) or {"label": key, "values": []}
        labels = {value["id"]: value["label"] for value in factor.get("values") or []}
        folded.append({
            "key": key,
            "label": factor.get("label") or key,
            "values": [{"id": value_id, "label": labels.get(value_id, value_id)} for value_id in value_ids],
        })
    selected_in = {}
    if stages is not None and not direction.removed:
        for scale, stage in stages.items():
            selected_in[scale] = sum(1 for combo in stage.selected if combo.direction == direction.direction_key)
    return {
        "direction_id": direction.direction_key,
        "ordinal": direction.ordinal,
        "title": direction.title,
        "what_it_tests": direction.what_it_tests,
        "hypothesis": direction.hypothesis,
        "scene_description": direction.scene_description,
        "copy": {
            "headline": (direction.copy_json or {}).get("headline", ""),
            "primary_text": (direction.copy_json or {}).get("primary_text", ""),
        },
        "factors": folded,
        "factor_values": dict(direction.factor_values_json or {}),
        "combination_count": len(direction.combination_signatures_json or []),
        "combination_signatures": list(direction.combination_signatures_json or []),
        "selected_in": selected_in,
        "removed": bool(direction.removed),
        "user_added": bool(direction.user_added),
    }


def plan_view(session, ctx: TenantContext, plan: AdTestPlanRecord) -> dict:
    recipe = dict(plan.recipe_json or {})
    directions = load_directions(session, ctx, plan.id)
    defaults = effective_defaults(session, ctx)
    runs = load_runs(session, ctx, plan.id)
    stages: dict[str, StagePlan] | None = None
    stage_views: dict[str, dict] = {}
    expansion = None
    if plan.status == "ready" and active_directions(directions):
        stages = compute_stages(plan, directions)
        titles = {direction.direction_key: direction.title for direction in directions}
        # Issue #167: a Run whose model takes the product's reference images
        # is priced with them; the card quotes what the preview will.
        references = defaults.reference_images_per_call(session, ctx, plan.product_id)
        # Issue #169: and each image's quality check, against these references.
        qc_references = defaults.quality_check_references(session, ctx, plan.product_id)
        stage_views = {
            scale: _stage_view(stage, defaults, titles, references, qc_references)
            for scale, stage in stages.items()
        }
        target = expansion_scale(plan.scale)
        generated, in_flight = generated_signatures(session, ctx, runs.get("sample"))
        target_selected = stages[target].selected
        reused = [combo for combo in target_selected if combo.signature in generated]
        remaining = len(target_selected) - len(reused)
        variants = defaults.variants_for(target)
        expansion = {
            "stage": "full",
            "scale": target,
            "combinations": remaining,
            "reused_combinations": len(reused),
            "variants": variants,
            "images": remaining * variants,
            "max_cost": defaults.max_cost(
                remaining * variants, reference_images=references, qc_references=qc_references,
            ),
            "sample_run_in_progress": in_flight,
            "already_run": "full" in runs and runs["full"].status == "submitted",
        }
    failure = None
    if plan.status == "failed":
        code = plan.failure_code or "drafting_failed"
        message, action = FAILURE_COPY.get(code, FAILURE_COPY["drafting_failed"])
        failure = {
            "code": code,
            "message": message,
            "detail": plan.failure_detail,
            # Creating the plan again is enough unless a key or the
            # guardrail must change first (``drafting.suggested_action``).
            "retryable": action == "retry",
        }
    factors = _factor_label_map(recipe)
    product = recipe.get("product") or {}
    audience = recipe.get("audience") or {}
    return {
        "plan_id": plan.id,
        "operation_id": plan.operation_id,
        "status": plan.status,
        "revision": plan.revision,
        "product_id": plan.product_id,
        "product_entry_id": plan.product_entry_id,
        "product_version_id": plan.product_version_id,
        "product_name": product.get("name") or "",
        "goal": plan.goal,
        "audience": {"text": plan.audience_text, "card_index": plan.audience_card_index, "card": audience.get("card")},
        "scale": plan.scale,
        "seed_asset_version_ids": list(plan.seed_asset_version_ids or []),
        "learnings_basis": recipe.get("learnings") or None,
        "failure": failure,
        "factors": [
            {
                "key": factor["key"],
                "label": factor.get("label") or factor["key"],
                "values": [
                    {"id": value["id"], "label": value["label"], "proposed_by": value.get("proposed_by", "ai")}
                    for value in factor.get("values") or []
                ],
            }
            for factor in recipe.get("factors") or []
        ],
        "constraints": list(recipe.get("constraints") or []),
        "directions": [_direction_view(direction, factors, stages) for direction in directions],
        "stages": stage_views,
        "selected_stage": stage_views.get(plan.scale),
        "expansion": expansion,
        "defaults": defaults.view(),
        "runs": [_run_view(session, ctx, runs[stage]) for stage in ("sample", "full") if stage in runs],
        "drafting": dict((plan.evidence_json or {}).get("drafting") or {}),
        "planner_version": recipe.get("planner_version"),
        "created_by": plan.created_by,
        "created_at": _iso(plan.created_at),
        "updated_at": _iso(plan.updated_at),
        "drafted_at": _iso(plan.drafted_at),
    }


def get_plan(ctx: TenantContext, plan_id: str) -> dict:
    require_member(ctx)
    with tenant_session(ctx) as session:
        return plan_view(session, ctx, load_plan(session, ctx, plan_id))


def list_plans(
    ctx: TenantContext, *, product_id: str | None = None, run_id: str | None = None, limit: int = 50,
) -> dict:
    require_member(ctx)
    with tenant_session(ctx) as session:
        statement = select(AdTestPlanRecord).where(AdTestPlanRecord.workspace_id == ctx.workspace_id)
        if run_id:
            statement = statement.where(
                AdTestPlanRecord.id.in_(
                    select(AdTestPlanRunRecord.plan_id).where(
                        AdTestPlanRunRecord.workspace_id == ctx.workspace_id,
                        AdTestPlanRunRecord.generation_run_id == run_id,
                    )
                )
            )
        if product_id:
            product = session.scalars(
                select(ProductRecord.id).where(
                    ProductRecord.workspace_id == ctx.workspace_id,
                    (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id),
                )
            ).first()
            statement = statement.where(AdTestPlanRecord.product_id == (product or product_id))
        plans = session.scalars(
            statement.order_by(AdTestPlanRecord.created_at.desc(), AdTestPlanRecord.id.desc()).limit(limit)
        ).all()
        items = []
        for plan in plans:
            directions = active_directions(load_directions(session, ctx, plan.id))
            runs = load_runs(session, ctx, plan.id)
            items.append({
                "plan_id": plan.id,
                "status": plan.status,
                "revision": plan.revision,
                "product_id": plan.product_id,
                "product_name": ((plan.recipe_json or {}).get("product") or {}).get("name") or "",
                "goal": plan.goal,
                "scale": plan.scale,
                "direction_count": len(directions),
                "run_stages": sorted(stage for stage, row in runs.items() if row.status == "submitted"),
                "created_at": _iso(plan.created_at),
                "updated_at": _iso(plan.updated_at),
            })
        return {"items": items}


# ---------------------------------------------------------------------------
# Revise
# ---------------------------------------------------------------------------

_EDITABLE_TEXT = ("title", "what_it_tests", "hypothesis", "scene_description")


def _resolve_value_refs(recipe: dict, factor_key: str, refs: list[dict], *, added: list[dict]) -> list[str]:
    factor = next((item for item in recipe["factors"] if item["key"] == factor_key), None)
    if factor is None:
        raise PlanProblem(422, "unknown_factor", f"方案中没有因素 {factor_key}")
    ids: list[str] = []
    for ref in refs:
        if ref.get("value_id"):
            value_id = str(ref["value_id"])
            if not any(value["id"] == value_id for value in factor["values"]):
                raise PlanProblem(422, "unknown_factor_value", f"因素 {factor_key} 没有取值 {value_id}")
            ids.append(value_id)
            continue
        label = drafting.clean_text(ref.get("label"), drafting.MAX_LABEL_CHARS)
        if not label:
            raise PlanProblem(422, "factor_value_invalid", "新取值需要 label 或 value_id")
        existing = next((value for value in factor["values"] if value["label"] == label), None)
        if existing is not None:
            ids.append(existing["id"])
            continue
        if len(factor["values"]) >= drafting.MAX_VALUES_PER_FACTOR:
            raise PlanProblem(422, "too_many_factor_values", f"因素 {factor_key} 最多 {drafting.MAX_VALUES_PER_FACTOR} 个取值")
        numbers = [int(value["id"].rsplit("-", 1)[-1]) for value in factor["values"] if value["id"].rsplit("-", 1)[-1].isdigit()]
        value_id = f"{factor_key}-{max(numbers, default=0) + 1}"
        value = {
            "id": value_id,
            "label": label,
            # The user's own wording is the provider-neutral fragment; every
            # adapter translates fragments into its own call (contract §5.1).
            "positive_fragments": [label],
            "negative_fragments": [],
            "proposed_by": "user",
        }
        factor["values"].append(value)
        added.append({"factor": factor_key, "value_id": value_id, "label": label})
        ids.append(value_id)
    if not ids:
        raise PlanProblem(422, "direction_factor_missing", f"因素 {factor_key} 至少需要一个取值")
    return list(dict.fromkeys(ids))


def revise_plan(
    ctx: TenantContext,
    plan_id: str,
    *,
    expected_revision: int,
    remove: list[str],
    restore: list[str],
    edit: list[dict],
    add: list[dict],
    scale: str | None,
) -> dict:
    """Remove / restore / edit / add directions, re-plan, bump the revision."""
    require_member(ctx)
    if scale is not None and scale not in PLAN_SCALES:
        raise PlanProblem(422, "scale_invalid", "规模必须是 sample、small 或 full")
    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id, for_update=True)
        if plan.revision != expected_revision:
            raise PlanProblem(409, "revision_conflict", f"测试方案已被修改（当前 revision {plan.revision}）")
        if plan.status != "ready":
            raise PlanProblem(409, "test_plan_not_ready", f"测试方案尚未起草完成（当前状态 {plan.status}）")
        runs = load_runs(session, ctx, plan.id)
        if "full" in runs and runs["full"].status == "submitted":
            raise PlanProblem(409, "test_plan_locked", "完整测试已经提交，方案不能再修改；请新建一轮方案")
        if any(row.status == "materializing" for row in runs.values()):
            raise PlanProblem(409, "test_plan_run_in_progress", "正在提交生成，请稍后再修改", retryable=True)

        recipe = json.loads(json.dumps(plan.recipe_json or {}))
        directions = load_directions(session, ctx, plan.id)
        by_key = {direction.direction_key: direction for direction in directions}
        before_counts = {scale_: len(stage.selected) for scale_, stage in compute_stages(plan, directions).items()}
        now = utcnow()
        changes: dict[str, list] = {"removed": [], "restored": [], "edited": [], "added": [], "new_values": []}

        for key in dict.fromkeys(remove):
            direction = by_key.get(key)
            if direction is None:
                raise PlanProblem(404, "direction_not_found", f"方向 {key} 不存在")
            if not direction.removed:
                direction.removed = True
                direction.updated_at = now
                changes["removed"].append(key)
        for key in dict.fromkeys(restore):
            direction = by_key.get(key)
            if direction is None:
                raise PlanProblem(404, "direction_not_found", f"方向 {key} 不存在")
            if direction.removed:
                direction.removed = False
                direction.updated_at = now
                changes["restored"].append(key)

        for change in edit:
            key = change.get("direction_id")
            direction = by_key.get(key)
            if direction is None:
                raise PlanProblem(404, "direction_not_found", f"方向 {key} 不存在")
            if direction.removed:
                raise PlanProblem(409, "direction_removed", f"方向 {key} 已删除，先恢复再修改")
            fields = []
            for name in _EDITABLE_TEXT:
                if change.get(name) is not None:
                    limit = drafting.MAX_TITLE_CHARS if name == "title" else drafting.MAX_TEXT_CHARS
                    value = drafting.clean_text(change[name], limit)
                    if name == "title" and not value:
                        raise PlanProblem(422, "direction_title_missing", "方向需要标题")
                    setattr(direction, name, value)
                    fields.append(name)
            if change.get("copy") is not None:
                copy = dict(direction.copy_json or {})
                for name, limit in (("headline", 200), ("primary_text", drafting.MAX_TEXT_CHARS)):
                    if change["copy"].get(name) is not None:
                        copy[name] = drafting.clean_text(change["copy"][name], limit)
                        fields.append(f"copy.{name}")
                direction.copy_json = copy
            if change.get("factor_values") is not None:
                values = dict(direction.factor_values_json or {})
                for factor_key, refs in change["factor_values"].items():
                    values[factor_key] = _resolve_value_refs(recipe, factor_key, refs, added=changes["new_values"])
                    fields.append(f"factor_values.{factor_key}")
                direction.factor_values_json = values
            direction.updated_at = now
            changes["edited"].append({"direction_id": key, "fields": fields})

        next_number = max(
            [int(d.direction_key.rsplit("-", 1)[-1]) for d in directions if d.direction_key.rsplit("-", 1)[-1].isdigit()],
            default=0,
        )
        next_ordinal = max([d.ordinal for d in directions], default=-1)
        for idea in add:
            title = drafting.clean_text(idea.get("title"), drafting.MAX_TITLE_CHARS)
            if not title:
                raise PlanProblem(422, "direction_title_missing", "新方向需要标题")
            factor_values: dict[str, list[str]] = {}
            provided = dict(idea.get("factor_values") or {})
            for factor in recipe.get("factors") or []:
                refs = provided.pop(factor["key"], None)
                factor_values[factor["key"]] = (
                    _resolve_value_refs(recipe, factor["key"], refs, added=changes["new_values"])
                    if refs else [factor["values"][0]["id"]]
                )
            if provided:
                raise PlanProblem(422, "unknown_factor", f"方案中没有因素 {sorted(provided)}")
            next_number += 1
            next_ordinal += 1
            scene = drafting.clean_text(idea.get("scene_description"), drafting.MAX_TEXT_CHARS)
            copy = idea.get("copy") or {}
            direction = AdTestPlanDirectionRecord(
                id=new_id("atd"),
                workspace_id=ctx.workspace_id,
                plan_id=plan.id,
                ordinal=next_ordinal,
                direction_key=f"dir-{next_number}",
                title=title,
                what_it_tests=drafting.clean_text(idea.get("what_it_tests"), drafting.MAX_TEXT_CHARS),
                hypothesis=drafting.clean_text(idea.get("hypothesis"), drafting.MAX_TEXT_CHARS),
                scene_description=scene,
                copy_json={
                    "headline": drafting.clean_text(copy.get("headline"), 200),
                    "primary_text": drafting.clean_text(copy.get("primary_text"), drafting.MAX_TEXT_CHARS),
                },
                factor_values_json=factor_values,
                fragments_json={"positive": [scene or title], "negative": []},
                combination_signatures_json=[],
                removed=False,
                user_added=True,
                created_at=now,
                updated_at=now,
            )
            session.add(direction)
            directions.append(direction)
            changes["added"].append(direction.direction_key)

        active = active_directions(directions)
        if not active:
            raise PlanProblem(422, "no_active_directions", "至少保留一个方向")
        if len(active) > drafting.MAX_DIRECTIONS:
            raise PlanProblem(422, "too_many_directions", f"最多 {drafting.MAX_DIRECTIONS} 个方向")
        try:
            space, _excluded = enumerate_plan_space(direction_spaces(directions), recipe.get("constraints") or [])
        except PlannerError as exc:
            raise PlanProblem(422, exc.code, exc.detail) from exc
        per_direction: dict[str, list[str]] = {}
        for combo in space:
            per_direction.setdefault(combo.direction, []).append(combo.signature)
        for direction in directions:
            if not direction.removed:
                direction.combination_signatures_json = per_direction.get(direction.direction_key, [])

        if scale is not None and scale != plan.scale:
            changes["scale"] = [plan.scale, scale]
            plan.scale = scale
        if not any(changes.values()):
            raise PlanProblem(422, "nothing_to_revise", "没有任何修改")
        plan.recipe_json = recipe
        plan.revision = plan.revision + 1
        plan.updated_at = now
        session.flush()
        after_counts = {scale_: len(stage.selected) for scale_, stage in compute_stages(plan, directions).items()}
        audit.append_event(
            session,
            event_type="test_plan.revised",
            resource_type=RESOURCE_TYPE,
            resource_id=plan.id,
            details={
                "revision": plan.revision,
                **{key: value for key, value in changes.items() if value},
                "combinations_before": before_counts,
                "combinations_after": after_counts,
            },
        )
        session.flush()
        return plan_view(session, ctx, plan)
