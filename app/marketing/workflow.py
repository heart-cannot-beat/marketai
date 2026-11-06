"""Question-led delivery workflow. External delivery stays in Meta Ads Manager."""
from __future__ import annotations

import base64
import hashlib
from datetime import timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update, text

from app.adapters.persistence.models import AssetVersionRecord, GenerationRunRecord, QualityReviewRecord, new_id, utcnow
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.generation.review import create_review, ReviewError, ReviewConflictError, _has_blocking_qc_failure
from app.platform.audit import append_event
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation
from .service import MarketingError, digest, get_project, ensure_active, latest_call
from .workflow_models import (MarketingRoundRecord as Round, MarketingQCRecord as QC,
    MarketingHandoffRecord as Handoff, MarketingAdBindingRecord as Binding,
    MarketingMetricBatchRecord as Batch, MarketingEvaluationRecord as Evaluation)

CHECKS = {
    "product_fidelity": "Product appearance and use are accurate",
    "facts_supported": "Claims are supported by the confirmed product facts",
    "proposition_clear": "The intended buying proposition is clear",
    "expression_realized": "The intended expression is present",
    "comparison_faithful": "Visible changes and held conditions match the intended comparison",
    "copy_consistent": "Image and accompanying copy agree",
    "language_correct": "All visible text is in the planned ad language and legible",
    "brand_style": "Logo, palette, typography and visual style follow the frozen brand baseline and style rule; nothing forbidden appears",
    "text_readable": "All text is clear and readable: not cropped, overlapping or garbled, and inside the safe area",
    "belongs_to_intended_combination": "Beside its comparison images, the image shows its planned angle and expression, not another combination's",
    "expressions_distinct": "The image is visibly different from the same angle's other expressions, as the plan says they differ",
}
STYLE_CHECKS = ("brand_style", "text_readable")
# Checked against the exact comparison versions (paired QC); every other check needs only the image itself (single QC).
PAIRED_CHECKS = ("belongs_to_intended_combination", "expressions_distinct", "comparison_faithful")
PAIRED_ONLY = ("belongs_to_intended_combination", "expressions_distinct")
QC_KINDS = ("single", "paired")


def scoped(session, ctx, cls, **filters):
    return session.scalars(select(cls).where(cls.workspace_id == ctx.workspace_id,
        *(getattr(cls, key) == value for key, value in filters.items()))).all()


def get_round(session, ctx, round_id):
    rows = scoped(session, ctx, Round, id=round_id)
    if not rows:
        raise MarketingError("round_not_found", "找不到这轮测试。", 404)
    return rows[0]


def serialize_workspace(session, ctx):
    # Serialize ad/date claims and project budget admission across API processes.
    if session.bind.dialect.name == "postgresql":
        key = int(hashlib.sha256(ctx.workspace_id.encode()).hexdigest()[:15], 16)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def mutate(session, ctx, row, revision, *, allow_closed_evidence=False):
    ensure_active(session, ctx)
    serialize_workspace(session, ctx)
    if row.state == "closed" and not allow_closed_evidence:
        raise MarketingError("round_closed", "本轮已结束；请查看结论或准备下一轮。", 409)
    conditions = [Round.workspace_id == ctx.workspace_id, Round.id == row.id, Round.revision == revision]
    if not allow_closed_evidence:
        conditions.append(Round.state != "closed")
    changed = session.execute(update(Round).where(*conditions).values(revision=revision + 1))
    if changed.rowcount != 1:
        raise MarketingError("stale_round_revision", "测试内容已更新，请刷新后重试。", 409)
    session.refresh(row)


def active_batches(session, ctx, row):
    batches = scoped(session, ctx, Batch, round_id=row.id, state="confirmed")
    replaced = {batch.replaces_id for batch in batches if batch.replaces_id}
    return sorted((b for b in batches if b.id not in replaced), key=lambda b: b.id)


def project_spend(session, ctx, rounds):
    return sum((Decimal(item["spend"]) for r in rounds for b in active_batches(session, ctx, r)
                for item in b.rows_json), Decimal("0"))


def freeze(session, ctx, project, revision, body):
    ensure_active(session, ctx)
    serialize_workspace(session, ctx)
    session.refresh(project)
    if project.revision != revision or project.state != "plan_confirmed":
        raise MarketingError("plan_not_confirmed", "请确认当前版本方案后再开始本轮。", 409)
    rounds = sorted(scoped(session, ctx, Round, project_id=project.id), key=lambda r: r.ordinal)
    same = next((r for r in rounds if r.plan_revision == revision), None)
    if same:
        if (same.start_date != body.start_date or same.end_date != body.end_date or same.timezone != body.account_timezone
                or same.execution_mode != body.execution_mode or same.snapshot_json.get('attribution_window', '7d_click_1d_view') != body.attribution_window
                or same.snapshot_json["protocol"] != body.protocol.model_dump(mode="json")):
            raise MarketingError("round_already_frozen", "这版方案已冻结为小轮，日期、模式和判断协议不能覆盖修改。", 409)
        return same
    if any((r.close_json or {}).get("reason") == "completed" for r in rounds):
        raise MarketingError("big_round_completed", "大轮已完成；新业务目标请新建测试。", 409)
    if any(r.state != "closed" for r in rounds):
        raise MarketingError("round_still_open", "请先结束当前小轮，再确认下一轮方案。", 409)
    call = latest_call(session, ctx, project.id)
    if call and call.state in ("queued", "running", "uncertain"):
        raise MarketingError("planning_in_progress", "请先等待策划完成或核对不确定调用。", 409)
    from zoneinfo import ZoneInfo
    if body.execution_mode == "byok" and body.start_date < utcnow().astimezone(ZoneInfo(body.account_timezone)).date():
        raise MarketingError("prospective_protocol_required", "真实投放的判断规则需要在投放开始前确认；请使用今天或之后的开始日期。")
    from .plan_review import plan_document
    plan = plan_document(project.plan_json)
    cap = rounds[0].snapshot_json["plan"] if rounds else plan
    if cap['currency'] != plan['currency']:
        raise MarketingError('big_round_currency_changed', '同一大轮的媒体预算币种不能改变；新币种请新建测试。', 409)
    if len(rounds) >= cap["rules"]["max_small_rounds"] or project_spend(session, ctx, rounds) >= Decimal(cap["media_budget"]):
        raise MarketingError("big_round_limit", "已达到大轮预算或小轮次数上限；请结束并总结。", 409)
    if (body.end_date - body.start_date).days + 1 != plan["duration_days"]:
        raise MarketingError("duration_mismatch", "投放日期必须与已确认方案的周期一致。")
    remaining = Decimal(cap["media_budget"]) - project_spend(session, ctx, rounds)
    if Decimal(plan["small_round_budget"]) > remaining:
        raise MarketingError("big_round_budget", "本轮预算超过大轮剩余额度。")
    row = Round(id=new_id("mround"), workspace_id=ctx.workspace_id, project_id=project.id,
        plan_revision=revision, ordinal=len(rounds) + 1, revision=1, state="reviewing", execution_mode=body.execution_mode,
        start_date=body.start_date, end_date=body.end_date, timezone=body.account_timezone,
        snapshot_json={"version": 1, "plan": plan, "plan_hash": digest(plan), "understanding": project.understanding_json,
                       "protocol": body.protocol.model_dump(mode="json"), "currency": plan['currency'], "attribution_window": body.attribution_window})
    session.add(row)
    session.flush()
    audit(session, row, "frozen", {"plan_hash": row.snapshot_json["plan_hash"]})
    return row


