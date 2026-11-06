"""Bounded next-round evidence: identities resolve to their actual creative meaning."""
from decimal import Decimal
from .service import MarketingError, digest


def planning_history(session, ctx, project):
    from .workflow import scoped, latest_evaluation, project_spend, ready, evaluation_current
    from .workflow_models import MarketingRoundRecord
    rounds = sorted(scoped(session, ctx, MarketingRoundRecord, project_id=project.id), key=lambda r: r.ordinal)
    if any(r.state != "closed" for r in rounds):
        raise MarketingError("round_still_open", "请先结束当前小轮，再起草下一轮；已冻结证据保持不变。", 409)
    if any((r.close_json or {}).get("reason") == "completed" for r in rounds):
        raise MarketingError("big_round_completed", "大轮已完成；新业务目标请新建测试。", 409)
    result = {"prior_rounds": [], "current_understanding_hash": digest(project.understanding_json)}
    freshness = {}
    for previous in rounds:
        plan = previous.snapshot_json["plan"]
        evaluation = latest_evaluation(session, ctx, previous)
        current = evaluation_current(session, ctx, previous, evaluation, _cache=freshness)
        evidence = evaluation.result_json if current else None
        qualified = set(evidence.get("qualified_asset_version_ids", [])) if evidence else set()
        entries = evaluation.input_json["entries"] if evaluation else []
        # Include all tested candidates, not a winner-only label summary. Failed
        # executions never become a global exclusion of an angle or expression.
        candidates = []
        for combo in plan["combinations"]:
            angle = next(a for a in plan["angles"] if a["id"] == combo["angle_id"])
            expression = next(e for e in angle["expressions"] if e["id"] == combo["expression_id"])
            candidates.append({"combination": combo, "proposition": angle["proposition"], "expression": expression,
                "executions": [{"asset_version_id": e["asset_version_id"], "content_sha256": e["content_sha256"],
                    "qualified": e["asset_version_id"] in qualified and ready(session, ctx, previous, e["asset_version_id"])}
                    for e in entries if e["combination_id"] == combo["id"]]})
        result["prior_rounds"].append({"round_id": previous.id, "stage": plan["stage"], "question": plan["question"],
            "product_understanding_hash": digest(previous.snapshot_json["understanding"]),
            "same_understanding": previous.snapshot_json["understanding"] == project.understanding_json,
            "close": previous.close_json, "execution_mode": previous.execution_mode,
            "conditions": {k: plan[k] for k in ("audience", "market", "placement", "offer", "landing_page", "currency")},
            "candidates": candidates, "evaluation_current": current,
            "result": {k: evidence.get(k) for k in ("metric", "threshold", "evidence_status", "qualified_asset_version_ids",
                "confirmed_combinations", "next_action", "limitations", "results", "relative_status")} if evidence else None})
    if rounds:
        limits = rounds[0].snapshot_json["plan"]
        spent = project_spend(session, ctx, rounds)
        remaining = Decimal(limits["media_budget"]) - spent
        count = max(0, limits["rules"]["max_small_rounds"] - len(rounds))
        if remaining <= 0 or not count:
            raise MarketingError("big_round_limit", "已达到大轮预算或次数上限；不再产生下一轮策划费用，请结束并总结。", 409)
        result["big_round_limits"] = {"media_budget": limits["media_budget"], "spent": str(spent),
            "currency": limits["currency"], "remaining_budget": str(remaining), "remaining_rounds": count, "max_small_rounds": limits["rules"]["max_small_rounds"]}
    return result


def validate_project_limits(session, ctx, project, plan):
    from .workflow import scoped
    from .workflow_models import MarketingRoundRecord
    rounds = scoped(session, ctx, MarketingRoundRecord, project_id=project.id)
    if not rounds:
        return
    cap = min(rounds, key=lambda r: r.ordinal).snapshot_json["plan"]
    if plan.currency != cap['currency']:
        raise MarketingError('big_round_currency_changed', '同一大轮的媒体预算币种不能改变；新币种请新建测试。', 409)
    if plan.media_budget != Decimal(cap["media_budget"]) or plan.rules.max_small_rounds != cap["rules"]["max_small_rounds"]:
        raise MarketingError("big_round_limits_frozen", "大轮总预算和次数上限沿用首轮确认值；后续小轮不能重置或扩大它们。")
