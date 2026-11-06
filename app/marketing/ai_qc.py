"""Prepare exact-image AI suggestions, then let the human confirm or refine."""
from __future__ import annotations
import difflib
import os
import re
import unicodedata
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from sqlalchemy import or_, select

from app.adapters.persistence.models import new_id
from app.generation.paid_execution import asset_blob_ref
from app.generation.selection import authorize_paid, check_budget, credential_binding, SelectionError
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import ReservationBook
from app.entitlements.types import BillableCallSpec, ReservationPlan
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation
from .ai_qc_contracts import AICheckTranscribedReport, report_model
from .ai_qc_models import MarketingAIQCRecord as Call
from .languages import output_language
from .service import MarketingError, digest, ensure_active
from . import ai_qc_text as text, workflow

POLICY = "marketing-vision-qc-v1"  # one call per image for every check, before the single/paired split
# v2 writes in the round's strategy language (J-43); calls frozen before that keep v1 and its Chinese prompts.
# single v3 also transcribes the rendered copy for the server to compare (#294); a v2 call keeps its own prompt and
# schema, so it stays current and is never billed again just to transcribe.
POLICIES = {"single": "marketing-vision-qc-single-v3", "paired": "marketing-vision-qc-paired-v2"}
LEGACY_POLICIES = {None: POLICY, "single": "marketing-vision-qc-single-v1", "paired": "marketing-vision-qc-paired-v1"}
MAX_TEXT_BYTES = 96000
MAX_IMAGES = 3
MAX_COMPARISONS = 4
NORMALIZATION_POLICY = "marketing-evidence-gates-v4"
COPY_FIELDS = ("headline", "body_copy")
MAX_COPY_CHANGES = 8
# Ideographs and kana are words of their own; other scripts are compared word by word. `%` carries meaning in ad copy.
_IDEOGRAPHS = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_WORD = re.compile(f"[{_IDEOGRAPHS}]|[^\\W_{_IDEOGRAPHS}]+|%")


class QCOutputError(ValueError):
    """A returned QC output the server refuses; `field` names where, for the call's error detail (#295)."""
    def __init__(self, code, field=None):
        super().__init__(code)
        self.code, self.field = code, field


