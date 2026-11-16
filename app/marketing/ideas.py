"""Ads-first ideas (ADR 0018): one product's buying claims and ad drafts, before any test settings.

An ads-first project is a ``MarketingProjectRecord`` with ``flow = "ads_first"``. Its states are only
``input``, ``understanding_draft`` and ``ideas_ready``, its ``plan_json`` stays empty in step 1, and it
always has a product. Its ads are the generated images whose recipe intent names the project and the
ads-first flow (design D6); no round is involved. The plan-first endpoints never return these projects.
"""
from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import select

from app.adapters.persistence.models import (AssetVersionRecord, GenerationRunRecord, LibraryEntryRecord, ProductRecord,
                                            new_id, utcnow)
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.generation.selection import SelectionError
from app.platform.audit import append_event
from . import service
from .models import MarketingAdKeepRecord, MarketingProjectRecord

FLOW = "ads_first"
#: The first batch (step-1 plan, Global Constraints): claims, ways of telling per claim, ads per way.
FIRST_BATCH = {"angles": 3, "expressions": 2, "combinations": 1}
#: States from which ideas may be drafted: an understanding exists, checked or not (ADR 0018 §1).
DRAFTABLE = ("understanding_draft", "ideas_ready")


def _product_anchor(session, ctx, product_id):
    """The workspace product an ads-first project hangs on, given its ``prod_`` id or its Library entry id.

    The first batch builds ideas on a product draft that is published only when the user checks it (D3),
    so a draft product is accepted; an archived one is not.
    """
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
        (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id))
        .order_by(ProductRecord.product_profile_entry_id.is_(None), ProductRecord.id).limit(1))
    if product is not None:
        entry = session.scalar(select(LibraryEntryRecord).where(LibraryEntryRecord.workspace_id == ctx.workspace_id,
            LibraryEntryRecord.id == product.product_profile_entry_id)) if product.product_profile_entry_id else None
        if entry is None or entry.lifecycle == "active":
            return product.id
    else:
        try:  # a published Library product that has no product anchor yet gets one
            return service.product_card(session, ctx, product_id)[0]
        except SelectionError:
            pass
    raise service.MarketingError("product_not_found", "找不到这个产品。", 404)


def create_ideas_project(session, ctx, *, product_id: str, source_text: str, market: str, ad_language: str):
    service.ensure_active(session, ctx)
    anchor = _product_anchor(session, ctx, product_id)
    if not source_text.strip() or len(source_text) > 16000:
        raise service.MarketingError("source_invalid", "请粘贴不超过 16000 字的产品资料。", params={"limit": 16000})
    row = MarketingProjectRecord(id=new_id("mp"), workspace_id=ctx.workspace_id, product_id=anchor, flow=FLOW,
        source_text=source_text.strip(), market=market.strip(), ad_language=ad_language, revision=1, state="input")
    session.add(row)
    session.flush()
    service.checkpoint(session, ctx, row, "created")
    return row


def get_ideas_project(session, ctx, project_id: str):
    return service.get_project(session, ctx, project_id, flow=FLOW)


def project_ads(session, ctx, row):
    """This project's ads, oldest first: images whose recipe intent names the project and the ads-first flow."""
    return _ads_by_project(session, ctx, [row.id])[row.id]