def audit(session, row, event, details):
    append_event(session, event_type="marketing." + event, resource_type="marketing_round", resource_id=row.id, details=details)


def creative_assets(session, ctx, row):
    records = session.execute(select(AssetVersionRecord, GenerationRecipeSnapshotRecord)
        .join(RunItemRecord, (RunItemRecord.id == AssetVersionRecord.producing_run_item_id) & (RunItemRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRunRecord, (GenerationRunRecord.id == RunItemRecord.generation_run_id) & (GenerationRunRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRecipeSnapshotRecord, (GenerationRecipeSnapshotRecord.id == GenerationRunRecord.recipe_snapshot_id) & (GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id)
        .where(GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string() == row.project_id)
        .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)).all()
    assets = []
    combos = {c["id"]: c for c in row.snapshot_json["plan"]["combinations"]}
    for version, recipe in records:
        intent = recipe.canonical_json.get("selection", {}).get("marketing_intent", {})
        if intent.get("project_id") != row.project_id or intent.get("plan_revision") != row.plan_revision:
            continue
        combo = combos.get(intent.get("combination", {}).get("id"))
        if not combo or intent.get("plan_hash") != row.snapshot_json["plan_hash"] or intent.get("combination") != combo:
            continue
        if version.execution_mode != row.execution_mode:
            continue
        assets.append((version, recipe, combo))
    from .reuse import creatives
    reused = creatives(session, ctx, row, combos)
    from .image_editing import creatives as edited_creatives
    reused += edited_creatives(session, ctx, row, combos)
    seen = {v.id for v, _, _ in assets}
    return assets + [item for item in reused if item[0].id not in seen]


def operation_of(recipe):
    feedback = recipe.canonical_json.get("selection", {}).get("marketing_feedback") or {}
    if feedback:
        return (feedback.get("image_tool") or {}).get("kind") or "modify"
    return "crop_rotate" if recipe.canonical_json.get("image_edit") else "reuse" if recipe.canonical_json.get("reuse") else "generated"


def execution_facts(session, ctx, row, assets, qcs=None):
    """The persisted facts `board.comparison_scope` reads, one dict per round asset version, with batched reads."""
    from app.generation.models import AssetQCAttemptRecord
    from .product_references import lineage_current
    ids = [v.id for v, _, _ in assets]
    if qcs is None:
        qcs = {}
        for qc in session.scalars(select(QC).where(QC.workspace_id == ctx.workspace_id, QC.round_id == row.id,
                QC.asset_version_id.in_(ids)).order_by(QC.created_at.desc(), QC.id.desc())).all() if ids else ():
            qcs.setdefault(qc.asset_version_id, qc)
    blocking = set(session.scalars(select(AssetQCAttemptRecord.asset_version_id).where(
        AssetQCAttemptRecord.workspace_id == ctx.workspace_id, AssetQCAttemptRecord.asset_version_id.in_(ids),
        AssetQCAttemptRecord.state == "failed_blocking")).all()) if ids else set()
    current = {}
    facts = []
    for version, recipe, combo in assets:
        key = digest(version.reference_sources_json)
        if key not in current:
            current[key] = lineage_current(session, ctx, version.reference_sources_json)
        qc = qcs.get(version.id)
        facts.append({"asset_version_id": version.id, "combination_id": combo["id"], "parent_version_id": version.parent_version_id,
            "content_sha256": version.content_sha256, "created_at": version.created_at.isoformat(),
            "reused": operation_of(recipe) == "reuse", "quality": qc.findings_json["quality"] if qc else None,
            "references_current": current[key], "blocking": version.id in blocking})
    return facts


def latest_qc(session, ctx, row, version_id):
    return session.scalar(select(QC).where(QC.workspace_id == ctx.workspace_id, QC.round_id == row.id,
        QC.asset_version_id == version_id).order_by(QC.created_at.desc(), QC.id.desc()).limit(1))


def required_checks(combo, plan):
    """Rounds frozen before a check existed keep the check list they were reviewed against.

    Whether an image shows its own expression, distinct from the same angle's others, is judged against how the
    plan says expressions differ; expressions frozen without those details keep expression_realized and
    comparison_faithful only. expressions_distinct applies when the comparison scope holds another expression of
    the same angle; the plan declares that, never a missing image.
    """
    from .board import distinct_counterparts
    skip = set() if combo.get("image_brief") else {"language_correct"}
    if "style_variation" not in plan:
        skip.update(STYLE_CHECKS)
    expression = next((e for a in plan.get("angles", ()) if a["id"] == combo.get("angle_id")
                       for e in a["expressions"] if e["id"] == combo.get("expression_id")), {})
    if not expression.get("visual_mechanism"):
        skip.update(PAIRED_ONLY)
    elif not distinct_counterparts(plan, combo["id"]):
        skip.add("expressions_distinct")
    checks = {k: v for k, v in CHECKS.items() if k not in skip}
    return {**checks, **{f"intent_{index + 1}": item for index, item in enumerate(combo["checks"])}}


def kind_checks(required, kind):
    """The part of a required check list one QC kind answers; a pre-split call (kind None) answered all of it."""
    if kind is None:
        return dict(required)
    return {k: v for k, v in required.items() if (k in PAIRED_CHECKS) == (kind == "paired")}


def active_quality(session, ctx, version_id):
    reviews = scoped(session, ctx, QualityReviewRecord, asset_version_id=version_id)
    replaced = {review.supersedes_review_id for review in reviews if review.supersedes_review_id}
    return [review for review in reviews if review.id not in replaced and review.review_value != "cleared"]


def ready(session, ctx, row, version_id):
    qc = latest_qc(session, ctx, row, version_id)
    quality = active_quality(session, ctx, version_id)
    from .product_references import lineage_current
    version = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.workspace_id == ctx.workspace_id, AssetVersionRecord.id == version_id))
    references_ready = bool(version and lineage_current(session, ctx, version.reference_sources_json))
    return bool(references_ready and qc and qc.findings_json["quality"] == "approved" and quality
        and all(r.review_value == "approved" for r in quality)
        and not _has_blocking_qc_failure(session, ctx, version_id))


def _quality_ready(qc, quality, blocked):
    return bool(qc and qc.findings_json['quality'] == 'approved' and quality
        and all(r.review_value == 'approved' for r in quality) and not blocked)


def handoff_quality_current(session, ctx, row, handoff):
    return bool(handoff.manifest_json.get("entries")) and all(
        ready(session, ctx, row, entry["asset_version_id"]) for entry in handoff.manifest_json["entries"])


