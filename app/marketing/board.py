"""Server board projection: angle → expression → combination → execution, plus comparison scope.

A pure function of the frozen plan and persisted execution facts, so the UI never infers who is
compared with whom or what is missing from layout. Single-image QC can run as soon as an image exists;
paired QC reads the same comparison scope as `compare_with` and waits until every counterpart has a
usable execution. Placeholders never carry an AssetVersion ID. Thumbnails are not projected here: the API signs
the 512px derivatives after its session closes (`workflow.board_thumbnail_refs`).
"""
from __future__ import annotations

from .executions import planned
from .expression_catalog import normalize_mechanism
from .plan_review import DETAILS

VERSION = "marketing-board-v1"
CORRECTION = ("needs_changes", "rejected")
# Without a frozen, current verified photo the product's appearance stays pending verification.
UNVERIFIED = {"status": "pending_verification", "reason": "no_reference", "reference_version_ids": []}


def comparison_kind(plan):
    if plan["comparison_kind"] == "specific_factor":
        return "factor"
    if plan["stage"] == "confirm_combination" or plan["comparison_kind"] == "business_retest":
        return "retest"
    return "within_angle" if plan["stage"] == "match_expressions" else "between_groups"


def current_angle(plan):
    """The angle whose expressions this round compares: most distinct arranged expressions, then plan order."""
    arranged = [(a["id"], len({c["expression_id"] for c in plan["combinations"] if c["angle_id"] == a["id"]})) for a in plan["angles"]]
    return max(arranged, key=lambda item: item[1])[0]


def factor_rows(plan):
    """Align combinations across groups by explicit angle and expression identity, never by position or name."""
    combos = {c["id"]: c for c in plan["combinations"]}
    rows = []
    for group in plan["groups"]:
        for cid in group["combination_ids"]:
            key = (combos[cid]["angle_id"], combos[cid]["expression_id"])
            row = next((r for r in rows if r["key"] == key and group["id"] not in r["members"]), None)
            if row is None:
                rows.append(row := {"key": key, "members": {}})
            row["members"][group["id"]] = cid
    return rows


def counterpart_combinations(plan, cid, rows=None):
    """Combinations the plan compares `cid` with, from the plan alone. A retest compares a combination with itself."""
    combos = {c["id"]: c for c in plan["combinations"]}
    group_of = {c: g["id"] for g in plan["groups"] for c in g["combination_ids"]}
    kind = comparison_kind(plan)
    if kind == "retest":
        return [cid]
    if kind == "within_angle":
        return [c for c in combos if combos[c]["angle_id"] == combos[cid]["angle_id"]
                and combos[c]["expression_id"] != combos[cid]["expression_id"]]
    if kind == "factor":
        row = next(r for r in (rows or factor_rows(plan)) if r["members"].get(group_of[cid]) == cid)
        if len(row["members"]) > 1:
            return [c for c in row["members"].values() if c != cid]
    return [c for c in combos if group_of[c] != group_of[cid]]


def distinct_counterparts(plan, cid):
    """Counterparts with the same angle and another expression: what `expressions_distinct` is checked against."""
    combos = {c["id"]: c for c in plan["combinations"]}
    return [c for c in counterpart_combinations(plan, cid) if combos[c]["angle_id"] == combos[cid]["angle_id"]
            and combos[c]["expression_id"] != combos[cid]["expression_id"]]


def blocked(e):
    return ("quality_changes_requested" if e["quality"] in CORRECTION else "file_qc_failed" if e["blocking"]
        else "reference_not_current" if not e["references_current"] else None)


def lineage(executions):
    """Execution numbers plus which executions are copies, original (reused) ads and independent new ones."""
    items = sorted(executions, key=lambda e: (e["created_at"], e["asset_version_id"]))
    numbers, count = execution_numbers(items)
    item_of = {e["asset_version_id"]: e for e in items}
    duplicate_of = {vid: n["duplicate_of"] for vid, n in numbers.items() if n["duplicate_of"]}
    original = {vid for vid, n in numbers.items() if item_of[n["root"]].get("reused") and vid not in duplicate_of}
    renewed = {vid for vid, n in numbers.items() if not item_of[n["root"]].get("reused") and vid not in duplicate_of}
    return items, numbers, count, duplicate_of, original, renewed