def _ads_by_project(session, ctx, project_ids):
    """``project_ads`` of several projects in one query (the list reads every row's counts)."""
    records = session.execute(select(AssetVersionRecord, GenerationRecipeSnapshotRecord)
        .join(RunItemRecord, (RunItemRecord.id == AssetVersionRecord.producing_run_item_id) & (RunItemRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRunRecord, (GenerationRunRecord.id == RunItemRecord.generation_run_id) & (GenerationRunRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRecipeSnapshotRecord, (GenerationRecipeSnapshotRecord.id == GenerationRunRecord.recipe_snapshot_id) & (GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id)
        .where(GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string().in_(project_ids))
        .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)).all() if project_ids else []
    ads = {project_id: [] for project_id in project_ids}
    for version, recipe in records:
        intent = _intent(recipe)
        if intent.get("project_id") in ads and intent.get("flow") == FLOW:
            ads[intent["project_id"]].append((version, recipe))
    return ads


def _intent(recipe):
    return (recipe.canonical_json or {}).get("selection", {}).get("marketing_intent") or {}


def _keeps(session, ctx, row):
    return _keeps_by_project(session, ctx, [row.id])[row.id]


def _keeps_by_project(session, ctx, project_ids):
    keeps = {project_id: {} for project_id in project_ids}
    for mark in session.scalars(select(MarketingAdKeepRecord).where(MarketingAdKeepRecord.workspace_id == ctx.workspace_id,
            MarketingAdKeepRecord.project_id.in_(project_ids))).all() if project_ids else []:
        keeps[mark.project_id][mark.asset_version_id] = mark.kept
    return keeps


def set_kept(session, ctx, row, asset_version_id: str, kept: bool) -> None:
    service.ensure_active(session, ctx)
    if asset_version_id not in {version.id for version, _ in project_ads(session, ctx, row)}:
        raise service.MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)
    # Serialize marks on one project so two first marks of the same ad never race on the unique key.
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
        MarketingProjectRecord.id == row.id).with_for_update())
    mark = session.scalar(select(MarketingAdKeepRecord).where(MarketingAdKeepRecord.workspace_id == ctx.workspace_id,
        MarketingAdKeepRecord.project_id == row.id, MarketingAdKeepRecord.asset_version_id == asset_version_id))
    actor = ctx.initiator_id or ctx.executor
    if mark is None:
        session.add(MarketingAdKeepRecord(id=new_id("mak"), workspace_id=ctx.workspace_id, project_id=row.id,
            asset_version_id=asset_version_id, kept=kept, actor_id=actor, updated_at=utcnow()))
    else:
        mark.kept, mark.actor_id, mark.updated_at = kept, actor, utcnow()
    append_event(session, event_type="marketing.ad_kept", resource_type="marketing_project", resource_id=row.id,
                 details={"asset_version_id": asset_version_id, "kept": kept})
    session.flush()


def _ad(version, recipe, keeps):
    combination = _intent(recipe).get("combination") or {}
    return {"asset_version_id": version.id, "combination_id": combination.get("id", ""),
            "angle_id": combination.get("angle_id", ""), "expression_id": combination.get("expression_id", ""),
            # Step 1 has no checking yet, so every ad is a draft (ADR 0018 §2); S10 derives the others.
            "status": "draft", "kept": keeps.get(version.id, False),
            "created_at": version.created_at.isoformat() if version.created_at else ""}


def ideas_dict(session, ctx, row) -> dict:
    """The ``IdeasProjectDetail`` of an ads-first project."""
    call = service.latest_call(session, ctx, row.id)
    keeps = _keeps(session, ctx, row)
    return {"id": row.id, "revision": row.revision, "state": row.state, "product_id": row.product_id,
            "market": row.market, "ad_language": row.ad_language, "understanding": row.understanding_json,
            "ideas": row.ideas_json,
            "latest_call": service.call_dict(call, include_maximum_cost=True) if call else None,
            "ads": [_ad(version, recipe, keeps) for version, recipe in project_ads(session, ctx, row)],
            "pending": []}


def ideas_summary(session, ctx, row) -> dict:
    """The ``IdeasProjectSummary`` of an ads-first project."""
    return ideas_summaries(session, ctx, [row])[0]


def ideas_summaries(session, ctx, rows) -> list[dict]:
    """``ideas_summary`` of several projects with a fixed number of queries, in the order given."""
    ids = [row.id for row in rows]
    ads, keeps = _ads_by_project(session, ctx, ids), _keeps_by_project(session, ctx, ids)
    product_ids = {row.product_id for row in rows}
    names = {p.id: p.name for p in session.scalars(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                                              ProductRecord.id.in_(product_ids))).all()} if rows else {}
    result = []
    for row in rows:
        own = {version.id for version, _ in ads[row.id]}
        name = names.get(row.product_id) or (row.understanding_json or {}).get("product_name") or row.source_text[:80]
        result.append({"id": row.id, "revision": row.revision, "state": row.state, "product_id": row.product_id,
                       "product_name": name, "ad_count": len(own), "kept_count": sum(1 for ad in own if keeps[row.id].get(ad)),
                       "updated_at": row.updated_at.isoformat() if row.updated_at else ""})
    return result


