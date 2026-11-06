"""Prepare a correction, never dispatch generation or inherit an approval."""
import json
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.adapters.persistence.models import new_id
from app.generation.draft import create_draft
from .contracts import CreativeCombination, Document
from .languages import guidance_matches
from .service import MarketingError, digest, get_project, creative_intent, image_brief
from .workflow_models import MarketingFeedbackDraftRecord as Feedback
from . import workflow
from app.generation.image_tools import ImageToolSpec, validate_tool, instruction

# A revision of an English-briefed combination (J-02) is that brief again plus a separate correction section (#285).
PROMPT_POLICY = "marketing-feedback-brief-v1"
REVISION = ("Revise the original creative (reference image 1): fix only what the corrections below describe. "
            "Keep the buying proposition, expression and every hold-constant condition; do not widen any claim.\n")
CORRECTIONS = ("Corrections (reviewer guidance for this revision, not ad copy: never render these words, "
               "their labels or their language in the image):")
UNMET = "Not met yet; satisfy every item listed under Checks."


class PrepareFeedback(Document):
    asset_version_id: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=1200)
    provider: Literal["gemini", "openai", "seedream", "mock"]
    model: str = Field(min_length=1, max_length=200)
    image_tool: ImageToolSpec | None = None


def prepare(session, ctx, row, body):
    if body.image_tool:
        try:
            validate_tool(body.image_tool, body.provider, body.model)
        except ValueError as exc:
            raise MarketingError("image_tool_unsupported", str(exc)) from exc
    if row.state != "reviewing" or workflow.scoped(session, ctx, workflow.Handoff, round_id=row.id):
        raise MarketingError("feedback_round_frozen", "投放清单已冻结；修改素材请另立小轮。", 409)
    project = get_project(session, ctx, row.project_id)
    if project.revision != row.plan_revision or project.state != "plan_confirmed":
        raise MarketingError("marketing_plan_changed", "方案已变化，请使用当前方案准备素材。", 409)
    assets = {v.id: (v, recipe, combo) for v, recipe, combo in workflow.creative_assets(session, ctx, row)}
    if body.asset_version_id not in assets:
        raise MarketingError("asset_not_in_round", "请使用当前小轮的素材。")
    version, recipe, combo = assets[body.asset_version_id]
    qc = workflow.latest_qc(session, ctx, row, version.id)
    from . import ai_qc
    # Current single and paired results both feed the correction; a pre-split call counts once.
    current = {}
    for kind in workflow.QC_KINDS:
        call = ai_qc.latest(session, ctx, row, version.id, kind)
        if call and call.state == "succeeded" and call.id not in current and ai_qc.input_is_current(session, ctx, row, call):
            current[call.id] = (kind, call, ai_qc.effective_report(call))
    single = next((call for kind, call, _ in current.values() if kind == "single"), None)
    paired = next((call for kind, call, _ in current.values() if kind == "paired"), None)
    report = next(iter(current.values()), (None, None, None))[2]
    if not qc and not report and not body.note.strip() and not body.image_tool:
        raise MarketingError("feedback_qc_required", "请先运行 AI 检查或保存质量审核意见。")
    findings = [f for _, _, r in current.values() for f in r["findings"] if f["status"] != "pass"]
    required = workflow.required_checks(combo, row.snapshot_json["plan"])
    human_findings = [{"check_id": key, "status": status, "correction": required[key]}
        for key, status in (qc.findings_json.get("checks", {}) if qc else {}).items() if status != "pass"]
    review_note = qc.findings_json.get("note", "").strip() if qc else ""
    note = "\n".join(part for part in (review_note, body.note.strip(), instruction(body.image_tool) if body.image_tool else "") if part)
    if not findings and not note:
        raise MarketingError("feedback_required", "请补充希望调整的内容。")
    intent = creative_intent(project, combo["id"])
    # Combinations planned before the English image brief keep the pre-#285 correction prompt.
    briefed = bool(combo.get("image_brief"))
    if briefed and not all(guidance_matches(text, intent["ad_language"]) for text in (review_note, body.note)):
        raise MarketingError("feedback_note_language_mismatch", "修改说明会原样发给生图模型，请用英文或广告语言填写。",
                             params={"ad_language": intent["ad_language"]})
    from app.providers.router import get_provider
    if body.model not in get_provider(body.provider).models or (body.provider == "mock") != (row.execution_mode == "mock"):
        raise MarketingError("feedback_provider_invalid", "供应商、模型与本轮模式不匹配。")
    frozen = {"round_id": row.id, "source_asset_version_id": version.id, "source_sha256": version.content_sha256,
        "combination_id": combo["id"], "qc_review_id": qc.id if qc else None,
        "ai_qc_id": (single or paired).id if report else None, "normalization_version": (report or {}).get("normalization_version"),
        **({"paired_ai_qc_id": paired.id} if single and paired else {}),
        "findings": findings, "human_findings": human_findings, "human_note": note,
        "request_note": body.note.strip(), "provider": body.provider, "model": body.model,
        **({"image_tool": body.image_tool.model_dump(mode="json")} if body.image_tool else {}),
        **({"prompt_policy": PROMPT_POLICY} if briefed else {})}
    hashed = digest(frozen)
    previous = workflow.scoped(session, ctx, Feedback, round_id=row.id, feedback_hash=hashed)
    if previous:
        return previous[0]
    # Feedback is source material, not permission to change claims or controls.
    # Keep every unresolved check once. Explicit human failures take priority;
    # additional wording must not replace the already-saved review note.
    by_check = {f["check_id"]: {"check_id": f["check_id"], "status": f["status"],
        "correction": f.get("suggested_fix") or f.get("rationale", "")}
        for f in findings if not qc or qc.findings_json.get("checks", {}).get(f["check_id"]) != "pass"}
    for finding in human_findings:
        existing = by_check.get(finding["check_id"], {})
        by_check[finding["check_id"]] = {**finding, "correction": existing.get("correction") or finding["correction"]}
    corrections = sorted(by_check.values(), key=lambda f: (f["status"] != "fail", f["check_id"]))
    # The correction reuses the exact photos the source was made from, never the product's newer set.
    from .product_references import for_asset, REFERENCE_BRIEF
    reference_selection = {k: v for k, v in recipe.canonical_json.get("selection", {}).items()
        if k in ("product_references", "reference_actor_id")}
    if not reference_selection.get("product_references"):
        reference_selection["product_references"] = for_asset(session, ctx, version)
        reference_selection["reference_actor_id"] = (version.reference_sources_json or {}).get("reference_actor_id")
    if briefed:
        from . import brand_kits
        kit = brand_kits.resolve_ref(session, ctx, intent["brand_kit"]) if intent.get("brand_kit") else None
        brand = brand_kits.brief_lines(kit.content_json, logo_supplied=False, logo_in_source=True) if kit else ()
        lines = [f"- {f['check_id']} ({f['status']}): {guidance(f, required, intent['ad_language'])[:140]}" for f in corrections]
        lines += ["- Note: " + " ".join(note.split())] if note else []
        prompt = (REVISION + image_brief(CreativeCombination.model_validate(combo), intent, brand=brand)
            + ("\n" + REFERENCE_BRIEF if reference_selection["product_references"] else "")
            + "\n" + CORRECTIONS + "".join("\n" + line for line in lines))
    else:
        original = (recipe.canonical_json.get("selection") or {}).get("prompt") or combo["visual_brief"]
        fixes = [{"check": f["check_id"], "status": f["status"], "correction": f["correction"][:140]} for f in corrections]
        prompt = (original[:900] + "\n修正本素材，保留原购买主张、表达与保持条件；不要扩大声明范围。\n"
            + "质量反馈（仅修正相关执行）：" + json.dumps({"findings": fixes, "human_note": note}, ensure_ascii=False)
            + "\n主张：" + intent["proposition"] + "\n保持条件：" + "；".join(intent["held_constant"]))
        if reference_selection["product_references"]:
            prompt += "\n" + REFERENCE_BRIEF
    if len(prompt) > 4000:
        raise MarketingError("feedback_too_long", "修改意见过长，请精简后重新准备。")
    draft = create_draft(session, ctx, product_id=project.product_id, provider=body.provider, model=body.model,
        execution_mode=row.execution_mode, combination_strategy={"mode": "single"},
        format_config={"aspect_ratio": generation_ratio(version.aspect_ratio), "image_size": "2K" if body.image_tool else "1K", "variants": 1},
        selectors={"prompt": prompt, **reference_selection}, regeneration_intent="quality_feedback", source_asset_version_id=version.id,
        source_recipe_identity=recipe.recipe_hash)
    record = Feedback(id=new_id("mfd"), workspace_id=ctx.workspace_id, round_id=row.id,
        source_asset_version_id=version.id, draft_id=draft.id, feedback_hash=hashed, feedback_json=frozen)
    session.add(record)
    session.flush()
    workflow.audit(session, row, "feedback_draft_prepared", {"feedback_id": record.id, "draft_id": draft.id, "source_asset_version_id": version.id})
    return record