def comparison_scope(plan, executions):
    """{asset_version_id: {combination_ids, compare_with, awaiting}} for every execution of the plan.

    The board's `compare_with` and the paired QC input read this one answer. `awaiting` lists counterpart
    combinations without a usable execution yet (等待某组图). An empty scope is the plan's own statement
    that the execution has no counterpart, never a missing image.
    """
    combos = {c["id"]: c for c in plan["combinations"]}
    order = {cid: n for n, cid in enumerate(combos)}
    kind = comparison_kind(plan)
    items, _, _, duplicate_of, original, renewed = lineage(e for e in executions if e["combination_id"] in combos)
    retested = {e["combination_id"] for e in items if e["asset_version_id"] in original}
    viable = [e for e in items if e["asset_version_id"] not in duplicate_of and not blocked(e)]
    rows = factor_rows(plan) if kind == "factor" else None
    scope = {}
    for e in items:
        vid, cid = e["asset_version_id"], e["combination_id"]
        if vid in duplicate_of:
            cids, wanted = [], None  # a copy of an existing file is not a comparison subject
        elif kind == "retest":
            cids, wanted = ([cid] if cid in retested else []), renewed if vid in original else original
        else:
            cids, wanted = counterpart_combinations(plan, cid, rows), None
        usable = [x for x in viable if x["combination_id"] in cids and x["asset_version_id"] != vid
                  and (wanted is None or x["asset_version_id"] in wanted)]
        scope[vid] = {"combination_ids": cids, "compare_with": [x["asset_version_id"] for x in usable],
            "awaiting": sorted((c for c in cids if not any(x["combination_id"] == c for x in usable)), key=order.get)}
    return scope


def expression_rows(plan):
    """Shared rows need one explicit expression identity with the same name and mechanism."""
    rows = []
    for angle in plan["angles"]:
        for expression in angle["expressions"]:
            mechanism = expression.get("visual_mechanism")
            identity = (expression["name"], normalize_mechanism(mechanism) if mechanism else None)
            row = next((r for r in rows if r["expression_id"] == expression["id"] and r["identity"] == identity), None)
            if row is None:
                rows.append(row := {"expression_id": expression["id"], "name": expression["name"], "visual_mechanism": mechanism,
                    "identity": identity, "angle_ids": [], "details_missing": False})
            row["angle_ids"].append(angle["id"])
            row["details_missing"] = row["details_missing"] or any(not expression.get(key) for key in DETAILS)
    return rows


def appearance_summary(nodes):
    """Round-level product appearance: photos support visible appearance only, never certification or performance."""
    backed = [n for n in nodes if n["product_appearance"]["status"] == "reference_backed"]
    ids = [rid for n in backed for rid in n["product_appearance"]["reference_version_ids"]]
    pending = [n["combination_id"] for n in nodes if n["product_appearance"]["status"] != "reference_backed"]
    return {"status": "reference_backed" if not pending else "partial" if backed else "pending_verification",
        "reference_version_ids": list(dict.fromkeys(ids)), "pending_combination_ids": list(dict.fromkeys(pending))}