def comparable(value):
    """Copy as the server compares it: case, whitespace and punctuation never count."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in folded if not c.isspace() and (c == "%" or not unicodedata.category(c).startswith("P")))


def copy_changes(expected, rendered):
    """The words that differ, confirmed vs rendered, at most MAX_COPY_CHANGES; one whole-text change when only symbols differ."""
    words = [_WORD.findall(unicodedata.normalize("NFKC", value)) for value in (expected, rendered)]
    folded = [[w.casefold() for w in side] for side in words]
    changes = [{"expected": " ".join(words[0][a1:a2])[:120], "rendered": " ".join(words[1][b1:b2])[:120]}
        for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, *folded, autojunk=False).get_opcodes() if op != "equal"]
    return changes[:MAX_COPY_CHANGES] or [{"expected": expected[:120], "rendered": rendered[:120]}]


def rendered_copy_gate(finding, rendered, combination, fix, separator):
    """copy_consistent against the verbatim transcription (#294). A word that differs fails; a field the model could not
    transcribe holds a pass back as unknown. A match never upgrades the model's own judgment."""
    diff = []
    for field in COPY_FIELDS:
        expected, seen = combination[field], rendered[field]
        if not comparable(seen):
            diff.append({"field": field, "expected": expected[:600], "rendered": "", "changes": []})
        elif comparable(seen) != comparable(expected):
            diff.append({"field": field, "expected": expected[:600], "rendered": seen[:600], "changes": copy_changes(expected, seen)})
    if any(d["rendered"] for d in diff):
        corrections = separator.join(f"“{c['rendered']}” → “{c['expected']}”" for d in diff for c in d["changes"])
        finding.update(status="fail", gate_reason="rendered_text_mismatch", rendered_text_diff=diff,
            suggested_fix=(fix["rendered_text_mismatch"] + corrections)[:320])
    elif diff:
        finding["rendered_text_diff"] = diff
        if finding["status"] != "fail":
            finding.update(status="unknown", gate_reason="rendered_text_missing")


def missing_claim_evidence(finding, facts):
    """Conservative guard for observable certifications and quantified claims.

    A product name is not evidence for UL certification or a 500 A rating.
    This gate cannot prove semantic entailment; ambiguous cases stay unknown.
    """
    from .claims import unsupported_claims
    support = "\n".join(f.statement + "\n" + f.source_quote for f in facts)
    return unsupported_claims(finding.visual_evidence + "\n" + finding.rationale, support)


def effective_report(call):
    """Reapply current evidence gates without changing or resending model output.

    Stored model receipt stays intact. Human confirmation freezes the derived
    report and this separate normalization version in its own audit record.
    """
    result = call.result_json
    if not result:
        return result
    from .ai_qc_contracts import AICheckFinding
    findings = [{k: v for k, v in item.items() if k in AICheckFinding.model_fields}
        | {"status": item.get("model_status", item["status"])} for item in result["findings"]]
    report = {"summary": result["summary"], "findings": findings}
    if "rendered_text" in result:
        report["rendered_text"] = result["rendered_text"]
    normalized = normalize_report(report, call.input_json)
    if call.kind == "single" and result.get("image_evidence"):
        # A single call from before J-43 keeps the English file finding it was shown with.
        normalized["findings"] = [file_finding(result["image_evidence"][0], call.input_json.get("strategy_language") or "en-US"),
            *normalized["findings"]]
    return {**result, **normalized, "source": result["source"]}


def file_finding(evidence, language):
    """The system's own file check on the exact bytes it sent; never a visual or semantic pass."""
    words = text.FILE_FINDING[language]
    return {"check_id": "file_valid", "status": "pass", "confidence": "high",
        "visual_evidence": words["visual_evidence"].format(width=evidence["width"], height=evidence["height"], mime=evidence["mime"]),
        "rationale": words["rationale"], "suggested_fix": "", "fact_ids": [], "model_status": "pass", "gate_reason": None, "unsupported_claims": []}


def covers(call):
    """QC kinds a call answers: a call from before the split checked the image and its comparison at once."""
    return (call.kind,) if call.kind else workflow.QC_KINDS


def latest(session, ctx, row, asset_id, kind):
    """Newest attempt answering `kind` for this exact image; a pre-split call answers both until a split call follows."""
    return session.scalar(select(Call).where(Call.workspace_id == ctx.workspace_id, Call.round_id == row.id,
        Call.asset_version_id == asset_id, or_(Call.kind == kind, Call.kind.is_(None)))
        .order_by(Call.sequence.desc(), Call.created_at.desc(), Call.id.desc()).limit(1))


def capability(session, ctx, row):
    if row.execution_mode == "mock":
        return {"provider": "mock", "model": "marketing-qc-example-v1", "unit_maximum": "0", "reason": None}
    provider = os.getenv("MARKETAI_MARKETING_QC_PROVIDER", "gemini").strip()
    model = os.getenv("MARKETAI_MARKETING_QC_MODEL", "").strip()
    maximum = os.getenv("MARKETAI_MARKETING_QC_MAX_COST_USD", "").strip()
    reason = None
    try:
        amount = Decimal(maximum)
        if provider not in ("gemini", "openai") or not model or not amount.is_finite() or not Decimal("0") < amount <= Decimal("10"):
            raise ValueError()
        if not credential_binding(session, ctx, provider):
            raise SelectionError("credential_required", "请配置工作区视觉检查凭据。")
        authorize_paid(session, ctx)
    except SelectionError as exc:
        reason = exc.code
    except (InvalidOperation, ValueError):
        reason = "qc_not_configured"
    return {"provider": provider, "model": model, "unit_maximum": maximum if reason is None else "0", "reason": reason}


def frozen_input(session, ctx, row, asset_id, *, kind, language=None, policy=None, assets=None, blob_refs=None, quality_by_asset=None,
                 scope=None):
    """The exact, hashed input of one QC call. `kind` None rebuilds a pre-split call's input to tell whether it is current.

    single: the image with its frozen product photos and brand logo; it never waits for other images.
    paired: the image with the exact versions `board.comparison_scope` compares it with, once none is missing.
    `language` is the strategy language the call writes in, so the same image checked in another language is another
    (paid) call; None rebuilds a call frozen before J-43 with its Chinese prompt. `policy` rebuilds a stored call under the
    policy it was frozen with; new calls use the current one.
    """
    assets = assets if assets is not None else workflow.creative_assets(session, ctx, row)
    by_id = {v.id: (v, recipe, combo) for v, recipe, combo in assets}
    if asset_id not in by_id:
        raise MarketingError("asset_not_in_round", "素材不属于本轮冻结方案。")
    version, recipe, combo = by_id[asset_id]
    plan = row.snapshot_json["plan"]
    groups = {c: group["id"] for group in plan["groups"] for c in group["combination_ids"]}
    angle_of = {a["id"]: a for a in plan["angles"]}
    if kind is None:
        selected = legacy_comparisons(session, ctx, row, assets, combo, groups, quality_by_asset)
    elif kind == "paired":
        selected, comparison_scope = paired_comparisons(session, ctx, row, assets, version.id, quality_by_asset, scope)
    else:
        selected = []
    images = []
    blob_refs = {} if blob_refs is None else blob_refs
    for v, snap, combination in [(version, recipe, combo), *selected]:
        if v.id not in blob_refs:
            blob_refs[v.id] = asdict(asset_blob_ref(session, ctx, v.id, version=v))
        if blob_refs[v.id] is None:
            raise SelectionError('output_unavailable', '该历史素材暂无可下载文件。')
        images.append({"asset_version_id": v.id, "role": "target" if v.id == version.id else "comparison",
            "content_sha256": v.content_sha256, "recipe_hash": snap.recipe_hash,
            "provenance": snap.canonical_json.get("reuse", {"kind": "generated_for_plan"}),
            "blob": blob_refs[v.id], "combination": combination,
            "group_id": groups[combination["id"]]})
        if kind == "paired":
            # Each comparison names what it stands for, so the model can tell belonging and distinctness apart.
            meaning = angle_of[combination["angle_id"]]
            images[-1].update(proposition=meaning["proposition"],
                expression=next(e for e in meaning["expressions"] if e["id"] == combination["expression_id"]))
    angle = angle_of[combo["angle_id"]]
    expression = next(e for e in angle["expressions"] if e["id"] == combo["expression_id"])
    required = workflow.kind_checks(workflow.required_checks(combo, plan), kind)
    policy = policy or (POLICIES[kind] if language else LEGACY_POLICIES[kind])
    inputs = {"policy_version": policy, **({"kind": kind} if kind else {}),
        **({"strategy_language": language} if language else {}), "round_id": row.id, "asset_version_id": version.id,
        "plan_hash": row.snapshot_json["plan_hash"], "recipe_hash": recipe.recipe_hash,
        "understanding": row.snapshot_json["understanding"], "proposition": angle["proposition"], "expression": expression,
        "combination": combo, "question": plan["question"]}
    if kind != "single":
        inputs.update({"comparison_kind": plan["comparison_kind"], "changed": plan["changed"],
            "allowed_changes": plan["allowed_changes"], "held_constant": plan["held_constant"]})
    if kind == "paired":
        inputs["comparison_scope"] = comparison_scope
        inputs.update(required_checks=required, images=images)
        if "style_variation" in plan:
            inputs.update(style_rule(plan, groups[combo["id"]]))
    else:
        from .product_references import assert_current, for_asset
        references = recipe.canonical_json.get("selection", {}).get("product_references", [])
        references = references or for_asset(session, ctx, version)
        assert_current(session, ctx, references)
        brand, logos = brand_style_input(session, ctx, plan, groups[combo["id"]]) if "brand_style" in required else ({}, [])
        inputs.update({"required_checks": required, "images": [*images, *references, *logos],
            "product_reference_available": bool(references), **brand})
        if "language_correct" in inputs["required_checks"]:
            inputs["ad_language"] = plan["ad_language"]
    from .ai_qc_provider import system_prompt, public_input, output_schema
    inputs["system_prompt"] = system_prompt(inputs)
    inputs["output_schema"] = output_schema(inputs)
    import json
    if len((system_prompt(inputs) + json.dumps(public_input(inputs), ensure_ascii=False)).encode()) > MAX_TEXT_BYTES:
        raise MarketingError("qc_input_too_large", "检查意图与产品依据过长，请精简后重新确认方案。")
    return inputs


def legacy_comparisons(session, ctx, row, assets, combo, groups, quality_by_asset):
    """The comparison images a pre-split call chose: one per other group, skipping executions sent back for changes."""
    def correction_requested(version_id):
        review = quality_by_asset.get(version_id) if quality_by_asset is not None else workflow.latest_qc(session, ctx, row, version_id)
        return bool(review and review.findings_json.get("quality") in ("needs_changes", "rejected"))
    # Stable selection: a newer variant does not silently change an existing pair.
    peers = sorted((item for item in assets if groups[item[2]["id"]] != groups[combo["id"]]),
        key=lambda item: (groups[item[2]["id"]], correction_requested(item[0].id), item[2]["id"], item[0].created_at, item[0].id))
    selected, seen = [], set()
    for item in peers:
        if groups[item[2]["id"]] in seen:
            continue
        selected.append(item)
        seen.add(groups[item[2]["id"]])
        if len(selected) >= MAX_IMAGES - 1:
            break
    return selected


def round_scope(session, ctx, row, assets, quality_by_asset=None):
    from .board import comparison_scope
    return comparison_scope(row.snapshot_json["plan"], workflow.execution_facts(session, ctx, row, assets, quality_by_asset))


def paired_comparisons(session, ctx, row, assets, asset_id, quality_by_asset, scope):
    """One usable version per counterpart combination, earliest first, so a later variant never silently changes a pair.

    Same-angle other expressions come first (they decide expressions_distinct); beyond MAX_COMPARISONS the input
    names the combinations it leaves out instead of pretending they were compared.
    """
    from .board import distinct_counterparts
    scope = (scope if scope is not None else round_scope(session, ctx, row, assets, quality_by_asset))[asset_id]
    if scope["awaiting"]:
        raise MarketingError("paired_qc_waiting", "对照对象还没有可用的图片，请等待对应组合生成或修订。", 409)
    by_id = {v.id: (v, recipe, combo) for v, recipe, combo in assets}
    plan = row.snapshot_json["plan"]
    order = {c["id"]: n for n, c in enumerate(plan["combinations"])}
    first = {}
    for vid in scope["compare_with"]:
        first.setdefault(by_id[vid][2]["id"], vid)
    distinct = set(distinct_counterparts(plan, by_id[asset_id][2]["id"]))
    ranked = sorted(first, key=lambda cid: (cid not in distinct, order[cid]))
    return ([by_id[first[cid]] for cid in ranked[:MAX_COMPARISONS]],
        {"combination_ids": scope["combination_ids"], "omitted_combination_ids": ranked[MAX_COMPARISONS:]})


def style_rule(plan, group_id):
    """Whether visual style is held or tested, and the style this image's group must follow."""
    group = next(g for g in plan["groups"] if g["id"] == group_id)
    tested = plan["style_variation"] == "tested"
    return {"style_variation": plan["style_variation"],
            "style": group.get("style", "") if tested else plan.get("style_notes", ""),
            **({"group_styles": {g["id"]: g.get("style", "") for g in plan["groups"]}} if tested else {})}


def brand_style_input(session, ctx, plan, group_id):
    """The round's frozen brand baseline and style rule, never the product's current kit.

    A reused older creative is checked against this round's baseline too. A logo that
    lost its file/rights gates is not sent; brand_style then cannot pass on the logo."""
    from . import brand_kits
    result = style_rule(plan, group_id)
    logos = []
    if plan.get("brand_kit"):
        kit = brand_kits.resolve_ref(session, ctx, plan["brand_kit"])
        result["brand_kit"] = brand_kits.qc_summary(kit)
        if kit.content_json.get("logo"):
            try:
                logos.append(brand_kits.logo_input(session, ctx, kit.content_json["logo"]))
            except MarketingError:
                pass
        result["brand_logo_available"] = bool(logos)
    return result, logos


def input_is_current(session, ctx, row, call, *, assets=None, blob_refs=None, quality_by_asset=None, scope=None):
    """Whether the call still answers its image in the language and under the policy it was written with; both are presentation,
    not a stale input."""
    try:
        return call.input_hash == digest(frozen_input(session, ctx, row, call.asset_version_id, kind=call.kind,
            language=call.input_json.get("strategy_language"), policy=call.input_json.get("policy_version"),
            assets=assets, blob_refs=blob_refs, quality_by_asset=quality_by_asset, scope=scope))
    except (MarketingError, SelectionError):
        return False


def projection(session, ctx, row, call, *, assets=None, blob_refs=None, quality_by_asset=None, scope=None):
    if not call:
        return None
    return {"id": call.id, "kind": call.kind or "combined", "language": text.record_language(call.input_json),
        "state": call.state, "provider": call.provider, "model": call.model,
        "execution_mode": call.execution_mode, "policy_version": call.policy_version, "input_hash": call.input_hash,
        "result": effective_report(call), "error_code": call.error_code, "error_detail": call.error_detail,
        "maximum_cost": call.maximum_cost,
        "current": input_is_current(session, ctx, row, call, assets=assets, blob_refs=blob_refs, quality_by_asset=quality_by_asset, scope=scope),
        "product_reference_version_ids": [i["reference_version_id"] for i in call.input_json["images"] if i["role"] == "product_reference"],
        "compared_asset_version_ids": [i["asset_version_id"] for i in call.input_json["images"] if i["role"] == "comparison"]}


def latest_reviews(session, ctx, row, assets):
    ids = [v.id for v, _, _ in assets]
    reviews = {}
    for qc in session.scalars(select(workflow.QC).where(workflow.QC.workspace_id == ctx.workspace_id, workflow.QC.round_id == row.id,
            workflow.QC.asset_version_id.in_(ids)).order_by(workflow.QC.created_at.desc(), workflow.QC.id.desc())).all() if ids else ():
        reviews.setdefault(qc.asset_version_id, qc)
    return reviews


def quote_and_inputs(session, ctx, row, kind, requested_language=None):
    """One quote per QC kind. Single checks every generated image that still needs it; paired skips images
    whose comparison objects are missing and lists them as waiting.

    New calls write in the round plan's strategy language, else the requester's interface language. A current
    answer written in another language or under an earlier policy is shown as recorded, never billed again to translate or
    re-check it."""
    ensure_active(session, ctx)
    language = output_language(row.snapshot_json["plan"], requested_language)
    cap = capability(session, ctx, row)
    assets = workflow.creative_assets(session, ctx, row)
    reviews = latest_reviews(session, ctx, row, assets)
    scope = round_scope(session, ctx, row, assets, reviews) if kind == "paired" else None
    candidates, pending, waiting = [], [], []
    reason = cap["reason"]
    if row.state != "reviewing" or workflow.scoped(session, ctx, workflow.Handoff, round_id=row.id):
        reason = "round_not_reviewable"
    for version, _, _ in assets:
        previous = latest(session, ctx, row, version.id, kind)
        review = reviews.get(version.id)
        if review and review.findings_json.get("quality") in ("needs_changes", "rejected"):
            continue  # Check the new correction; do not bill again for a rejected execution.
        if workflow.ready(session, ctx, row, version.id):
            continue
        if previous and previous.state in ("queued", "running", "uncertain"):
            pending.append(previous.id)  # an unknown outcome is reconciled, never resent
            continue
        if kind == "paired" and scope[version.id]["awaiting"]:
            waiting.append({"asset_version_id": version.id, "awaiting_combination_ids": scope[version.id]["awaiting"]})
            continue
        inputs = frozen_input(session, ctx, row, version.id, kind=kind, language=language, assets=assets, quality_by_asset=reviews, scope=scope)
        hashed = digest(inputs)
        if previous and previous.state == "succeeded" and (previous.input_hash == hashed or (
                (previous.input_json.get("strategy_language"), previous.input_json.get("policy_version"))
                != (language, inputs["policy_version"])
                and input_is_current(session, ctx, row, previous, assets=assets, quality_by_asset=reviews, scope=scope))):
            continue
        candidates.append({"asset_version_id": version.id, "input": inputs, "input_hash": hashed,
            "retry_of_id": previous.id if previous else None, "sequence": previous.sequence + 1 if previous else 1})
    candidates = sorted(candidates, key=lambda item: item["asset_version_id"])[:60]
    if not candidates:
        reason = reason or ("qc_in_progress" if pending else "paired_qc_waiting" if waiting else "nothing_to_check")
    total = Decimal(cap["unit_maximum"]) * len(candidates)
    if row.execution_mode == "byok" and not reason:
        try:
            check_budget(session, ctx, total)
        except SelectionError as exc:
            reason = exc.code
    fingerprint = digest({"round_id": row.id, "mode": row.execution_mode, "provider": cap["provider"], "model": cap["model"],
        "maximum": cap["unit_maximum"], "policy": POLICIES[kind], "kind": kind,
        "inputs": [{k: item[k] for k in ("asset_version_id", "input_hash", "retry_of_id")} for item in candidates]})
    return {"available": reason is None, "unavailable_reason": reason, "provider": cap["provider"], "model": cap["model"],
        "execution_mode": row.execution_mode, "quantity": len(candidates), "maximum_cost": str(total), "currency": "USD",
        "quote_hash": fingerprint, "asset_version_ids": [c["asset_version_id"] for c in candidates],
        "kind": kind, "waiting": waiting}, candidates, cap


def start(session, ctx, row, body):
    """Queue one durable, idempotent job per image; paid calls reserve their own maximum before they can dispatch."""
    kind, policy = body.kind, POLICIES[body.kind]
    quoted, inputs, cap = quote_and_inputs(session, ctx, row, kind, body.strategy_language)
    if not quoted["available"] or quoted["quote_hash"] != body.quote_hash:
        raise MarketingError("qc_quote_changed", "检查对象、报价或权限已变化，请刷新预览后重试。", 409)
    records = []
    for item in inputs:
        call_id = new_id("maqc")
        op = create_operation(session, command_type="marketing.ai_qc")
        job = enqueue_in_session(session, context=ctx, job_type="marketing.ai_qc", payload={"call_id": call_id},
            idempotency_key=call_id, resource_type="marketing_ai_qc", resource_id=call_id,
            safe_to_retry_after_crash=True, max_attempts=1)
        record = Call(id=call_id, workspace_id=ctx.workspace_id, round_id=row.id, asset_version_id=item["asset_version_id"], kind=kind,
            operation_id=op.id, job_id=job.id, retry_of_id=item["retry_of_id"], sequence=item["sequence"], state="queued",
            execution_mode=row.execution_mode, provider=cap["provider"], model=cap["model"], maximum_cost=cap["unit_maximum"],
            policy_version=policy, input_hash=item["input_hash"],
            request_hash=digest([item["input_hash"], item["retry_of_id"], cap["provider"], cap["model"], policy]), input_json=item["input"])
        session.add(record)
        session.flush()
        if row.execution_mode == "byok":
            entitlement = authorize_paid(session, ctx)
            bundle = CostQuoteEngine({record.provider: {record.model: Decimal(record.maximum_cost)}}, policy).bundle(
                [BillableCallSpec(record.id, record.provider, record.model, 1)], execution_mode="byok", budget_currency="USD")
            ReservationBook().reserve(session, ctx, ReservationPlan(bundle, (), entitlement, op.id, {record.id: job.id}))
        records.append(record)
    workflow.audit(session, row, "ai_qc_requested", {"kind": kind, "call_ids": [r.id for r in records],
        "quote_hash": quoted["quote_hash"], "maximum_cost": quoted["maximum_cost"]})


def normalize_report(data, inputs):
    report = report_model(inputs).model_validate(data)
    ids = [f.check_id for f in report.findings]
    if len(ids) != len(set(ids)) or set(ids) != set(inputs["required_checks"]):
        raise QCOutputError("qc_check_coverage_invalid", "findings")
    confirmed_facts = {f["id"] for f in inputs["understanding"]["facts"] if f["confirmation"] == "confirmed"}
    from .contracts import Fact
    facts = {f["id"]: Fact.model_validate(f) for f in inputs["understanding"]["facts"] if f["id"] in confirmed_facts}
    fix = text.GATE_FIX[text.record_language(inputs)]
    transcribed = isinstance(report, AICheckTranscribedReport)
    findings = []
    for index, check in enumerate(report.findings):
        if set(check.fact_ids) - confirmed_facts:
            raise QCOutputError("qc_fact_reference_invalid", f"findings.{index}.fact_ids")
        finding = check.model_dump(mode="json")
        finding["model_status"] = check.status
        if check.confidence == "low":
            finding.update(status="unknown", gate_reason="low_confidence")
        if check.check_id == "product_fidelity" and not inputs["product_reference_available"] and finding["status"] != "fail":
            finding.update(status="unknown", gate_reason="product_reference_missing", suggested_fix=fix["product_reference_missing"])
        if check.check_id == "facts_supported" and check.status == "pass" and not check.fact_ids:
            finding.update(status="unknown", gate_reason="supporting_facts_missing")
        if check.check_id == "facts_supported" and finding["status"] == "pass":
            missing = missing_claim_evidence(check, [facts[fid] for fid in check.fact_ids])
            if missing:
                finding.update(status="unknown", gate_reason="claim_evidence_missing", unsupported_claims=missing,
                    suggested_fix=(fix["claim_evidence_missing"] + text.CLAIM_SEPARATOR[text.record_language(inputs)].join(missing))[:320])
        if check.check_id == "comparison_faithful" and inputs["comparison_kind"] == "specific_factor" and not any(i["role"] == "comparison" for i in inputs["images"]) and finding["status"] != "fail":
            finding.update(status="unknown", gate_reason="comparison_missing")
        if check.check_id == "expressions_distinct" and finding["status"] == "pass" and not any(
                i["role"] == "comparison" and i["combination"]["angle_id"] == inputs["combination"]["angle_id"]
                and i["combination"]["expression_id"] != inputs["combination"]["expression_id"] for i in inputs["images"]):
            finding.update(status="unknown", gate_reason="comparison_missing")
        if (check.check_id == "brand_style" and finding["status"] == "pass"
                and (inputs.get("brand_kit") or {}).get("logo", {}).get("required") and not inputs.get("brand_logo_available")):
            finding.update(status="unknown", gate_reason="brand_logo_missing", suggested_fix=fix["brand_logo_missing"])
        if check.check_id == "copy_consistent" and transcribed:
            rendered_copy_gate(finding, report.rendered_text.model_dump(), inputs["combination"], fix,
                text.CLAIM_SEPARATOR[text.record_language(inputs)])
        findings.append(finding)
    # The summary is the model's own words: it reads as the verdict only when it is ready_to_confirm (#294).
    return {"summary": report.summary, "findings": findings, "source": "ai_visual_suggestion", "normalization_version": NORMALIZATION_POLICY,
        "quality_suggestion": "needs_changes" if any(f["status"] == "fail" for f in findings) else
            "needs_confirmation" if any(f["status"] == "unknown" for f in findings) else "ready_to_confirm",
        "human_approved": False, **({"rendered_text": report.rendered_text.model_dump()} if transcribed else {})}


def resolve_call(session, ctx, row, call_id, asset_id=None, kind=None):
    """A succeeded, current call that is still the newest answer for `kind` (default: the call's own kind)."""
    records = workflow.scoped(session, ctx, Call, id=call_id, round_id=row.id)
    if not records or (asset_id and records[0].asset_version_id != asset_id):
        raise MarketingError("ai_qc_not_found", "找不到这条素材的 AI 检查。", 404)
    call = records[0]
    kind = kind or call.kind or "single"
    if kind not in covers(call):
        raise MarketingError("ai_qc_kind_mismatch", "这条检查不是所需的单图检查或对照检查。")
    newest = latest(session, ctx, row, call.asset_version_id, kind)
    if call.state != "succeeded" or not call.result_json or newest.id != call.id or not input_is_current(session, ctx, row, call):
        raise MarketingError("ai_qc_not_current", "AI 检查未完成或已过期，请先查看当前检查结果。", 409)
    return call


def confirm(session, ctx, row, body):
    """Approve images in bulk from their current AI results. Each image needs both a single and a paired result:
    a single-image pass never stands in for the comparison."""
    from .workflow_contracts import ReviewCreative
    if len(set(body.ai_qc_ids)) != len(body.ai_qc_ids):
        raise MarketingError("duplicate_ai_qc", "同一检查结果不能重复确认。")
    if set(body.resolved_checks) - workflow.CHECKS.keys() or (body.resolved_checks and not body.note.strip()):
        raise MarketingError("qc_confirmation_note_required", "请说明对共同待确认项的判断依据。")
    by_asset = {}
    for rid in body.ai_qc_ids:
        call = resolve_call(session, ctx, row, rid)
        answered = by_asset.setdefault(call.asset_version_id, {})
        for kind in covers(call):
            if call.kind is None and latest(session, ctx, row, call.asset_version_id, kind).id != call.id:
                continue  # a later split call replaced this pre-split answer for that kind
            if kind in answered:
                raise MarketingError("duplicate_ai_qc", "同一检查结果不能重复确认。")
            answered[kind] = call
    for asset_id, answered in by_asset.items():
        if set(answered) != set(workflow.QC_KINDS):
            raise MarketingError("ai_qc_kind_missing", "请同时确认这张图的单图检查与对照检查结果。")
        review = workflow.latest_qc(session, ctx, row, asset_id)
        if review and review.findings_json.get("quality") in ("needs_changes", "rejected"):
            raise MarketingError("human_changes_require_review", "该版本已有人工修正或拒绝意见，不能用旧 AI 通过结论整批覆盖。请审核修订版本。")
        single, paired = answered["single"], answered["paired"]
        required = {**single.input_json["required_checks"], **paired.input_json["required_checks"]}
        findings = {f["check_id"]: f["status"] for call in {single.id: single, paired.id: paired}.values()
            for f in effective_report(call)["findings"] if f["check_id"] in required}
        for key in body.resolved_checks:
            if findings.get(key) not in ("unknown", "pass"):
                raise MarketingError("qc_failure_requires_review", "AI 标出的失败项需要单独确认修正，不能整批忽略。")
            if findings.get(key) == "unknown":
                findings[key] = "pass"
        if any(value != "pass" for value in findings.values()):
            raise MarketingError("semantic_qc_unresolved", "仍有需要处理的疑点，请确认或调整后再审核。")
        workflow.review(session, ctx, row, ReviewCreative(asset_version_id=asset_id, quality="approved",
            checks=findings, note=body.note.strip() or text.CONFIRM_NOTE[text.record_language(single.input_json)] + single.result_json["summary"][:1800],
            compared_asset_version_ids=[i["asset_version_id"] for i in paired.input_json["images"] if i["role"] == "comparison"],
            ai_qc_id=single.id, paired_ai_qc_id=paired.id if paired.id != single.id else None, override_note=body.note))