def review_evidence_current(session, ctx, evaluation):
    """A new approval is new evidence, even when the visible outcome is unchanged."""
    inputs = evaluation.input_json
    reviews = {entry["asset_version_id"]: active_quality(session, ctx, entry["asset_version_id"])
        for entry in inputs["entries"]}
    if "quality_review_ids" in inputs:
        return inputs["quality_review_ids"] == {key: sorted(r.id for r in values) for key, values in reviews.items()}
    # Legacy snapshots predate explicit review IDs. Preserve them only when
    # every current review demonstrably predates the immutable evaluation.
    # A tied/unknown timestamp cannot prove the old review was the same one.
    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return bool(evaluation.created_at) and all(values and all(
        r.created_at and aware(r.created_at) < aware(evaluation.created_at) for r in values) for values in reviews.values())


def review(session, ctx, row, body):
    if scoped(session, ctx, Handoff, round_id=row.id):
        raise MarketingError("handoff_frozen", "导出已冻结；新版本请放到下一轮审核。", 409)
    assets = {v.id: (v, recipe, combo) for v, recipe, combo in creative_assets(session, ctx, row)}
    if body.asset_version_id not in assets:
        raise MarketingError("asset_not_in_round", "素材不属于当前方案与生成模式。")
    version, recipe, combo = assets[body.asset_version_id]
    required = required_checks(combo, row.snapshot_json["plan"])
    if set(body.checks) != set(required):
        raise MarketingError("qc_checks_incomplete", "请逐项检查主张、表达、产品与本轮对照要求。")
    peers = set(body.compared_asset_version_ids)
    if peers - assets.keys() or version.id in peers:
        raise MarketingError("comparison_asset_invalid", "请使用本轮其他测试组的对应版本进行对照。")
    if row.snapshot_json["plan"]["comparison_kind"] == "specific_factor" and body.quality == "approved":
        group_of = {c: g["id"] for g in row.snapshot_json["plan"]["groups"] for c in g["combination_ids"]}
        if not any(group_of[assets[p][2]["id"]] != group_of[combo["id"]] and (assets[p][0].content_sha256 != version.content_sha256 or assets[p][2]["headline"] != combo["headline"] or assets[p][2]["body_copy"] != combo["body_copy"]) for p in peers):
            raise MarketingError("paired_review_required", "具体因素比较需要同时核对另一组的不同执行版本。")
    if body.quality == "approved" and any(value != "pass" for value in body.checks.values()):
        raise MarketingError("semantic_qc_unresolved", "存在失败或待核对项，暂不能通过审核。")
    ai_context = {}
    if body.ai_qc_id or body.paired_ai_qc_id:
        from . import ai_qc
        calls = {}
        if body.ai_qc_id:
            calls["single"] = ai_qc.resolve_call(session, ctx, row, body.ai_qc_id, version.id, "single")
        if body.paired_ai_qc_id:
            calls["paired"] = ai_qc.resolve_call(session, ctx, row, body.paired_ai_qc_id, version.id, "paired")
        elif calls["single"].kind is None and ai_qc.latest(session, ctx, row, version.id, "paired").id == calls["single"].id:
            calls["paired"] = calls["single"]  # a pre-split call answered the comparison checks too
        if "paired" in calls:
            expected_peers = {i["asset_version_id"] for i in calls["paired"].input_json["images"] if i["role"] == "comparison"}
            if peers != expected_peers:
                raise MarketingError("ai_qc_comparison_changed", "对照版本已变化，请重新检查或明确使用人工审核。", 409)
        reports = {call.id: (call, ai_qc.effective_report(call)) for call in calls.values()}
        findings = [f for _, report in reports.values() for f in report["findings"]]
        proposed = {f["check_id"]: f["status"] for f in findings}
        overrides = {key: {"ai": proposed[key], "human": value} for key, value in body.checks.items() if key in proposed and proposed[key] != value}
        # Single-image results never stand in for the comparison: approving without one is a human override.
        manual = [kind for kind in QC_KINDS if kind not in calls]
        if (overrides or (manual and body.quality == "approved")) and not body.override_note.strip():
            raise MarketingError("qc_override_note_required", "请补充对 AI 疑点或结论的调整依据。")
        first = calls.get("single") or calls["paired"]
        ai_context = {"ai_qc_id": body.ai_qc_id, "ai_input_hash": first.input_hash,
            "ai_policy_version": first.policy_version, "normalization_version": reports[first.id][1]["normalization_version"],
            "effective_findings": findings, "overrides": overrides, "override_note": body.override_note,
            "qc_kinds": {kind: "ai" if kind in calls else "human" for kind in QC_KINDS}}
        if body.paired_ai_qc_id:
            ai_context.update(paired_ai_qc_id=body.paired_ai_qc_id, paired_ai_input_hash=calls["paired"].input_hash,
                paired_ai_policy_version=calls["paired"].policy_version)
    previous = active_quality(session, ctx, version.id)
    # Correct every active review so no older approval can bypass a rejection.
    try:
        for prior in previous[1:]:
            create_review(session, ctx, version.id, "cleared", body.note, (ctx.initiator_id or ctx.executor), prior.id)
        quality = create_review(session, ctx, version.id, body.quality, body.note, (ctx.initiator_id or ctx.executor), previous[0].id if previous else None)
    except (ReviewError, ReviewConflictError) as exc:
        raise MarketingError(exc.code, exc.detail, 409) from exc
    record = QC(id=new_id("mqc"), workspace_id=ctx.workspace_id, round_id=row.id, asset_version_id=version.id,
        quality_review_id=quality.id, actor_id=(ctx.initiator_id or ctx.executor),
        findings_json={**body.model_dump(mode="json", exclude={"paired_ai_qc_id"}), "source":"ai_assisted_human_review" if ai_context else "human_semantic_review", "required_checks": required, **ai_context,
                       "recipe_hash": recipe.recipe_hash, "plan_hash": row.snapshot_json["plan_hash"]})
    session.add(record)
    session.flush()
    audit(session, row, "creative_reviewed", {"review_id": record.id, "asset_version_id": version.id, "quality": body.quality})