def execution_numbers(executions):
    """Stable execution numbers per combination, in generation order (created_at, then asset version ID).

    A revision keeps its execution's index and takes the next version (①v2); an identical file in the same
    combination is the same execution, never a new one. Later executions never renumber earlier ones.
    Returns ({asset_version_id: {index, version, root, duplicate_of}}, {combination_id: execution count}).
    """
    items = sorted(executions, key=lambda e: (e["created_at"], e["asset_version_id"]))
    combo_of = {e["asset_version_id"]: e["combination_id"] for e in items}
    numbers, first_file, count, versions = {}, {}, {}, {}
    for e in items:
        vid, cid, parent = e["asset_version_id"], e["combination_id"], e.get("parent_version_id")
        same = first_file.get((cid, e["content_sha256"]))
        if same:
            numbers[vid] = {**numbers[same], "duplicate_of": same}
        elif parent in numbers and combo_of[parent] == cid:
            root = numbers[parent]["root"]
            versions[root] += 1
            numbers[vid] = {"index": numbers[parent]["index"], "version": versions[root], "root": root, "duplicate_of": None}
        else:
            count[cid] = count.get(cid, 0) + 1
            versions[vid] = 1
            numbers[vid] = {"index": count[cid], "version": 1, "root": vid, "duplicate_of": None}
        first_file.setdefault((cid, e["content_sha256"]), vid)
    return numbers, count


def execution_nodes(board):
    """Every execution of a projected board, placeholders included."""
    return [x for a in board["tree"]["angles"] for e in a["expressions"] for c in e["combinations"] for x in c["executions"]]


