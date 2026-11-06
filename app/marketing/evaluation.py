"""Deterministic, scoped decisions from a frozen final checkpoint.

Wilson CTR intervals and simultaneous Bonferroni coverage:
https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm
CPA/ROAS use the existing seeded daily block bootstrap, not order totals.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.experiments.judge import judge
from .service import digest


def combination_key(snapshot, entry):
    plan = snapshot["plan"]
    combo = next(c for c in plan["combinations"] if c["id"] == entry["combination_id"])
    angle = next(a for a in plan["angles"] if a["id"] == combo["angle_id"])
    expression = next(e for e in angle["expressions"] if e["id"] == combo["expression_id"])
    context = {k: plan[k] for k in ("audience", "market", "placement", "offer", "landing_page", "currency")}
    # Keep historical default-window combination hashes byte-for-byte stable; the planned execution count is a
    # generation quantity, not creative meaning, so it never changes the key.
    if snapshot.get('attribution_window', '7d_click_1d_view') != '7d_click_1d_view':
        context['attribution_window'] = snapshot['attribution_window']
    return digest({"product_understanding_hash": digest(snapshot.get("understanding")), "combination": {k: v for k, v in combo.items() if k not in ("id", "angle_id", "expression_id", "executions")},
        "proposition": angle["proposition"], "expression": {k: v for k, v in expression.items() if k != "id"},
        "context": context,
        "metric": plan["rules"]["business_metric"], "threshold": plan["rules"]["threshold"]})


def evaluate_snapshot(inputs):
    snapshot, entries = inputs["snapshot"], inputs["entries"]
    plan, protocol = snapshot["plan"], snapshot["protocol"]
    metric = plan["rules"]["business_metric"]
    bindings = {b["ad_id"]: b for b in inputs["bindings"]}
    observed = {(r["date"], r["ad_id"]) for r in inputs["rows"]}
    days = []
    cursor, end = date.fromisoformat(inputs["start_date"]), date.fromisoformat(inputs["end_date"])
    while cursor <= end:
        days.append(str(cursor))
        cursor += timedelta(days=1)
    entities = [{"id": "group:" + g["id"], "kind": "group", "label": g["name"],
        "entry_ids": [e["entry_id"] for e in entries if e["combination_id"] in g["combination_ids"]]} for g in plan["groups"]]
    entities += [{"id": "asset:" + e["asset_version_id"], "kind": "asset", "label": e.get("display_name") or e["ad_name"],
                  "entry_ids": [e["entry_id"]]} for e in entries]
    judged = []
    for entity in entities:
        entry_ids = set(entity["entry_ids"])
        ads = {b["ad_id"] for b in inputs["bindings"] if b["entry_id"] in entry_ids}
        source_rows = [record for record in inputs["rows"] if record["ad_id"] in ads]
        entity["purchases_reported"] = bool(source_rows) and all(record.get("purchases") is not None for record in source_rows)
        reasons = []
        if entry_ids & set(inputs.get("quality_changed", [])):
            reasons.append("quality_review_changed")
        if not inputs["mature"]:
            reasons.append("final_checkpoint_not_mature")
        bound_entries = {b["entry_id"] for b in inputs["bindings"]}
        if not entry_ids or entry_ids - bound_entries:
            reasons.append("unbound_exported_assets")
        if any((day, ad) not in observed for day in days for ad in ads):
            reasons.append("missing_ad_day_data")
        if any(not bindings[ad]["conditions_match"] or bindings[ad]["deviations"] for ad in ads):
            reasons.append("delivery_conditions_changed")
        daily = defaultdict(lambda: {"impressions": 0, "clicks": 0, "spend": Decimal("0"), "purchases": Decimal("0"), "purchase_value": Decimal("0")})
        for record in inputs["rows"]:
            if record["ad_id"] not in ads:
                continue
            for field in ("impressions", "clicks", "spend", "purchases", "purchase_value"):
                value = record[field]
                if value is None:
                    if field not in ("purchase_value", "purchases") or metric == "roas" or (field == "purchases" and metric == "cpa"):
                        reasons.append("missing_metric_value")
                    continue
                daily[record["date"]][field] += Decimal(str(value))
        if sum((d["spend"] for d in daily.values()), Decimal("0")) < Decimal(protocol["min_spend"]):
            reasons.append("insufficient_spend")
        if sum((d["clicks"] for d in daily.values()), Decimal("0")) < protocol["min_clicks"]:
            reasons.append("insufficient_clicks")
        judged.append({"id": entity["id"], "daily": [{k: str(v) for k, v in item.items()} for _, item in sorted(daily.items())],
                       "data_quality_reasons": reasons})
    config = {"primary_metric": metric, "alpha": str(Decimal(protocol["alpha"]) / max(1, len(entities))),
        "bootstrap_samples": 4000, "min_days": protocol["min_days"], "min_impressions": protocol["min_impressions"],
        "min_clicks": protocol["min_clicks"], "min_purchases": protocol["min_purchases"],
        metric + "_threshold": plan["rules"]["threshold"]}
    output = judge({"experiment": {"config": config}, "snapshot_hash": digest(inputs), "entries": judged})
    results = []
    for meta, result in zip(entities, output["entries"]):
        # judge sorts IDs. Resolve by immutable entity ID, never list position.
        result = next(r for r in output["entries"] if r["entry_id"] == meta["id"])
        verdict = {"win": "passes", "lose": "does_not_pass", "learn": "uncertain", "inconclusive": "insufficient"}[result["verdict"]]
        if metric in ("cpa", "roas") and result["interval_low"] is not None and result["interval_low"] == result["interval_high"]:
            verdict = "uncertain"
            result["reason_codes"] = [*result["reason_codes"], "degenerate_daily_uncertainty"]
        results.append({**meta, **{k: v for k, v in result.items() if k != "verdict"}, "business_status": verdict,
                        "conclusion_scope": "asset" if meta["kind"] == "asset" else "tested_group_of_combinations"})
    assets = [r for r in results if r["kind"] == "asset"]
    groups = [r for r in results if r["kind"] == "group"]
    relative = "not_compared" if plan["comparison_kind"] == "business_retest" else "insufficient"
    relative_ids = []
    spends = [Decimal(g["totals"]["spend"]) for g in groups]
    comparable = bool(groups) and all(g["business_status"] != "insufficient" and "degenerate_daily_uncertainty" not in g["reason_codes"] for g in groups)
    if spends and (min(spends) <= 0 or max(spends) / max(Decimal("0.000001"), min(spends)) > Decimal(protocol["max_spend_ratio"])):
        comparable = False
    if protocol["allocation"] == "observational":
        comparable = False
    if comparable and relative != "not_compared":
        relative = "no_clear_difference"
        for group in groups:
            low, high = group["interval_low"], group["interval_high"]
            if low is None or high is None:
                continue
            others = [g for g in groups if g["id"] != group["id"]]
            separated = all(g["interval_low"] is not None and g["interval_high"] is not None and (
                Decimal(high) < Decimal(g["interval_low"]) if metric == "cpa" else Decimal(low) > Decimal(g["interval_high"])) for g in others)
            if separated and others:
                relative_ids.append(group["id"])
        if relative_ids:
            relative = "separated_under_observed_conditions"
    qualified = [r["id"].removeprefix("asset:") for r in assets if r["business_status"] == "passes"] if metric != "ctr" and inputs["execution_mode"] == "byok" else []
    confirmations = []
    prior = inputs.get("prior_confirmations", [])
    if qualified and plan["stage"] == "confirm_combination":
        for entry in entries:
            if entry["asset_version_id"] not in qualified:
                continue
            key = combination_key(snapshot, entry)
            matches = [p for p in prior if p["combination_key"] == key and p["content_sha256"] != entry["content_sha256"]
                       and p["timezone"] == inputs["timezone"] and p["protocol"] == protocol]
            if matches:
                confirmations.append({"combination_id": entry["combination_id"], "combination_key": key,
                    "current_asset_version_id": entry["asset_version_id"], "supporting_evaluations": sorted({p["evaluation_id"] for p in matches}),
                    "scope": "repeatable_under_recorded_conditions"})
    budget = sum((Decimal(r["spend"]) for r in inputs["rows"]), Decimal("0"))
    limits = list(plan["limitations"]) + [
        "CSV was supplied by the user; Meta provenance is not independently verified.",
        "Relative performance does not establish a causal effect of an angle or an element.",
        "Unobserved combinations remain unknown; approval is a quality decision.",
        "CPA/ROAS intervals use a seeded daily bootstrap and are approximate.",
    ]
    if not inputs["rows"]:
        limits.append("No delivery data has been supplied; recorded spend is not proof of zero external spend or that ads ran.")
    if inputs["execution_mode"] == "mock":
        limits.append("Simulated data cannot become validated marketing knowledge.")
    if plan.get("style_variation") == "tested":
        limits.append("Visual style was an actively tested change; differences include style and cannot be attributed to the angle alone.")
    elif inputs.get("style_deviations"):
        limits.append(f"Visual style was meant to be held constant, but quality checks found {len(inputs['style_deviations'])} "
                      "creative(s) breaking the brand baseline or style; differences cannot be attributed to the angle.")
    next_action = "inspect_data_gaps_or_close" if any(r["business_status"] == "insufficient" for r in groups) else \
        "confirm_new_execution" if qualified else "revise_hypotheses_or_close"
    if qualified and plan["stage"] == "screen_angles":
        next_action = "match_expressions_for_candidates"
    if confirmations:
        next_action = "retain_confirmed_combinations_and_close"
    if budget >= Decimal(plan["small_round_budget"]):
        next_action = "close_or_plan_within_remaining_budget"
    return {"algorithm_version": "marketing-eval-v1", "metric": metric, "threshold": plan["rules"]["threshold"],
        'currency': snapshot.get('currency', plan['currency']),
        'attribution_window': snapshot.get('attribution_window', '7d_click_1d_view'), 'account_timezone': inputs['timezone'],
        "evidence_status": "simulated" if inputs["execution_mode"] == "mock" else "not_supplied" if not inputs["rows"] else "user_supplied_delivery",
        "results": results, "relative_status": relative, "relative_group_ids": relative_ids,
        "qualified_asset_version_ids": qualified, "confirmed_combinations": confirmations,
        "all_assets_failed": bool(assets) and all(r["business_status"] == "does_not_pass" for r in assets) and metric != "ctr",
        "round_spend": str(budget), "small_round_budget": plan["small_round_budget"],
        "mature": inputs["mature"], "next_action": next_action, "limitations": limits,
        "factor_conclusion": "conditional_association_only" if plan["comparison_kind"] == "specific_factor" and relative_ids else "not_established",
        "conditions": {k: plan[k] for k in ("audience", "market", "placement", "offer", "landing_page")}}