def create_handoff(session, ctx, row, body):
    if scoped(session, ctx, Handoff, round_id=row.id):
        raise MarketingError("handoff_exists", "本轮已有冻结导出包，请查看其状态。", 409)
    ids = body.asset_version_ids
    if len(ids) != len(set(ids)):
        raise MarketingError("duplicate_asset", "同一素材不能重复计入。")
    assets = {v.id: (v, recipe, combo) for v, recipe, combo in creative_assets(session, ctx, row)}
    if set(ids) - assets.keys():
        raise MarketingError("asset_not_in_round", "导出只能包含当前方案的具体版本。")
    if not all(ready(session, ctx, row, item) for item in ids):
        raise MarketingError("qc_required", "请先完成所选素材的逐项质量审核。")
    plan = row.snapshot_json["plan"]
    if plan["comparison_kind"] == "specific_factor":
        for item in ids:
            peers = set(latest_qc(session, ctx, row, item).findings_json["compared_asset_version_ids"])
            if not peers or peers - set(ids):
                raise MarketingError("paired_export_required", "导出必须同时包含质量审核时核对的具体对照版本。")
    combos = {assets[item][2]["id"] for item in ids}
    # Preserve the approved design, including each angle's distinct expressions.
    if combos != {c["id"] for c in plan["combinations"]}:
        raise MarketingError("comparison_coverage_missing", "请为方案中的每个创意组合保留至少一条通过审核的素材；调整比较对象需要重新确认方案。")
    if sum(assets[item][0].byte_size for item in ids) > 100 * 1024 * 1024:
        raise MarketingError("export_too_large", "导出包素材超过 100MB，请减少执行版本。")
    from app.generation.paid_execution import asset_blob_ref
    from dataclasses import asdict
    from .board import execution_numbers
    from . import export_text
    # The package's own words follow the requester's interface language, frozen here: a retry
    # or a Worker restart rebuilds the same text.
    language = ctx.ui_language
    # The same numbers the board shows, so a Meta ad name says which execution (and revision) it carries.
    numbers, _ = execution_numbers([{"asset_version_id": v.id, "combination_id": combo["id"], "parent_version_id": v.parent_version_id,
        "content_sha256": v.content_sha256, "created_at": v.created_at.isoformat()} for v, _, combo in assets.values()])
    entries = []
    for item in sorted(ids):
        version, recipe, combo = assets[item]
        group = next(g for g in plan["groups"] if combo["id"] in g["combination_ids"])
        angle = next(a for a in plan["angles"] if a["id"] == combo["angle_id"])
        expression = next(e for e in angle["expressions"] if e["id"] == combo["expression_id"])
        entry = hashlib.sha256((row.id + ":" + version.id).encode()).hexdigest()[:24]
        ref = asset_blob_ref(session, ctx, item)
        feedback = recipe.canonical_json.get("selection", {}).get("marketing_feedback")
        provenance = recipe.canonical_json.get("image_edit") or recipe.canonical_json.get("reuse") or ({"kind": "qc_feedback_revision",
            **{k: feedback[k] for k in ("source_asset_version_id", "source_sha256", "qc_review_id", "ai_qc_id")},
            "feedback_id": feedback["id"], "feedback_hash": feedback["hash"],
            **({"image_tool": feedback["image_tool"]} if feedback.get("image_tool") else {})} if feedback else {"kind": "generated_for_plan"})
        entries.append({"entry_id": entry, "asset_version_id": item, "group_id": group["id"], "group_name": group["name"],
            "combination_id": combo["id"], "angle_id": combo["angle_id"], "expression_id": combo["expression_id"],
            "proposition": angle["proposition"], "expression_name": expression["name"],
            # ASCII from the English image brief, identical in every language (#255).
            "ad_name": export_text.ad_name(entry, combo, numbers[item]),
            "display_name": export_text.display_name(angle["proposition"], expression["name"], numbers[item], language),
            "headline": combo["headline"], "body_copy": combo["body_copy"], "content_sha256": version.content_sha256,
            "mime_type": version.mime_type, "blob": asdict(ref), "recipe_hash": recipe.recipe_hash,
            "provenance": provenance,
            "product_references": version.reference_sources_json,
            "provider": recipe.canonical_json.get("provider", provenance.get("provider")),
            "model": recipe.canonical_json.get("model", provenance.get("model")),
            "qc": latest_qc(session, ctx, row, item).findings_json})
    op = create_operation(session, command_type="marketing.export")
    record = Handoff(id=new_id("mh"), workspace_id=ctx.workspace_id, round_id=row.id, operation_id=op.id,
        state="queued", manifest_json={"schema_version": 2, "language": language, "workspace_id": ctx.workspace_id, "project_id": row.project_id,
            "round_id": row.id, "execution_mode": row.execution_mode, "snapshot": row.snapshot_json,
            "start_date": str(row.start_date), "end_date": str(row.end_date), "account_timezone": row.timezone,
            'currency': row.snapshot_json['currency'], 'attribution_window': row.snapshot_json['attribution_window'],
            "entries": entries})
    session.add(record)
    session.flush()
    enqueue_in_session(session, context=ctx, job_type="marketing.export", payload={"handoff_id": record.id},
        idempotency_key=record.id, resource_type="marketing_handoff", resource_id=record.id,
        safe_to_retry_after_crash=True, max_attempts=3)
    row.state = "handoff"
    audit(session, row, "handoff_requested", {"handoff_id": record.id, "manifest_hash": digest(record.manifest_json)})


def retry_handoff(session, ctx, row):
    records = scoped(session, ctx, Handoff, round_id=row.id)
    if not records or records[0].state != "failed":
        raise MarketingError("handoff_not_retryable", "只有失败的导出任务需要重试。", 409)
    record = records[0]
    if not all(ready(session, ctx, row, e["asset_version_id"]) for e in record.manifest_json["entries"]):
        raise MarketingError("qc_required", "冻结素材的质量审核已发生变化，请结束本轮并调整方案。", 409)
    op = create_operation(session, command_type="marketing.export")
    record.operation_id, record.state, record.error_code = op.id, "queued", None
    enqueue_in_session(session, context=ctx, job_type="marketing.export", payload={"handoff_id": record.id},
        idempotency_key=op.id, resource_type="marketing_handoff", resource_id=record.id,
        safe_to_retry_after_crash=True, max_attempts=3)
    audit(session, row, "handoff_retry_requested", {"handoff_id": record.id, "operation_id": op.id})


def bind(session, ctx, row, body):
    handoffs = scoped(session, ctx, Handoff, round_id=row.id)
    if not handoffs or handoffs[0].state != "ready":
        raise MarketingError("handoff_not_ready", "请先生成完整导出包。", 409)
    handoff = handoffs[0]
    if body.entry_id not in {e["entry_id"] for e in handoff.manifest_json["entries"]}:
        raise MarketingError("entry_not_found", "请选择本轮导出的素材。")
    account = body.ad_account_id.removeprefix("act_")
    existing = scoped(session, ctx, Binding, ad_account_id=account, ad_id=body.ad_id)
    if any(b.start_date <= row.end_date and b.end_date >= row.start_date for b in existing):
        raise MarketingError("overlapping_ad_binding", "该广告在这个日期范围内已对应其他记录，请核对广告 ID。", 409)
    if any(b.ad_account_id != account for b in scoped(session, ctx, Binding, round_id=row.id)):
        raise MarketingError("account_mismatch", "同一小轮必须使用同一个广告账户。")
    record = Binding(id=new_id("mab"), workspace_id=ctx.workspace_id, round_id=row.id, handoff_id=handoff.id,
        entry_id=body.entry_id, ad_account_id=account, ad_id=body.ad_id, start_date=row.start_date, end_date=row.end_date,
        conditions_json={"conditions_match": body.conditions_match, "deviations": body.deviations})
    session.add(record)
    if row.state != "closed":
        row.state = "awaiting_data"
    session.flush()
    audit(session, row, "ad_bound", {"binding_id": record.id, "entry_id": body.entry_id})