def start_ideas_planning(session, ctx, row, revision, kind, mode, quote_hash, strategy_language="en-US"):
    """Admit a drafting call of ads-first ideas: the product understanding, or the ideas drawn from it (#190).

    Neither needs a checked fact (ADR 0018 §1). The understanding keeps the plan-first checks on quotes and
    claims; the ideas need a drafted understanding. Both are ordinary paid planning calls: one job, one
    reservation, settled by the Worker, and an explicit retry carries the last rejected draft."""
    if kind == "understanding":
        return service.start_planning(session, ctx, row, revision, "understanding", mode, quote_hash,
                                      ad_language=row.ad_language, strategy_language=strategy_language)
    if row.state not in DRAFTABLE or not row.understanding_json:
        raise service.MarketingError("understanding_required", "请先生成或完善产品理解。")
    cap = service.planner_capability(session, ctx)
    if mode == "byok" and (not cap["available"] or quote_hash != cap["quote_hash"]):
        raise service.MarketingError("planner_unavailable", "策划凭据、报价或预算不可用，请刷新配置。")
    from . import brand_kits
    from .expression_catalog import planner_reference
    from .planner import system_prompt
    inputs = {"source_text": row.source_text, "understanding": row.understanding_json, "market": row.market,
              "ad_language": row.ad_language, "strategy_language": strategy_language,
              "expression_catalog": planner_reference()}
    brand_ref = brand_kits.default_ref(session, ctx, row)
    if brand_ref:
        inputs["brand_kit"] = brand_kits.qc_summary(brand_kits.resolve_ref(session, ctx, brand_ref))
    repair = service.planning_repair_context(session, ctx, row, "ideas")
    if repair:
        inputs["rejected_plan_feedback"] = repair

    def size():
        return len((system_prompt("ideas", inputs) + json.dumps(inputs, ensure_ascii=False)).encode("utf-8"))
    if repair and size() > 96000:
        repair.pop("rejected_plan", None)
    if size() > 96000:
        raise service.MarketingError("planning_input_too_large", "产品资料与结构化理解过长，请精简后再起草方案。")
    service.advance(session, ctx, row, revision, "planning_requested")
    return service.admit_call(session, ctx, row, "ideas", mode, cap, inputs)


def _fields(*paths):
    return [f"body.ideas.{path}" for path in paths]


def checked_ideas(row, value, inputs):
    """Drafted ideas as they may be stored, or ``MarketingError``: the whole draft is refused, never trimmed.

    Design doc §6: a way of telling that repeats another, or lacks one of its three elements, is rewritten
    by the system before any image -- here, the call is ``planner_output_invalid`` and an explicit retry
    carries the errors. The project's market and the product's brand baseline are the application's."""
    from .contracts import BrandKitRef, Understanding
    from .languages import english_brief_matches, english_text, script_matches
    from .plan_review import indistinct_expressions
    if value.ad_language != inputs.get("ad_language") or value.strategy_language != inputs.get("strategy_language", "en-US"):
        raise service.MarketingError("planning_language_mismatch", "The plan must retain the requested languages.")
    pairs = Counter((c.angle_id, c.expression_id) for c in value.combinations)
    if (len(value.angles) > FIRST_BATCH["angles"] or any(len(a.expressions) > FIRST_BATCH["expressions"] for a in value.angles)
            or any(count > FIRST_BATCH["combinations"] for count in pairs.values())):
        raise service.MarketingError("ideas_first_batch_too_large",
                                     "首批最多 3 个主张、每个主张 2 种讲法、每种讲法 1 张广告。")
    understanding = Understanding.model_validate(row.understanding_json)
    facts, inferences = {f.id for f in understanding.facts}, {i.id for i in understanding.inferences}
    for ai, angle in enumerate(value.angles):
        if set(angle.fact_ids) - facts or set(angle.inference_ids) - inferences:
            raise service.MarketingError("plan_evidence_invalid", "角度引用了不存在的事实或推测。",
                                         fields=_fields(f"angles.{ai}"))
        for ei, expression in enumerate(angle.expressions):
            if set(expression.evidence_basis.fact_ids) - facts:
                raise service.MarketingError("plan_evidence_invalid", "表达的证据依据引用了不存在的事实。",
                                             fields=_fields(f"angles.{ai}.expressions.{ei}.evidence_basis.fact_ids"))
    duplicates = [f"angles.{ai}.expressions.{ei}.visual_mechanism"
                  for ai, _, members in indistinct_expressions(value) for ei, _ in members]
    if duplicates:
        raise service.MarketingError("expressions_not_distinct",
                                     "同一角度下的表达画面机制重复；请补充不同的讲法与画面机制。", fields=_fields(*duplicates))
    for ci, combo in enumerate(value.combinations):
        if not all(script_matches(text, value.ad_language) for text in (combo.headline, combo.body_copy)):
            raise service.MarketingError("copy_language_mismatch",
                                         "Ad headline and body copy must use the selected advertising language.",
                                         fields=_fields(f"combinations.{ci}.headline", f"combinations.{ci}.body_copy"))
        if not english_brief_matches(combo):
            raise service.MarketingError("image_brief_language_mismatch",
                                         "Write the English image brief for every ad.", fields=_fields(f"combinations.{ci}.image_brief"))
    if not english_text(value.style_notes):
        raise service.MarketingError("image_brief_language_mismatch", "Write the visual style notes in English for the image model.",
                                     fields=_fields("style_notes"))
    brand = inputs.get("brand_kit")
    return value.model_copy(update={"market": row.market,
                                    "brand_kit": BrandKitRef.model_validate(brand["ref"]) if brand else None})