def guidance(finding, required, language):
    """A fix in a script this ad must not show stays in the QC record; the check's own description stands in."""
    for text in (finding["correction"], required.get(finding["check_id"], "")):
        if text and guidance_matches(text, language):
            return " ".join(text.split())
    return UNMET


def resolve(session, ctx, draft):
    record = session.scalar(select(Feedback).where(Feedback.workspace_id == ctx.workspace_id, Feedback.draft_id == draft.id))
    if not record:
        return None
    if record.feedback_json.get("image_tool") and (draft.source_asset_version_id != record.source_asset_version_id
            or draft.provider != record.feedback_json["provider"] or draft.model != record.feedback_json["model"]):
        raise MarketingError("image_tool_draft_changed", "编辑来源或供应商已变化，请重新准备编辑草稿。", 409)
    if record.feedback_json.get("image_tool"):
        from app.adapters.persistence.models import AssetVersionRecord
        source = session.get(AssetVersionRecord, record.source_asset_version_id)
        if not source or draft.format_json != {"aspect_ratio": generation_ratio(source.aspect_ratio), "image_size": "2K", "variants": 1}:
            raise MarketingError("image_tool_format_changed", "图片工具固定为一次一张 2K 处理，请恢复设置或重新准备草稿。", 409)
    row = workflow.get_round(session, ctx, record.round_id)
    project = get_project(session, ctx, row.project_id)
    if row.state != "reviewing" or workflow.scoped(session, ctx, workflow.Handoff, round_id=row.id):
        raise MarketingError("feedback_round_frozen", "本轮素材已冻结，请准备新一轮。", 409)
    if project.revision != row.plan_revision or project.state != "plan_confirmed" or draft.product_id != project.product_id or draft.execution_mode != row.execution_mode:
        raise MarketingError("marketing_plan_changed", "方案或模式已变化，请重新准备素材。", 409)
    return {"intent": creative_intent(project, record.feedback_json["combination_id"]),
        "feedback": {"id": record.id, "hash": record.feedback_hash, **record.feedback_json}}


def generation_ratio(ratio):
    """Keep the original pixels; choose the nearest supported output canvas."""
    from math import log
    from app.generation.selection import ASPECT_RATIOS
    if ratio in ASPECT_RATIOS:
        return ratio
    try:
        w, h = map(float, (ratio or "4:5").split(":"))
        actual = w / h
        return min(ASPECT_RATIOS, key=lambda value: abs(log(actual / (float(value.split(":")[0]) / float(value.split(":")[1])))))
    except (ValueError, ZeroDivisionError):
        return "4:5"