def style_deviations(session, ctx, row):
    """Creatives whose check found the brand baseline or style rule broken; a human review outranks the AI suggestion."""
    from . import ai_qc
    found = []
    for version, _, _ in creative_assets(session, ctx, row):
        qc = latest_qc(session, ctx, row, version.id)
        status = (qc.findings_json.get("checks") or {}).get("brand_style") if qc else None
        if status is None:
            call = ai_qc.latest(session, ctx, row, version.id, "single")
            report = ai_qc.effective_report(call) if call and call.state == "succeeded" else None
            status = next((f["status"] for f in (report or {}).get("findings", []) if f["check_id"] == "brand_style"), None)
        if status == "fail":
            found.append(version.id)
    return sorted(found)


def evaluate(session, ctx, row):
    from .evaluation import evaluate_snapshot, combination_key
    handoffs = scoped(session, ctx, Handoff, round_id=row.id)
    if not handoffs or handoffs[0].state != "ready":
        raise MarketingError("handoff_not_ready", "请先完成审核和导出。", 409)
    bindings = scoped(session, ctx, Binding, round_id=row.id)
    batches = active_batches(session, ctx, row)
    from zoneinfo import ZoneInfo
    mature = utcnow().astimezone(ZoneInfo(row.timezone)).date() > row.end_date + timedelta(days=row.snapshot_json["protocol"]["attribution_lag_days"])
    prior_confirmations = []
    freshness = {}
    for previous in scoped(session, ctx, Round, project_id=row.project_id, state="closed"):
        if previous.id == row.id or previous.execution_mode != "byok":
            continue
        evidence = latest_evaluation(session, ctx, previous)
        if not evidence or not evaluation_current(session, ctx, previous, evidence, _cache=freshness):
            continue
        for entry in evidence.input_json["entries"]:
            if entry["asset_version_id"] in evidence.result_json.get("qualified_asset_version_ids", []) and ready(session, ctx, previous, entry["asset_version_id"]):
                prior_confirmations.append({"combination_key": combination_key(previous.snapshot_json, entry),
                    "content_sha256": entry["content_sha256"], "evaluation_id": evidence.id,
                    "timezone": previous.timezone, "protocol": previous.snapshot_json["protocol"]})
    inputs = {"prior_confirmations": prior_confirmations, "timezone": row.timezone, "round_id": row.id, "snapshot": row.snapshot_json, "execution_mode": row.execution_mode,
        "start_date": str(row.start_date), "end_date": str(row.end_date), "mature": mature,
        "entries": handoffs[0].manifest_json["entries"],
        "bindings": [{"entry_id": b.entry_id, "ad_id": b.ad_id, **b.conditions_json} for b in sorted(bindings, key=lambda b: b.id)],
        "quality_changed": [e["entry_id"] for e in handoffs[0].manifest_json["entries"] if not ready(session, ctx, row, e["asset_version_id"])],
        "quality_review_ids": {e["asset_version_id"]: sorted(r.id for r in active_quality(session, ctx, e["asset_version_id"]))
            for e in handoffs[0].manifest_json["entries"]},
        "batch_ids": [b.id for b in batches], "rows": sorted([item for b in batches for item in b.rows_json], key=lambda d: (d["date"], d["ad_id"]))}
    if "style_variation" in row.snapshot_json["plan"]:
        inputs["style_deviations"] = style_deviations(session, ctx, row)
    hashed = digest(inputs)
    existing = scoped(session, ctx, Evaluation, round_id=row.id, input_hash=hashed)
    if existing:
        if row.state != "closed":
            row.state = "evaluated"
        return existing[0]
    result = evaluate_snapshot(inputs)
    rounds = scoped(session, ctx, Round, project_id=row.project_id)
    cap = min(rounds, key=lambda r: r.ordinal).snapshot_json["plan"]
    total_spend = project_spend(session, ctx, rounds)
    result["big_round_spend"] = str(total_spend)
    result["big_round_budget"] = cap["media_budget"]
    result["budget_exhausted"] = total_spend >= Decimal(cap["media_budget"])
    result["round_limit_reached"] = len(rounds) >= cap["rules"]["max_small_rounds"]
    if result["budget_exhausted"] or result["round_limit_reached"]:
        result["next_action"] = "close_and_summarize"
    record = Evaluation(id=new_id("me"), workspace_id=ctx.workspace_id, round_id=row.id,
        input_hash=hashed, input_json=inputs, result_json=result)
    session.add(record)
    if row.state != "closed":
        row.state = "evaluated"
    session.flush()
    audit(session, row, "evaluated", {"evaluation_id": record.id, "input_hash": hashed})
    return record


def latest_evaluation(session, ctx, row):
    return session.scalar(select(Evaluation).where(Evaluation.workspace_id == ctx.workspace_id, Evaluation.round_id == row.id)
        .order_by(Evaluation.created_at.desc(), Evaluation.id.desc()).limit(1))


def evaluation_current(session, ctx, row, evaluation, *, _seen=None, _cache=None):
    """Read-only freshness gate; an immutable past decision is not a current recommendation."""
    seen = set(_seen or ())
    if not evaluation or evaluation.id in seen or row.state not in ("evaluated", "closed"):
        return False
    cache = {} if _cache is None else _cache
    if evaluation.id in cache:
        return cache[evaluation.id]
    cache[evaluation.id] = False
    seen.add(evaluation.id)
    inputs = evaluation.input_json
    latest = latest_evaluation(session, ctx, row)
    if (not latest or latest.id != evaluation.id or digest(inputs) != evaluation.input_hash
            or inputs["snapshot"] != row.snapshot_json or inputs["round_id"] != row.id
            or inputs["execution_mode"] != row.execution_mode or inputs["timezone"] != row.timezone
            or inputs["start_date"] != str(row.start_date) or inputs["end_date"] != str(row.end_date)):
        return False
    if not review_evidence_current(session, ctx, evaluation):
        return False
    batches = active_batches(session, ctx, row)
    bindings = [{"entry_id": b.entry_id, "ad_id": b.ad_id, **b.conditions_json}
        for b in sorted(scoped(session, ctx, Binding, round_id=row.id), key=lambda b: b.id)]
    quality_changed = {e["entry_id"] for e in inputs["entries"] if not ready(session, ctx, row, e["asset_version_id"])}
    if (inputs["batch_ids"] != [b.id for b in batches] or inputs["bindings"] != bindings
            or inputs["rows"] != sorted([item for b in batches for item in b.rows_json], key=lambda d: (d["date"], d["ad_id"]))
            or set(inputs.get("quality_changed", [])) != quality_changed):
        return False
    from zoneinfo import ZoneInfo
    mature = utcnow().astimezone(ZoneInfo(row.timezone)).date() > row.end_date + timedelta(days=row.snapshot_json["protocol"]["attribution_lag_days"])
    if inputs["mature"] != mature:
        return False
    from .evaluation import combination_key
    for proof in inputs.get("prior_confirmations", []):
        supporting = scoped(session, ctx, Evaluation, id=proof["evaluation_id"])
        if not supporting:
            return False
        prior = get_round(session, ctx, supporting[0].round_id)
        if (prior.project_id != row.project_id or prior.id == row.id or prior.state != "closed"
                or prior.execution_mode != "byok" or not evaluation_current(session, ctx, prior, supporting[0], _seen=seen, _cache=cache)):
            return False
        if not any(e["asset_version_id"] in supporting[0].result_json.get("qualified_asset_version_ids", [])
                and e["content_sha256"] == proof["content_sha256"]
                and combination_key(prior.snapshot_json, e) == proof["combination_key"]
                and prior.timezone == proof["timezone"] and prior.snapshot_json["protocol"] == proof["protocol"]
                for e in supporting[0].input_json["entries"]):
            return False
    cache[evaluation.id] = True
    return True


