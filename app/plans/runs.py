"""Materialise a test-plan stage into a generation Run (Issue #163).

``POST /test-plans/{id}/runs {stage}`` orchestrates only existing domain
commands (ADR 0011 "front flow orchestrates, never bypasses"):

1. **AI dimension template** -- one ``dimension_template`` Library entry per
   plan (``source: "ai"``). Its dimensions are ``direction`` plus every
   factor, with the values the active directions use; ``excludes``
   constraints bind each direction to its own factor values (and carry the
   plan constraints), so the template's constraint-pruned space *is* the
   plan space. A new version is published server-side only when the content
   changed; publishing needs no MFA after login (ADR 0015), and the user's
   click on "generate" is the confirmation the AI proposal needs (contract
   §6).
2. **Generation draft** -- ``selectors.dimension_template_ids``, provider /
   model / mode / aspect ratio / variants from the Workspace's effective
   generation defaults (frozen here), ``combination_strategy`` recording
   the plan stage, and ``exclusions.rules`` that drop every plan
   combination this stage does not generate: the ones outside the stage's
   planner selection (``not_in_stage``) and, for ``full``, the ones the
   sample Run already produced (``reused_from_sample`` -- the same
   "filter already-decided signatures before cost" approach as the
   preview's ``skip_losers``). The preview's call count is therefore
   exactly *remaining combinations x variants*.
3. **Preview** (cross-checked against the planner) and **Atomic
   Submission**, which reserves real spend under BYOK, in one transaction.

Idempotency: one ``test_plan_runs`` row per (plan, stage). Replaying the
same stage returns the same Run; a failed attempt resumes (the template
entry and version are reused, the draft/preview/submission transaction is
all-or-nothing). ``full`` waits for the sample Run to settle so a
combination still in flight is never paid for twice.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AdTestPlanRunRecord, new_id, utcnow
from app.generation.draft import create_draft, get_draft
from app.generation.planner import DIRECTION_DIMENSION_ID, TEST_PLANNER_VERSION, PlanCombination
from app.generation.preview import PreviewError, create_preview
from app.generation.submission import SubmissionError, submit_generation
from app.library import service as library
from app.library.contracts import ENTRY_DIMENSION_TEMPLATE
from app.platform import audit
from app.platform.tenancy import TenantContext

from .defaults import GenerationDefaults, effective_defaults
from .service import (
    PlanProblem,
    RESOURCE_TYPE,
    _run_view,
    active_directions,
    compute_stages,
    expansion_scale,
    generated_signatures,
    load_directions,
    load_plan,
    load_runs,
    require_member,
)

STAGES = ("sample", "full")
CLAIM_TTL = timedelta(seconds=60)

REASON_NOT_IN_STAGE = "not_in_stage"
REASON_REUSED = "reused_from_sample"

#: Preview / submission refusal -> HTTP status.
_STATUS_FOR_CODE = {
    "credential_required": 409,
    "guardrail_exceeded": 409,
    "spend_guardrail_exceeded": 409,
    "mode_unavailable": 409,
    "model_unavailable": 409,
    "entitlement_unavailable": 403,
}


def _sha(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Template content and generation identities
# ---------------------------------------------------------------------------


def template_content(plan, directions) -> dict:
    """The AI dimension template for the plan's active directions."""
    recipe = dict(plan.recipe_json or {})
    active = active_directions(directions)
    used: dict[str, set[str]] = {}
    for direction in active:
        for key, value_ids in (direction.factor_values_json or {}).items():
            used.setdefault(key, set()).update(value_ids)
    dimensions = [{
        "id": DIRECTION_DIMENSION_ID,
        "name": "创意方向",
        "values": [
            {
                "id": direction.direction_key,
                "name": direction.title,
                "positive_fragments": list((direction.fragments_json or {}).get("positive") or [direction.title]),
                "negative_fragments": list((direction.fragments_json or {}).get("negative") or []),
            }
            for direction in active
        ],
    }]
    template_values: dict[str, list[str]] = {}
    for factor in recipe.get("factors") or []:
        values = [value for value in factor.get("values") or [] if value["id"] in used.get(factor["key"], set())]
        if not values:
            continue
        template_values[factor["key"]] = [value["id"] for value in values]
        dimensions.append({
            "id": factor["key"],
            "name": factor.get("label") or factor["key"],
            "values": [
                {
                    "id": value["id"],
                    "name": value["label"],
                    "positive_fragments": list(value.get("positive_fragments") or [value["label"]]),
                    "negative_fragments": list(value.get("negative_fragments") or []),
                }
                for value in values
            ],
        })
    constraints: list[dict] = []
    for direction in active:
        for key, value_ids in template_values.items():
            allowed = set((direction.factor_values_json or {}).get(key) or [])
            for value_id in value_ids:
                if value_id not in allowed:
                    constraints.append({
                        "type": "excludes",
                        "dimension_id_a": DIRECTION_DIMENSION_ID, "value_id_a": direction.direction_key,
                        "dimension_id_b": key, "value_id_b": value_id,
                    })
    for constraint in recipe.get("constraints") or []:
        a, b = constraint.get("factor_a"), constraint.get("factor_b")
        if constraint.get("value_a") in template_values.get(a, []) and constraint.get("value_b") in template_values.get(b, []):
            constraints.append({
                "type": "excludes",
                "dimension_id_a": a, "value_id_a": constraint["value_a"],
                "dimension_id_b": b, "value_id_b": constraint["value_b"],
            })
    return {
        "source": "ai",
        "generated_by": {
            "kind": "test_plan",
            "test_plan_id": plan.id,
            "planner_version": TEST_PLANNER_VERSION,
        },
        "dimensions": dimensions,
        "constraints": constraints,
        "concept_key_mappings": [],
        "combination_cardinality": "one_per_dimension",
    }