def project(plan, executions, *, round_state, handoff=None, bindings=None, data_ad_ids=(), generating=(),
            generation_open=False, evaluation_current=False, prepared_appearance=None):
    """`executions` holds one dict per round asset version; see `workflow.round_dict` for the keys.

    `prepared_appearance` maps a combination to the product photos frozen in its prepared draft, for placeholders.
    """
    combos = {c["id"]: c for c in plan["combinations"]}
    group_of = {cid: g for g in plan["groups"] for cid in g["combination_ids"]}
    kind = comparison_kind(plan)
    items, numbers, count, duplicate_of, original, renewed = lineage(e for e in executions if e["combination_id"] in combos)
    combo_of = {e["asset_version_id"]: e["combination_id"] for e in items}
    retested = {combo_of[vid] for vid in original}
    scopes = comparison_scope(plan, items)
    rows = factor_rows(plan) if kind == "factor" else []

    handoff_entries = (handoff or {}).get("entries", {})
    all_ready = {e["combination_id"] for e in items if e["ready"]} == set(combos)
    closed = round_state == "closed"
    # The newest deliverable version of each execution, by the same `ready` the export gate checks.
    approved = {}
    for e in items:
        if e["ready"]:
            n = numbers[e["asset_version_id"]]
            approved[n["root"]] = max(approved.get(n["root"], 0), n["version"])

    def revised(e):
        """A later version of this execution is approved (①v2 after ①), so this one needs no further change (#161)."""
        n = numbers[e["asset_version_id"]]
        return approved.get(n["root"], 0) > n["version"]

    def done(ai):
        return bool(ai and ai["state"] == "succeeded" and ai["current"])

    def checking(ai):
        return bool(ai and ai["state"] in ("queued", "running", "uncertain"))

    def status(e, awaiting, entry, ad_ids, has_data):
        reason = blocked(e)
        if entry is not None:
            if reason or not e["ready"]:
                return "blocked", "review", "none", reason or "review_withdrawn"
            if handoff["state"] != "ready":
                return "ready", "export", "export" if handoff["state"] == "failed" and not closed else "none", None
            if not ad_ids:
                return "exported", "bind", "bind", None
            if not has_data:
                return "bound", "data", "import", None
            return "has_data", "none", "none" if evaluation_current else "evaluate", None
        if reason:
            if revised(e):
                # It stays blocked and out of every comparison; the approved revision carries the execution on.
                return "blocked", "none", "none", reason
            return "blocked", "review" if reason == "quality_changes_requested" else "single_qc", "revise", reason
        if e["ready"]:
            return "ready", "export", "export" if all_ready else "none", None
        single, paired = e["ai_qc"], e.get("paired_ai_qc")
        if e["quality"] == "approved" or (done(single) and done(paired)):
            return "paired_qc_done", "review", "review", None
        # A single-image check needs only this image, so it never waits for the rest of the round.
        if not done(single):
            return ("single_qc_running", "single_qc", "none", None) if checking(single) else (
                "single_qc_pending", "single_qc", "single_qc", None)
        if checking(paired):
            return "paired_qc_running", "paired_qc", "none", None
        if awaiting:
            return "paired_qc_pending", "paired_qc", "none", None
        return "single_qc_done", "paired_qc", "paired_qc", None

    def node(e):
        vid, cid = e["asset_version_id"], e["combination_id"]
        awaiting = scopes[vid]["awaiting"]
        entry = handoff_entries.get(vid)
        ad_ids = sorted((bindings or {}).get(entry, [])) if entry else []
        has_data = any(ad in data_ad_ids for ad in ad_ids)
        stage, gap, action, reason = status(e, awaiting, entry, ad_ids, has_data)
        if entry is None and (handoff is not None or closed):
            # Not in the frozen delivery: progress stays readable, but this round takes no further action on it.
            gap, action = ("none" if handoff is not None else gap), "none"

        def check(ai):
            return {k: ai[k] for k in ("id", "state", "current", "quality_suggestion")} if ai else None
        return {"asset_version_id": vid, "execution_index": numbers[vid]["index"], "execution_version": numbers[vid]["version"],
            "parent_version_id": e.get("parent_version_id"),
            "duplicate_of": duplicate_of.get(vid), "angle_id": combos[cid]["angle_id"], "expression_id": combos[cid]["expression_id"],
            "combination_id": cid, "group_id": group_of[cid]["id"], "stage": stage, "blocked_reason": reason,
            "quality": e["quality"], "ai_qc": check(e["ai_qc"]), "paired_ai_qc": check(e.get("paired_ai_qc")),
            "delivery": {"entry_id": entry, "ad_ids": ad_ids, "has_data": has_data} if entry else None,
            "product_appearance": e.get("product_appearance", UNVERIFIED),
            "compare_with": scopes[vid]["compare_with"], "awaiting_combination_ids": awaiting, "gap": gap, "next_action": action}

    def placeholder(cid, index):
        running = cid in generating
        return {"asset_version_id": None, "execution_index": index, "execution_version": 1, "parent_version_id": None, "duplicate_of": None,
            "angle_id": combos[cid]["angle_id"], "expression_id": combos[cid]["expression_id"], "combination_id": cid,
            "group_id": group_of[cid]["id"], "stage": "generating" if running else "pending_generation", "blocked_reason": None,
            "quality": None, "ai_qc": None, "paired_ai_qc": None, "delivery": None, "product_appearance": (prepared_appearance or {}).get(cid, UNVERIFIED),
            "compare_with": [], "awaiting_combination_ids": [],
            # A frozen delivery without this planned execution takes no further action on it this round.
            "gap": "none" if handoff is not None else "pending_generation",
            "next_action": "generate" if generation_open and not running and not closed else "none"}

    def executions_of(cid):
        result = [node(e) for e in items if e["combination_id"] == cid]
        # One slot per planned execution still to generate. Retest needs independent new executions beside the
        # original ad; revisions and copies do not count.
        made = count.get(cid, 0)
        fresh = len({numbers[vid]["root"] for vid in renewed if combo_of[vid] == cid}) if kind == "retest" and cid in retested else made
        result += [placeholder(cid, made + n) for n in range(1, planned(plan, combos[cid]) - fresh + 1)]
        return sorted(result, key=lambda r: (r["execution_index"], r["asset_version_id"] is None))

    expressions = expression_rows(plan)
    shared = {(r["expression_id"], a) for r in expressions if len(r["angle_ids"]) > 1 for a in r["angle_ids"]}
    details = {(r["expression_id"], a): r["details_missing"] for r in expressions for a in r["angle_ids"]}
    tree = {"angles": [{"angle_id": angle["id"], "proposition": angle["proposition"],
        "group_ids": [g["id"] for g in plan["groups"] if any(combos[c]["angle_id"] == angle["id"] for c in g["combination_ids"])],
        "expressions": [{"expression_id": x["id"], "name": x["name"], "visual_mechanism": x.get("visual_mechanism"),
            "details_missing": details[(x["id"], angle["id"])], "shared": (x["id"], angle["id"]) in shared,
            "combinations": [{"combination_id": c["id"], "group_id": group_of[c["id"]]["id"], "group_name": group_of[c["id"]]["name"],
                "headline": c["headline"], "planned_executions": planned(plan, c), "executions": executions_of(c["id"])}
                for c in plan["combinations"] if c["angle_id"] == angle["id"] and c["expression_id"] == x["id"]]}
            for x in angle["expressions"]]} for angle in plan["angles"]]}

    if kind == "factor":
        primary = {"kind": "factor", "changed": list(plan["changed"]),
            "pairs": [{"combination_ids": list(r["members"].values()), "group_ids": list(r["members"])} for r in rows if len(r["members"]) > 1],
            "unpaired_combination_ids": [cid for r in rows if len(r["members"]) == 1 for cid in r["members"].values()]}
    elif kind == "retest":
        primary = {"kind": "retest", "original_asset_version_ids": [vid for vid in combo_of if vid in original],
            "new_execution_ids": [vid for vid in combo_of if vid in renewed]}
    elif kind == "within_angle":
        angle = current_angle(plan)
        arranged = {c["expression_id"] for c in plan["combinations"] if c["angle_id"] == angle}
        expression_ids = [x["id"] for a in plan["angles"] if a["id"] == angle for x in a["expressions"] if x["id"] in arranged]
        primary = {"kind": "within_angle", "angle_id": angle, "expression_ids": expression_ids}
    else:
        primary = {"kind": "between_groups", "groups": [{"id": g["id"], "name": g["name"],
            "angle_ids": [a["id"] for a in plan["angles"] if any(combos[c]["angle_id"] == a["id"] for c in g["combination_ids"])],
            "combination_ids": list(g["combination_ids"])} for g in plan["groups"]]}
    secondary = ({"kind": "expression_coverage", "angle_id": None} if plan["stage"] == "screen_angles" and kind == "between_groups"
        else {"kind": "same_angle", "angle_id": primary["angle_id"]} if kind == "within_angle" else None)

    cols = [{"angle_id": a["id"], "proposition": a["proposition"]} for a in plan["angles"]]
    cells = [[[c["id"] for c in plan["combinations"] if c["angle_id"] == col["angle_id"] and c["expression_id"] == row["expression_id"]]
              if col["angle_id"] in row["angle_ids"] else [] for col in cols] for row in expressions]
    matrix = {"rows": [{k: row[k] for k in ("expression_id", "name", "visual_mechanism", "angle_ids", "details_missing")}
        | {"shared": len(row["angle_ids"]) > 1} for row in expressions], "cols": cols,
        "cells": [[cell or None for cell in line] for line in cells]}
    nodes = [n for a in tree["angles"] for x in a["expressions"] for c in x["combinations"] for n in c["executions"]]
    return {"version": VERSION, "question": plan["question"], "stage": plan["stage"], "comparison_kind": plan["comparison_kind"],
        "primary_comparison": primary, "secondary_observation": secondary, "tree": tree, "matrix": matrix,
        "product_appearance": appearance_summary(nodes)}


# Board readers that predate the single/paired split (no include_qc_kinds) keep the J-11 stage and action names.
LEGACY_STAGES = {"single_qc_running": "single_qc_pending", "single_qc_done": "paired_qc_pending",
                 "paired_qc_running": "paired_qc_pending", "paired_qc_done": "single_qc_done"}
LEGACY_ACTIONS = {"single_qc": "ai_qc", "paired_qc": "ai_qc"}


def legacy_qc_projection(board):
    for node in (n for a in board["tree"]["angles"] for x in a["expressions"] for c in x["combinations"] for n in c["executions"]):
        node.pop("paired_ai_qc", None)
        node["stage"] = LEGACY_STAGES.get(node["stage"], node["stage"])
        node["next_action"] = LEGACY_ACTIONS.get(node["next_action"], node["next_action"])
    return board