def close(session, ctx, row, body):
    evaluation = evaluate(session, ctx, row) if scoped(session, ctx, Handoff, round_id=row.id, state="ready") else None
    result = evaluation.result_json if evaluation else {}
    if body.reason == "completed" and not result.get("confirmed_combinations"):
        raise MarketingError("reuse_evidence_required", "本轮仅确认具体素材或组合候选；可复用结论还需要跨小轮的新执行和新增投放证据。")
    if body.reason == "asset_only" and (row.execution_mode != "byok" or not result.get("qualified_asset_version_ids")):
        raise MarketingError("qualified_asset_required", "尚无真实回传数据支持达标素材，请按证据不足或主动停止结束。")
    if body.reason == "no_qualified" and (row.execution_mode != "byok" or not result.get("all_assets_failed")):
        raise MarketingError("failure_evidence_required", "不能把未知或样本不足当作失败。")
    row.state = "closed"
    row.close_json = {"reason": body.reason, "note": body.note, "evaluation_id": evaluation.id if evaluation else None,
        "plan_hash": row.snapshot_json["plan_hash"], "execution_mode": row.execution_mode, "closed_at": utcnow().isoformat()}
    audit(session, row, "round_closed", row.close_json)


def round_dict(session, ctx, row, *, include_image_edits=False, include_version_history=False, include_languages=False, include_plan_review=False,
               include_board=False, include_brand_style=False, include_execution_plan=False, include_qc_kinds=False,
               include_text_language=False, include_qc_evidence=False):
    include_image_edits = include_image_edits or include_version_history
    from . import image_editing
    from .workflow_models import MarketingFeedbackDraftRecord
    from . import explanation
    from . import ai_qc
    from .board import comparison_scope
    from .ai_qc_models import MarketingAIQCRecord
    creative_records = creative_assets(session, ctx, row)
    ids = [v.id for v, _, _ in creative_records]
    # Query each relation once, irrespective of how many variants are shown.
    qcs, ai_calls, reviews = {}, {}, {}
    if ids:
        records = session.scalars(select(QC).where(QC.workspace_id == ctx.workspace_id,
            QC.round_id == row.id, QC.asset_version_id.in_(ids)).order_by(QC.created_at.desc(), QC.id.desc())).all()
        for qc in records:
            qcs.setdefault(qc.asset_version_id, qc)
        records = session.scalars(select(MarketingAIQCRecord).where(MarketingAIQCRecord.workspace_id == ctx.workspace_id,
            MarketingAIQCRecord.round_id == row.id, MarketingAIQCRecord.asset_version_id.in_(ids))
            .order_by(MarketingAIQCRecord.sequence.desc(), MarketingAIQCRecord.created_at.desc(), MarketingAIQCRecord.id.desc())).all()
        for call in records:
            for kind in ai_qc.covers(call):
                ai_calls.setdefault((call.asset_version_id, kind), call)
        records = session.scalars(select(QualityReviewRecord).where(QualityReviewRecord.workspace_id == ctx.workspace_id,
            QualityReviewRecord.asset_version_id.in_(ids))).all()
        replaced = {r.supersedes_review_id for r in records if r.supersedes_review_id}
        for review in records:
            if review.id not in replaced and review.review_value != 'cleared':
                reviews.setdefault(review.asset_version_id, []).append(review)
    facts = {f["asset_version_id"]: f for f in execution_facts(session, ctx, row, creative_records, qcs)}
    scope = comparison_scope(row.snapshot_json["plan"], facts.values())
    from app.generation.paid_execution import asset_blob_refs
    blob_refs = asset_blob_refs(session, ctx, [v for v, _, _ in creative_records]) if ai_calls else {}
    projected = {}

    def check(call):
        if call and call.id not in projected:
            projected[call.id] = ai_qc.projection(session, ctx, row, call, assets=creative_records, blob_refs=blob_refs,
                quality_by_asset=qcs, scope=scope)
        return projected[call.id] if call else None
    assets, executions = [], []
    from .product_references import appearance
    appearances = {}
    for version, recipe, combo in creative_records:
        qc, quality, fact = qcs.get(version.id), reviews.get(version.id, []), facts[version.id]
        frozen_references = recipe.canonical_json.get("selection", {}).get("product_references", [])
        appearance_key = digest([frozen_references, version.reference_sources_json])
        if include_board and appearance_key not in appearances:
            appearances[appearance_key] = appearance(session, ctx, frozen_references, version)
        single, paired = check(ai_calls.get((version.id, "single"))), check(ai_calls.get((version.id, "paired")))
        assets.append({**({"parent_version_id": version.parent_version_id} if include_image_edits else {}),
            **({"asset_id": version.asset_id, "created_at": version.created_at.isoformat(), "edit_operation": operation_of(recipe)} if include_version_history else {}),
            "ai_qc": single, "paired_ai_qc": paired, "asset_version_id": version.id, "combination_id": combo["id"], "headline": combo["headline"],
            "body_copy": combo["body_copy"], "required_checks": required_checks(combo, row.snapshot_json["plan"]), "quality": qc.findings_json if qc else None,
            "ready": fact["references_current"] and _quality_ready(qc, quality, fact["blocking"]), "mime_type": version.mime_type})
        executions.append({**fact, "ready": assets[-1]["ready"],
            **({"product_appearance": appearances[appearance_key]} if include_board else {}),
            **{key: {**c, "quality_suggestion": (c["result"] or {}).get("quality_suggestion")} if c else None
               for key, c in (("ai_qc", single), ("paired_ai_qc", paired))}})
    handoffs = scoped(session, ctx, Handoff, round_id=row.id)
    handoff = handoffs[0] if handoffs else None
    evaluation = latest_evaluation(session, ctx, row) if row.state in ("evaluated", "closed") else None
    rounds = scoped(session, ctx, Round, project_id=row.project_id)
    cap = min(rounds, key=lambda r: r.ordinal).snapshot_json["plan"]
    remaining_budget = max(Decimal("0"), Decimal(cap["media_budget"]) - project_spend(session, ctx, rounds))
    remaining_rounds = max(0, cap["rules"]["max_small_rounds"] - len(rounds))
    completed = any((r.close_json or {}).get("reason") == "completed" for r in rounds)
    continuation = {"can_plan": not completed and remaining_budget > 0 and remaining_rounds > 0 and all(r.state == "closed" for r in rounds),
        "remaining_budget": str(remaining_budget), "remaining_rounds": remaining_rounds,
        "limit_reached": remaining_budget <= 0 or remaining_rounds <= 0, "completed": completed}
    result = {"continuation": continuation, "id": row.id, "revision": row.revision, "plan_revision": row.plan_revision, "ordinal": row.ordinal, "state": row.state,
        "execution_mode": row.execution_mode, "snapshot": row.snapshot_json, "start_date": str(row.start_date),
        "end_date": str(row.end_date), "timezone": row.timezone, "assets": assets,
        "handoff": {"id": handoff.id, "state": handoff.state, "error_code": handoff.error_code,
            "quality_current": handoff_quality_current(session, ctx, row, handoff),
            "manifest_hash": digest(handoff.manifest_json), "language": handoff.manifest_json.get("language"), "entries": [{k: v for k, v in e.items() if k not in ("blob", "qc")} for e in handoff.manifest_json["entries"]]} if handoff else None,
        "bindings": [{"id": b.id, "entry_id": b.entry_id, "ad_account_id": b.ad_account_id, "ad_id": b.ad_id, **b.conditions_json}
                     for b in scoped(session, ctx, Binding, round_id=row.id)],
        "batches": [{"id": b.id, "state": b.state, "raw_hash": b.raw_hash, "metadata": b.metadata_json,
            "row_count": len(b.rows_json), "issues": b.issues_json, "replaces_batch_id": b.replaces_id}
            for b in sorted(scoped(session, ctx, Batch, round_id=row.id), key=lambda b: (b.created_at, b.id))],
        "evaluation": {"id": evaluation.id, "input_hash": evaluation.input_hash,
            "currency": row.snapshot_json['currency'],
            "attribution_window": row.snapshot_json['attribution_window'], "account_timezone": row.timezone,
            **evaluation.result_json, "current": evaluation_current(session, ctx, row, evaluation)} if evaluation else None,
        "explanation": explanation.projection(session, ctx, row, evaluation),
        "feedback_drafts": [{"id": f.id, "draft_id": f.draft_id, "source_asset_version_id": f.source_asset_version_id,
            "combination_id": f.feedback_json["combination_id"], "request_note": f.feedback_json["request_note"],
            "provider": f.feedback_json["provider"], "model": f.feedback_json["model"],
            **({"image_tool": f.feedback_json["image_tool"]} if include_image_edits and f.feedback_json.get("image_tool") else {})} for f in
            sorted(scoped(session, ctx, MarketingFeedbackDraftRecord, round_id=row.id), key=lambda f: (f.created_at, f.id))],
        **({"image_edits": image_editing.summaries(session, ctx, row)} if include_image_edits else {}), "close": row.close_json}
    if include_board:
        result["board"] = board_projection(session, ctx, row, executions, handoff, result["evaluation"])
        if not include_execution_plan:
            for combo in (c for a in result["board"]["tree"]["angles"] for x in a["expressions"] for c in x["combinations"]):
                combo.pop("planned_executions")
                for node in combo["executions"]:
                    node.pop("execution_version")
    if not include_qc_kinds:
        legacy_qc_projection(result)
    if not include_qc_evidence:
        legacy_qc_evidence_projection(result)
    if not include_text_language:
        legacy_text_projection(result)

    # A round shows no competitor review; its frozen plan keeps the cited snapshot (#273).
    from .competitor_review import legacy_projection as competitor_projection
    result["snapshot"] = {**result["snapshot"], "plan": competitor_projection(result["snapshot"]["plan"])}
    if not include_plan_review:
        from .plan_review import legacy_review_projection
        result["snapshot"] = {**row.snapshot_json, "plan": legacy_review_projection(result["snapshot"]["plan"])}
    if not include_brand_style:
        from .plan_review import legacy_style_projection
        result["snapshot"] = {**result["snapshot"], "plan": legacy_style_projection(result["snapshot"]["plan"])}
    if not include_languages:
        from .languages import legacy_plan_projection
        result["snapshot"] = {**result["snapshot"], "plan": legacy_plan_projection(result["snapshot"]["plan"])}
        for call in [*(a.get(key) for a in result["assets"] for key in ("ai_qc", "paired_ai_qc")), result["explanation"]]:
            if call:
                call.pop("language", None)
        for entry in (result.get("handoff") or {}).get("entries", []):
            provenance = entry.get("provenance") or {}
            if provenance.get("combination"):
                entry["provenance"] = {**provenance, "combination": {k: v for k, v in provenance["combination"].items() if k != "image_brief"}}
    if not include_execution_plan:
        from .executions import legacy_projection
        result["snapshot"] = {**result["snapshot"], "plan": legacy_projection(result["snapshot"]["plan"])}
        for entry in (result.get("handoff") or {}).get("entries", []):
            provenance = entry.get("provenance") or {}
            if provenance.get("combination"):
                entry["provenance"] = {**provenance, "combination": {k: v for k, v in provenance["combination"].items() if k != "executions"}}
    return result if include_image_edits else legacy_round_projection(result)