def generation_dimensions(entry_id: str, combo: PlanCombination) -> dict[str, str]:
    """The combination as the preview names it (``<entry>:<dimension>``)."""
    return {f"{entry_id}:{key}": value for key, value in combo.dimensions().items()}


def generation_fingerprint(entry_id: str, combo: PlanCombination) -> str:
    """Same formula as ``planner.enumerate_full`` -> the RunItem signature."""
    dims = generation_dimensions(entry_id, combo)
    return ":".join(f"{key}={dims[key]}" for key in sorted(dims))


def ensure_template_entry(ctx: TenantContext, plan_id: str, content: dict | None = None) -> str:
    """The plan's AI dimension template *entry* id, created once (a Draft only).

    The entry id is the stable half of every concept key the plan's
    combinations carry (``<entry>:<factor>``), so a confirmation that tags an
    external creative with this plan's values (Issue #168) needs it before
    the plan ever generates. Creating the entry publishes nothing: the
    template's first immutable version still waits for the user's
    "generate" click (contract §6, AI proposals need a confirmation).
    """
    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id)
        if plan.template_entry_id:
            return plan.template_entry_id
        if content is None:
            content = template_content(plan, load_directions(session, ctx, plan.id))
    created = library.create_entry(
        ctx,
        entry_type=ENTRY_DIMENSION_TEMPLATE,
        display_name=f"AI 测试方案 {plan_id[-8:]}",
        content=content,
        idempotency_key=f"test-plan:{plan_id}:template",
        request_fingerprint=_sha({"test_plan_id": plan_id, "step": "template"}),
    )
    entry_id = created["id"]
    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id, for_update=True)
        if not plan.template_entry_id:
            plan.template_entry_id = entry_id
        return plan.template_entry_id


def _ensure_template(ctx: TenantContext, plan_id: str, entry_id: str | None, content: dict) -> tuple[str, str]:
    """Create (once) and publish (when changed) the plan's AI template."""
    if entry_id is None:
        entry_id = ensure_template_entry(ctx, plan_id, content)
    entry = library.get_entry(ctx, entry_id)
    current_id = entry.get("current_version_id")
    if current_id:
        current = next((v for v in entry["versions"] if v["id"] == current_id), None)
        if current is not None and current.get("content") == content:
            return entry_id, current_id
    draft = entry.get("draft") or {}
    revision = int(draft.get("revision") or 1)
    if (draft.get("content") or {}) != content:
        saved = library.save_draft(ctx, entry_id, content, revision)
        revision = int(saved["revision"])
    try:
        version = library.publish(ctx, entry_id, revision)
    except library.LibraryValidationError as invalid:
        raise PlanProblem(422, "template_invalid", f"AI 维度模板未通过校验：{invalid}") from invalid
    return entry_id, version["id"]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _selection(plan, directions, runs, stage: str, session, ctx) -> dict:
    stages = compute_stages(plan, directions)
    target = "sample" if stage == "sample" else expansion_scale(plan.scale)
    stage_plan = stages[target]
    reused: list[str] = []
    if stage == "full":
        generated, in_flight = generated_signatures(session, ctx, runs.get("sample"))
        if in_flight:
            raise PlanProblem(
                409, "sample_run_in_progress", "样图还在生成中，完成后再展开完整测试", retryable=True,
            )
        reused = [combo.signature for combo in stage_plan.selected if combo.signature in generated]
    remaining = [combo for combo in stage_plan.selected if combo.signature not in set(reused)]
    if not remaining:
        raise PlanProblem(409, "nothing_to_generate", "这一档的组合都已经生成过了")
    return {
        "target_scale": target,
        "space": [combo.signature for combo in stage_plan.space],
        "selected": [combo.signature for combo in stage_plan.selected],
        "remaining": [combo.signature for combo in remaining],
        "reused": reused,
        "untested": stage_plan.untested_summary(
            {direction.direction_key: direction.title for direction in directions}
        ),
        "_space": stage_plan.space,
    }