EXAMPLE_CLAIMS = (
    (("Understand the product value", "清楚理解产品价值"),
     (("Usage demonstration", "使用情境演示", "live_connection_demo",
       "Show one real use step by step so the benefit is observed, not asserted.", "逐步展示一次真实使用，让好处被看见而不是被宣称。"),
      ("Problem and solution", "问题与解决过程", "scenario_story",
       "Tell a short situation where the problem appears and the product resolves it.", "讲一个问题出现、产品解决问题的具体情境。"))),
    (("Address purchase concerns", "减少购买顾虑"),
     (("Questions and answers", "疑问问答", "qa_objection",
       "Ask the buyer's hesitation as a question and answer it with documented facts.", "把购买前的疑虑写成问题，用资料中的事实回答。"),
      ("Product details", "产品细节说明", "feature_closeup",
       "Zoom into the parts that deliver the benefit and label each with one documented fact.", "放大实现好处的部位，每处只标注一条有依据的事实。"))),
    (("Know where it fits", "知道适合哪里"),
     (("Usage boundaries", "适用边界解读", "fit_guide",
       "Show who and which situations it suits, and where it does not.", "说明适合谁、适合什么情境，以及不适合的情况。"),
      ("Everyday situation", "日常情境", "scenario_story",
       "Follow one ordinary day in which the product is simply there when needed.", "跟随普通的一天，产品在需要时就在那里。"))),
)
EXAMPLE_COPY = {"en": ("Explore the product", "Explore actual uses and suitable conditions."),
                "zh": ("了解产品用途", "了解产品的实际用途与适用条件。"),
                "ja": ("製品を知る", "製品の用途と適切な使用条件をご確認ください。"),
                "ko": ("제품을 알아보세요", "제품의 용도와 적절한 사용 조건을 확인하세요.")}


def example(inputs):
    """Controlled Mock ideas: the first batch's fixed shape from the drafted understanding, never a fallback for paid AI."""
    from .planner import EXAMPLE_STYLE
    zh = inputs.get("strategy_language", "en-US") == "zh-CN"
    ad_language = inputs.get("ad_language", "en-US")
    headline, body = EXAMPLE_COPY.get(ad_language.split("-")[0].lower(), EXAMPLE_COPY["en"])
    fact_ids = [f["id"] for f in inputs["understanding"]["facts"]][:3]
    gap = "" if fact_ids else ("资料中还没有可引用的事实。" if zh else "The source has no fact to cite yet.")
    angles, combinations = [], []
    for n, ((claim_en, claim_zh), tellings) in enumerate(EXAMPLE_CLAIMS, 1):
        claim = claim_zh if zh else claim_en
        angle = {"id": f"a{n}", "proposition": claim, "fact_ids": fact_ids, "expressions": [],
                 "rationale": "仅用于验证流程的示例假设。" if zh else "An unvalidated hypothesis for the controlled workflow example."}
        for j, (name_en, name_zh, mechanism, telling_en, telling_zh) in enumerate(tellings, 1):
            eid = f"e{n}_{j}"
            angle["expressions"].append({"id": eid, "name": name_zh if zh else name_en,
                "description": f"用{name_zh}讲清主张。" if zh else f"Explain the proposition through {name_en.lower()}.",
                "fit_reason": "示例建议，需核对产品适用性。" if zh else "A proposed way to explain this claim; confirm product applicability.",
                "how_to_tell": telling_zh if zh else telling_en, "visual_mechanism": mechanism,
                "evidence_basis": {"fact_ids": fact_ids, "gap": gap}})
            combinations.append({"id": f"c{n}_{j}", "angle_id": angle["id"], "expression_id": eid,
                "elements": {"scene": "Actual product usage environment"}, "headline": headline, "body_copy": body,
                "visual_brief": f"Show the product clearly using {name_en.lower()}, without unsupported claims.",
                "image_brief": {"proposition": claim_en, "expression": name_en, "hold_constant": ["Same product identity"],
                                "checks": ["Accurate product appearance", "No unsupported claims"]},
                "checks": ["产品外观与资料一致"] if zh else ["Product appearance matches the supplied facts"]})
        angles.append(angle)
    return {"market": inputs.get("market") or "", "ad_language": ad_language,
            "strategy_language": inputs.get("strategy_language", "en-US"), "angles": angles,
            "combinations": combinations, "style_notes": EXAMPLE_STYLE}