def legacy_text_projection(result):
    """Drop #255's fields for clients that predate them: their strict validators reject unknown keys."""
    if result.get("handoff"):
        result["handoff"].pop("language", None)
        for entry in result["handoff"]["entries"]:
            entry.pop("display_name", None)
    result["batches"] = [{**b, "issues": [{k: v for k, v in i.items() if k != "params"} for i in b["issues"]]}
                         for b in result["batches"]]


def board_projection(session, ctx, row, executions, handoff, evaluation):
    from . import board
    project = get_project(session, ctx, row.project_id)
    generating = session.scalars(select(GenerationRecipeSnapshotRecord.canonical_json).join(GenerationRunRecord,
        (GenerationRunRecord.recipe_snapshot_id == GenerationRecipeSnapshotRecord.id) & (GenerationRunRecord.workspace_id == ctx.workspace_id))
        .where(GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id, GenerationRunRecord.status.in_(("queued", "running")),
            GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string() == row.project_id,
            GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['plan_revision'].as_integer() == row.plan_revision)).all()
    bindings = {}
    for b in scoped(session, ctx, Binding, round_id=row.id):
        bindings.setdefault(b.entry_id, []).append(b.ad_id)
    from app.generation.models import GenerationDraftRecord
    from .models import MarketingGenerationLinkRecord
    from .product_references import appearance
    prepared = session.execute(select(MarketingGenerationLinkRecord.combination_id, GenerationDraftRecord.selectors_json)
        .join(GenerationDraftRecord, (GenerationDraftRecord.id == MarketingGenerationLinkRecord.draft_id)
            & (GenerationDraftRecord.workspace_id == ctx.workspace_id))
        .where(MarketingGenerationLinkRecord.workspace_id == ctx.workspace_id, MarketingGenerationLinkRecord.project_id == row.project_id,
            MarketingGenerationLinkRecord.plan_revision == row.plan_revision)).all()
    return board.project(row.snapshot_json["plan"], executions, round_state=row.state,
        prepared_appearance={cid: appearance(session, ctx, selectors.get("product_references", [])) for cid, selectors in prepared},
        handoff={"state": handoff.state, "entries": {e["asset_version_id"]: e["entry_id"] for e in handoff.manifest_json["entries"]}} if handoff else None,
        bindings=bindings, data_ad_ids={item["ad_id"] for b in active_batches(session, ctx, row) for item in b.rows_json},
        generating={c["selection"]["marketing_intent"].get("combination", {}).get("id") for c in generating},
        generation_open=row.state == "reviewing" and not handoff and project.state == "plan_confirmed" and project.revision == row.plan_revision,
        evaluation_current=bool(evaluation and evaluation["current"]))


