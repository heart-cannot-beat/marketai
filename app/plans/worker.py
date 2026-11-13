"""The ``test_plans.draft`` Job: draft a plan's directions (Issues #163, #164).

One step: build the drafter inputs from the plan's frozen recipe (product
facts, audience, goal, scale, seed assets / winners / losers), call the
drafter (:func:`app.plans.drafting.resolve_plan_drafter` -- the governed LLM
drafter for a ``byok`` plan, the mock drafter otherwise), normalize and plan
the result, then write the factor catalog, the constraints and the direction
rows and mark the plan ``ready`` -- in one transaction, so a crash leaves
either no directions or all of them and a retried Job can simply run again
(the governed call replays its stored answer instead of paying twice, and
the mapping is deterministic, so the replay yields the same ids).

A drafter or normalization failure ends the plan ``failed`` with a stable
code and a ``suggested_action`` (``bind_openai_key``,
``adjust_spend_guardrail`` or ``retry``, intake's vocabulary) recorded in
the plan's drafting evidence; the Operation folds to ``failed`` from the
Job. A paid call whose outcome is unknown leaves the Job ``uncertain`` for
reconciliation. A plan that fails before its call releases the call's
reservation. A plan that is no longer ``drafting`` (a replayed Job) is a
no-op success.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AdTestPlanDirectionRecord, new_id, utcnow
from app.generation.planner import PlanDirectionSpace, PlannerError, enumerate_plan_space
from app.platform import audit
from app.platform.tenancy import TenantContext

from . import drafting
from .service import RESOURCE_TYPE, failure_action, load_plan
from app.platform.runtime_role import WORKER_ROLE, runs_as


@dataclass(frozen=True)
class DraftOutcome:
    kind: str  # "ready" | "failed" | "uncertain"
    code: str | None = None
    detail: str | None = None


def _inputs(plan) -> tuple:
    recipe = dict(plan.recipe_json or {})
    audience = recipe.get("audience") or {}
    return (
        drafting.ProductFacts.from_dict(recipe.get("product") or {}),
        plan.goal,
        drafting.AudienceChoice(
            text=audience.get("text") or plan.audience_text,
            card_index=audience.get("card_index"),
            card=audience.get("card"),
        ),
        plan.scale,
        drafting.WinnersLosers.from_dict(recipe.get("seeds")),
    )


def _release_unused_reservation(session, ctx: TenantContext, plan) -> bool:
    """Give back a ``byok`` plan's reserved LLM maximum its call never used.

    Only a reservation no attempt consumed is released; one an attempt holds
    is settled (or reconciled) by the coordinator.
    """
    admission = (plan.evidence_json or {}).get("admission") or {}
    if admission.get("drafting_mode") != "byok" or not plan.job_id:
        return False
    from app.providers.llm_chat import release_llm_chat_reservation

    from .llm_drafter import CALL_NAME

    return release_llm_chat_reservation(session, ctx, job_id=plan.job_id, call_name=CALL_NAME)


@runs_as(WORKER_ROLE)
def mark_plan_failed(
    ctx: TenantContext,
    plan_id: str,
    *,
    code: str,
    detail: str,
    call: dict | None = None,
) -> None:
    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id, for_update=True)
        if plan.status != "drafting":
            return
        now = utcnow()
        plan.status = "failed"
        plan.failure_code = code
        plan.failure_detail = detail[:1000]
        plan.updated_at = now
        evidence = dict(plan.evidence_json or {})
        drafting_evidence = {
            **dict(evidence.get("drafting") or {}),
            "status": "failed",
            "code": code,
            "suggested_action": failure_action(code),
        }
        if call:
            drafting_evidence["call"] = call
        evidence["drafting"] = drafting_evidence
        plan.evidence_json = evidence
        released = _release_unused_reservation(session, ctx, plan)
        audit.append_event(
            session,
            event_type="test_plan.draft_failed",
            resource_type=RESOURCE_TYPE,
            resource_id=plan_id,
            details={"code": code, "detail": detail[:300], "reservation_released": released},
        )


@runs_as(WORKER_ROLE)
async def run_plan_drafting(
    ctx: TenantContext,
    *,
    plan_id: str,
    drafter: drafting.PlanDrafter | None = None,
    llm_client=None,
) -> DraftOutcome:
    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id)
        if plan.status != "drafting":
            return DraftOutcome("ready")
        inputs = _inputs(plan)
        mode = ((plan.evidence_json or {}).get("admission") or {}).get("drafting_mode", "mock")
        job_id, operation_id = plan.job_id, plan.operation_id

    chosen = drafter
    started = time.monotonic()
    try:
        if chosen is None:
            chosen = drafting.resolve_plan_drafter(
                mode, ctx=ctx, llm_client=llm_client, job_id=job_id, operation_id=operation_id,
            )
        draft = await chosen.draft_plan(*inputs)
        normalized = drafting.normalize_plan_draft(draft)
        directions = normalized.directions
        space, excluded = enumerate_plan_space(
            [PlanDirectionSpace(item["direction_key"], item["factor_values"]) for item in directions],
            normalized.constraints,
        )
    except drafting.DraftingFailed as failed:
        code = failed.code or "drafting_failed"
        mark_plan_failed(ctx, plan_id, code=code, detail=failed.detail,
                         call=getattr(chosen, "call_evidence", None) or None)
        if failed.dispatch_certainty == "uncertain":
            return DraftOutcome("uncertain", code, failed.detail)
        return DraftOutcome("failed", code, failed.detail)
    except (drafting.DraftInvalid, PlannerError) as invalid:
        mark_plan_failed(ctx, plan_id, code="draft_invalid", detail=f"{invalid.code}: {invalid.detail}",
                         call=getattr(chosen, "call_evidence", None) or None)
        return DraftOutcome("failed", "draft_invalid", invalid.detail)
    duration_ms = int((time.monotonic() - started) * 1000)

    per_direction: dict[str, list[str]] = {}
    for combo in space:
        per_direction.setdefault(combo.direction, []).append(combo.signature)
    empty = [item["title"] for item in directions if not per_direction.get(item["direction_key"])]
    if empty:
        detail = f"constraints leave no combination for {empty}"
        mark_plan_failed(ctx, plan_id, code="draft_invalid", detail=detail, call=draft.evidence.get("call"))
        return DraftOutcome("failed", "draft_invalid", detail)

    with tenant_session(ctx) as session:
        plan = load_plan(session, ctx, plan_id, for_update=True)
        if plan.status != "drafting":
            return DraftOutcome("ready")
        now = utcnow()
        for item in directions:
            session.add(AdTestPlanDirectionRecord(
                id=new_id("atd"),
                workspace_id=ctx.workspace_id,
                plan_id=plan_id,
                ordinal=item["ordinal"],
                direction_key=item["direction_key"],
                title=item["title"],
                what_it_tests=item["what_it_tests"],
                hypothesis=item["hypothesis"],
                scene_description=item["scene_description"],
                copy_json=item["copy"],
                factor_values_json=item["factor_values"],
                fragments_json=item["fragments"],
                combination_signatures_json=per_direction[item["direction_key"]],
                removed=False,
                user_added=False,
                created_at=now,
                updated_at=now,
            ))
        recipe = dict(plan.recipe_json or {})
        recipe["factors"] = [
            {**factor, "values": [{**value, "proposed_by": "ai"} for value in factor["values"]]}
            for factor in normalized.factors
        ]
        recipe["constraints"] = normalized.constraints
        plan.recipe_json = recipe
        evidence = dict(plan.evidence_json or {})
        evidence["drafting"] = {
            "status": "succeeded",
            "drafter": draft.drafter,
            "drafter_version": draft.drafter_version,
            "duration_ms": duration_ms,
            "direction_count": len(directions),
            "factor_count": len(normalized.factors),
            "space_count": len(space),
            "constraint_excluded_count": len(excluded),
            **dict(draft.evidence or {}),
        }
        plan.evidence_json = evidence
        plan.status = "ready"
        plan.drafted_at = now
        plan.updated_at = now
        call = (draft.evidence or {}).get("call") or {}
        audit.append_event(
            session,
            event_type="test_plan.drafted",
            resource_type=RESOURCE_TYPE,
            resource_id=plan_id,
            details={
                "drafter": draft.drafter,
                "drafter_version": draft.drafter_version,
                "direction_count": len(directions),
                "space_count": len(space),
                "auto_widened_count": len((draft.evidence or {}).get("auto_widened") or []),
                **({"attempt_id": call.get("attempt_id"), "replayed": call.get("replayed")} if call else {}),
            },
        )
        session.flush()
    return DraftOutcome("ready")


__all__ = ["DraftOutcome", "mark_plan_failed", "run_plan_drafting"]