def _exclusion_rules(entry_id: str, space: list[PlanCombination], remaining: set[str], reused: set[str]) -> tuple[list[dict], dict]:
    rules = []
    fingerprints: dict[str, str] = {}
    for combo in space:
        fingerprints[generation_fingerprint(entry_id, combo)] = combo.signature
        if combo.signature in remaining:
            continue
        rules.append({
            "dimensions": generation_dimensions(entry_id, combo),
            "reason": REASON_REUSED if combo.signature in reused else REASON_NOT_IN_STAGE,
        })
    return rules, fingerprints


def _fail(ctx: TenantContext, row_id: str, plan_id: str, stage: str, code: str, detail: str) -> None:
    with tenant_session(ctx) as session:
        row = session.get(AdTestPlanRunRecord, row_id)
        if row is None or row.workspace_id != ctx.workspace_id or row.status == "submitted":
            return
        row.status = "failed"
        row.failure_code = code
        row.failure_detail = detail[:1000]
        row.updated_at = utcnow()
        audit.append_event(
            session,
            event_type="test_plan.run_failed",
            resource_type=RESOURCE_TYPE,
            resource_id=plan_id,
            details={"stage": stage, "code": code, "detail": detail[:300], "test_plan_run_id": row_id},
        )


def start_run(ctx: TenantContext, plan_id: str, *, stage: str, idempotency_key: str) -> dict:
    """Materialise ``stage`` of the plan; replays return the same Run."""
    require_member(ctx)
    if stage not in STAGES:
        raise PlanProblem(422, "stage_invalid", "stage 必须是 sample 或 full")

    # Step 0: claim (or replay / resume) the stage under the plan row lock.
    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id, for_update=True)
        if plan.status != "ready":
            raise PlanProblem(409, "test_plan_not_ready", f"测试方案尚未起草完成（当前状态 {plan.status}）")
        runs = load_runs(session, ctx, plan.id)
        row = runs.get(stage)
        if row is not None and row.status == "submitted":
            return {**_run_view(session, ctx, row), "plan_id": plan.id, "replayed": True}
        if row is not None and row.status == "materializing":
            updated = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=utcnow().tzinfo)
            if utcnow() - updated < CLAIM_TTL:
                raise PlanProblem(409, "test_plan_run_in_progress", "这一档正在提交生成", retryable=True)
        if stage == "full" and "sample" in runs and runs["sample"].status == "materializing":
            raise PlanProblem(409, "sample_run_in_progress", "样图正在提交，完成后再展开完整测试", retryable=True)
        directions = load_directions(session, ctx, plan.id)
        if not active_directions(directions):
            raise PlanProblem(422, "no_active_directions", "至少保留一个方向")
        selection = _selection(plan, directions, runs, stage, session, ctx)
        space = selection.pop("_space")
        defaults: GenerationDefaults = effective_defaults(session, ctx)
        variants = defaults.variants_for(selection["target_scale"])
        images = len(selection["remaining"]) * variants
        counts = {
            "combinations": len(selection["remaining"]),
            "reused_combinations": len(selection["reused"]),
            "variants": variants,
            "images": images,
            "max_cost": defaults.max_cost(
                images,
                reference_images=defaults.reference_images_per_call(session, ctx, plan.product_id),
                qc_references=defaults.quality_check_references(session, ctx, plan.product_id),
            ),
        }
        now = utcnow()
        if row is None:
            row = AdTestPlanRunRecord(
                id=new_id("atr"),
                workspace_id=ctx.workspace_id,
                plan_id=plan.id,
                stage=stage,
                created_by=ctx.initiator_id,
                created_at=now,
            )
            session.add(row)
        row.target_scale = selection["target_scale"]
        row.status = "materializing"
        row.plan_revision = plan.revision
        row.defaults_json = defaults.frozen()
        row.selection_json = {**selection, "counts": counts}
        row.failure_code = None
        row.failure_detail = None
        row.updated_at = now
        session.flush()
        row_id = row.id
        audit.append_event(
            session,
            event_type="test_plan.run_requested",
            resource_type=RESOURCE_TYPE,
            resource_id=plan.id,
            details={
                "stage": stage,
                "target_scale": selection["target_scale"],
                "plan_revision": plan.revision,
                "test_plan_run_id": row_id,
                **{key: value for key, value in counts.items() if key != "max_cost"},
                "max_cost": counts["max_cost"],
                "execution_mode": defaults.execution_mode,
            },
        )
        content = template_content(plan, directions)
        template_entry_id = plan.template_entry_id
        product_id = plan.product_id

    # Step 1: the AI dimension template (Library service, its own transactions).
    try:
        template_entry_id, template_version_id = _ensure_template(ctx, plan_id, template_entry_id, content)
    except PlanProblem as problem:
        _fail(ctx, row_id, plan_id, stage, problem.code, problem.message)
        raise
    except library.LibraryError as error:
        _fail(ctx, row_id, plan_id, stage, "template_publish_failed", str(error))
        raise PlanProblem(409, "template_publish_failed", f"AI 维度模板发布失败：{error}", retryable=True) from error

    rules, fingerprints = _exclusion_rules(
        template_entry_id, space, set(selection["remaining"]), set(selection["reused"]),
    )

    # Step 2: draft -> preview -> submission, all or nothing.
    try:
        with tenant_session(ctx) as session:
            row = session.get(AdTestPlanRunRecord, row_id, with_for_update=True)
            draft = create_draft(
                session, ctx,
                product_id=product_id,
                provider=defaults.provider,
                model=defaults.model,
                execution_mode=defaults.execution_mode,
                combination_strategy={
                    "kind": "full",
                    "test_plan": {
                        "test_plan_id": plan_id,
                        "stage": stage,
                        "scale": selection["target_scale"],
                        "planner_version": TEST_PLANNER_VERSION,
                    },
                },
                format_config=defaults.format_config(selection["target_scale"]),
                exclusions={"rules": rules},
                selectors={"dimension_template_ids": [template_entry_id]},
            )
            preview = create_preview(session, ctx, draft.id, draft.revision)
            projected = (preview.snapshot_json or {}).get("ui_projections") or {}
            if projected.get("combination_count") != len(selection["remaining"]):
                raise PlanProblem(
                    500, "planner_preview_mismatch",
                    f"预览组合数 {projected.get('combination_count')} 与方案 {len(selection['remaining'])} 不一致",
                )
            submitted = submit_generation(
                session, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=get_draft(session, ctx, draft.id).revision,
                idempotency_key=f"test-plan-run:{row_id}",
            )
            row.template_entry_id = template_entry_id
            row.template_version_id = template_version_id
            row.generation_draft_id = draft.id
            row.preview_id = preview.id
            row.generation_run_id = submitted["generation_run_id"]
            row.operation_id = submitted["operation_id"]
            row.selection_json = {
                **dict(row.selection_json or {}),
                "fingerprints": fingerprints,
                "preview": {
                    "call_count": projected.get("call_count"),
                    "combination_count": projected.get("combination_count"),
                    "maximum_cost": projected.get("maximum_cost"),
                    "excluded_count": (projected.get("exclusion_summary") or {}).get("excluded_count"),
                },
            }
            row.status = "submitted"
            row.updated_at = utcnow()
            audit.append_event(
                session,
                event_type="test_plan.run_submitted",
                resource_type=RESOURCE_TYPE,
                resource_id=plan_id,
                details={
                    "stage": stage,
                    "test_plan_run_id": row_id,
                    "generation_run_id": submitted["generation_run_id"],
                    "operation_id": submitted["operation_id"],
                    "template_entry_id": template_entry_id,
                    "template_version_id": template_version_id,
                    "generation_draft_id": draft.id,
                    "preview_id": preview.id,
                    "call_count": projected.get("call_count"),
                    "reused_combinations": len(selection["reused"]),
                    "execution_mode": defaults.execution_mode,
                },
            )
            session.flush()
            return {**_run_view(session, ctx, row), "plan_id": plan_id, "replayed": False}
    except (PreviewError, SubmissionError) as error:
        _fail(ctx, row_id, plan_id, stage, error.code, error.detail)
        raise PlanProblem(_STATUS_FOR_CODE.get(error.code, 422), error.code, error.detail) from error
    except PlanProblem as problem:
        _fail(ctx, row_id, plan_id, stage, problem.code, problem.message)
        raise