def board_thumbnail_refs(session, ctx, board):
    """The 512px derivative refs of a board's generated executions, in one Workspace-scoped query.

    Signing is async and belongs after the session closes (`api._sign_board_thumbnails`); a foreign or unknown version ID
    is simply absent, so it can never surface another Workspace's link.
    """
    from app.generation.thumbnails import thumbnail_refs_for_versions
    from .board import execution_nodes
    return thumbnail_refs_for_versions(session, ctx, (x["asset_version_id"] for x in execution_nodes(board)))


QC_KIND_KEYS = ("paired_ai_qc_id", "paired_ai_input_hash", "paired_ai_policy_version", "qc_kinds")


def legacy_qc_projection(result):
    """Strict BFF readers from before the single/paired split see one AI check per asset and the J-11 board stages."""
    from .board import legacy_qc_projection as legacy_board
    for asset in result["assets"]:
        asset.pop("paired_ai_qc", None)
        if asset["ai_qc"]:
            asset["ai_qc"] = {k: v for k, v in asset["ai_qc"].items() if k != "kind"}
        if asset["quality"]:
            asset["quality"] = {k: v for k, v in asset["quality"].items() if k not in QC_KIND_KEYS}
    if result.get("board"):
        legacy_board(result["board"])
    return result


QC_EVIDENCE_KEYS = ("rendered_text", "rendered_text_diff", "error_detail")


def legacy_qc_evidence_projection(result):
    """Drop #294/#295's fields for clients that predate them: their strict validators reject unknown keys."""
    def strip(value):
        return {k: v for k, v in value.items() if k not in QC_EVIDENCE_KEYS}
    for asset in result["assets"]:
        for key in ("ai_qc", "paired_ai_qc"):
            call = asset.get(key)
            if call:
                asset[key] = {**strip(call), "result": call["result"] and {**strip(call["result"]),
                    "findings": [strip(f) for f in call["result"]["findings"]]}}
        if asset["quality"] and "effective_findings" in asset["quality"]:
            asset["quality"] = {**asset["quality"], "effective_findings": [strip(f) for f in asset["quality"]["effective_findings"]]}
    return result


def legacy_round_projection(result):
    """Keep old strict BFF validators usable while the UI rolls forward."""
    result.pop("image_edits", None)
    for asset in result["assets"]:
        asset.pop("parent_version_id", None)
        for key in ("asset_id", "created_at", "edit_operation"):
            asset.pop(key, None)
    for entry in (result.get("handoff") or {}).get("entries", []):
        provenance = entry.get("provenance") or {}
        if "image_tool" in provenance:
            entry["provenance"] = {k: v for k, v in provenance.items() if k != "image_tool"}
        if provenance.get("kind") == "image_edit":
            entry["provenance"] = {"kind": "existing_creative", "origin_kind": "edit",
                "asset_version_id": entry["asset_version_id"], "content_sha256": entry["content_sha256"],
                "source_asset_version_id": provenance["source_asset_version_id"], "source_sha256": provenance["source_sha256"]}
    return result


def workflow_dict(session, ctx, project_id, *, include_image_edits=False, include_version_history=False, include_languages=False, include_plan_review=False,
                  include_board=False, include_brand_style=False, include_execution_plan=False, include_qc_kinds=False, include_qc_evidence=False,
                  include_text_language=False):
    from .workflow_models import MarketingImageEditRecord
    from .ai_qc_models import MarketingAIQCRecord
    get_project(session, ctx, project_id)
    rounds = sorted(scoped(session, ctx, Round, project_id=project_id), key=lambda r: r.ordinal)
    busy = False
    if rounds:
        # Ask about running jobs before projecting the round (#307). Each statement reads the latest commit, so a job
        # finishing in between would otherwise be shown running and reported idle, and the client would stop polling
        # on that frame. A finished job never runs again: idle here means idle in the projection below.
        busy = bool(session.scalar(select(GenerationRunRecord.id).join(GenerationRecipeSnapshotRecord,
            (GenerationRecipeSnapshotRecord.id == GenerationRunRecord.recipe_snapshot_id)
            & (GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id)).where(
                GenerationRunRecord.workspace_id == ctx.workspace_id,
                GenerationRunRecord.status.in_(('queued', 'running')),
                GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string() == project_id,
                GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['plan_revision'].as_integer() == rounds[-1].plan_revision
            ).union_all(select(MarketingImageEditRecord.id).where(
                MarketingImageEditRecord.workspace_id == ctx.workspace_id,
                MarketingImageEditRecord.round_id == rounds[-1].id, MarketingImageEditRecord.state == 'queued'),
            select(MarketingAIQCRecord.id).where(
                MarketingAIQCRecord.workspace_id == ctx.workspace_id, MarketingAIQCRecord.round_id == rounds[-1].id,
                MarketingAIQCRecord.state.in_(('queued', 'running')))).limit(1)))
    current = round_dict(session, ctx, rounds[-1], include_image_edits=include_image_edits, include_version_history=include_version_history, include_languages=include_languages, include_plan_review=include_plan_review, include_board=include_board, include_brand_style=include_brand_style, include_execution_plan=include_execution_plan, include_qc_kinds=include_qc_kinds, include_text_language=include_text_language, include_qc_evidence=include_qc_evidence) if rounds else None
    if current:
        # The handoff and explanation states come from the projection itself, so they always agree with it.
        busy = bool(busy
            or (current['handoff'] or {}).get('state') in ('queued', 'running')
            or (current['explanation'] or {}).get('state') in ('queued', 'running'))
    return {"project_id": project_id, "rounds": [{"id": r.id, "ordinal": r.ordinal, "state": r.state,
        "close": r.close_json} for r in rounds], "current": current,
        'poll_after_ms': 3000 if busy else 0,
        "next_action": "freeze_round" if not current else "prepare_next_plan" if current["state"] == "closed" and current["continuation"]["can_plan"] else
            "summarize_and_end" if current["state"] == "closed" else
            "review_creatives" if current["state"] == "reviewing" else "download_and_bind" if current["state"] == "handoff" else
            "import_delivery_data" if current["state"] == "awaiting_data" else "read_results_and_close"}
