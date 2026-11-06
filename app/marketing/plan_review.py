"""Deterministic plan checks before paid generation (spec §4.1–§4.2).

AI findings and guiding questions are planner output; ``system`` findings are
recomputed from the plan on every write, so neither a planner nor a client
can forge, resolve or keep a stale deterministic result.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal

from pydantic import ValidationError

from .contracts import COMPETITOR_KINDS, GuidingQuestion, ReviewItem
from .expression_catalog import normalize_mechanism
from .workflow_contracts import EvaluationProtocol

DETAILS = ("how_to_tell", "visual_mechanism", "evidence_basis")
# Server bookkeeping in the stored plan, never sent to clients or frozen into a round:
# per combination, the fingerprint of the expression its English image brief was written for (#292).
BRIEF_BASIS = "image_brief_basis"
BRIEF_EXPRESSION = ("name", "how_to_tell", "visual_mechanism")
PENDING = {"landing_page": {"en-US": "Landing page pending confirmation", "zh-CN": "待确认落地页"},
           "offer": {"en-US": "Offer pending confirmation", "zh-CN": "待确认销售方案"}}
URL = re.compile(r"(?:https?://|www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s\"'<>，。；）)]*)?", re.I)
OFFER_TERMS = re.compile(
    r"[$€£¥￥]\s?\d|\d\s?(?:usd|eur|gbp|cad|aud|jpy|cny|rmb|美元|欧元|英镑|元)|\d\s?%\s?off\b|"
    r"\b(?:prices?|priced|pricing|discounts?|coupons?|promo(?:tion)?s?|sale|free (?:shipping|delivery)|bundles?|installments?|financing)\b|"
    r"价格|售价|优惠|折扣|打折|包邮|免运费|满减|赠品|套装|分期|特价|促销", re.I)
KEY_QUESTIONS = {
    "landing_page": ("system-landing-page", False, {
        "en-US": ("The product information gives no landing page. Where should these ads send people?",
            ["Product page on our own website", "Marketplace listing (for example Amazon)",
             "A dedicated campaign landing page", "No landing page yet; hold delivery until one is ready"],
            "The landing page is held constant across combinations and recorded with the results, so the plan cannot invent one. "
            "Add the URL as a custom answer or in the Landing page field."),
        "zh-CN": ("产品资料没有提供落地页。广告要把人带到哪里？",
            ["自有网站的产品页", "电商平台商品页（例如 Amazon）", "专门的活动落地页", "暂无落地页，准备好之前不投放"],
            "落地页在各组合间保持不变并随结果记录，方案不能自行编造。请在自定义回答或落地页字段中填写链接。")}),
    "offer": ("system-offer", True, {
        "en-US": ("The product information gives no Offer (price, discount or shipping terms). Which Offer applies during this test?",
            ["Regular price, no promotion", "A discount or coupon", "Free or discounted shipping",
             "A bundle or gift with purchase", "Not decided yet; confirm before delivery"],
            "The Offer is held constant across combinations and affects whether results compare; ad copy must not promise terms "
            "the source does not confirm. Describe the exact terms as a custom answer or in the Offer field."),
        "zh-CN": ("产品资料没有提供 Offer（价格、折扣或配送条款）。本次测试使用哪种 Offer？",
            ["原价，无促销", "折扣或优惠券", "免运费或运费优惠", "套装或赠品", "尚未决定，投放前确认"],
            "Offer 在各组合间保持不变并影响结果能否比较；文案不能承诺资料未确认的条款。请在自定义回答或 Offer 字段中写明具体条款。")}),
}


def indistinct_expressions(plan):
    """Per angle, the expressions whose normalized visual mechanisms coincide."""
    found = []
    for ai, angle in enumerate(plan.angles):
        same = {}
        for ei, expression in enumerate(angle.expressions):
            if expression.visual_mechanism:
                same.setdefault(normalize_mechanism(expression.visual_mechanism), []).append((ei, expression))
        found.extend((ai, angle, members) for members in same.values() if len(members) > 1)
    return found


def duplicate_fields(plan):
    return [f"body.plan.angles.{ai}.expressions.{ei}.visual_mechanism"
            for ai, _, members in indistinct_expressions(plan) for ei, _ in members]


def declared_gaps(plan):
    """Per angle, the expressions that state an evidence gap; a shared expression is listed once."""
    seen, found = set(), []
    for angle in plan.angles:
        members = [e for e in angle.expressions if e.evidence_basis and e.evidence_basis.gap and e.id not in seen]
        seen.update(e.id for e in members)
        if members:
            found.append((angle, members))
    return found


def budget_shortfall(plan):
    """Per-group budget against the spend the default evaluation protocol needs before any verdict."""
    protocol = EvaluationProtocol()
    need = protocol.min_spend
    if plan.rules.business_metric == "cpa":
        need = max(need, protocol.min_purchases * plan.rules.threshold)
    each = plan.small_round_budget / len(plan.groups)
    return (each, need, protocol.min_purchases) if each < need else None


def _planned(plan, combo):
    angle = next(a for a in plan.angles if a.id == combo.angle_id)
    return angle, next(e for e in angle.expressions if e.id == combo.expression_id)


def expression_fingerprint(expression):
    """What an English image brief is written for: the expression's name, way of telling and visual mechanism."""
    value = {key: getattr(expression, key) for key in BRIEF_EXPRESSION}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def brief_basis(plan, stored=None):
    """Per combination, the fingerprint of the expression its English image brief was last written for.

    A planner draft (no ``stored``) writes every brief. A save that adds or edits a combination's brief
    (``image_brief`` or ``visual_brief``) records the current expression; an unchanged brief keeps its
    record. A plan stored before #292 has no record, so it is never blocked retroactively."""
    stored = stored or {}
    kept = stored.get(BRIEF_BASIS, {})
    before = {c["id"]: (c.get("image_brief"), c.get("visual_brief")) for c in stored.get("combinations", [])}
    basis = {}
    for combo in plan.combinations:
        if combo.image_brief is None:
            continue
        if before.get(combo.id) != (combo.image_brief.model_dump(mode="json"), combo.visual_brief):
            basis[combo.id] = expression_fingerprint(_planned(plan, combo)[1])
        elif combo.id in kept:
            basis[combo.id] = kept[combo.id]
    return basis


def stale_briefs(plan, basis):
    """The combinations whose English image brief was written for an earlier version of their expression."""
    found = []
    for ci, combo in enumerate(plan.combinations):
        angle, expression = _planned(plan, combo)
        if combo.image_brief and combo.id in basis and basis[combo.id] != expression_fingerprint(expression):
            found.append((ci, combo, angle, expression))
    return found


def stale_fields(plan, basis):
    return [f"body.plan.combinations.{ci}.image_brief.expression" for ci, *_ in stale_briefs(plan, basis or {})]


def stored_plan(plan, basis):
    from .competitor_review import compact
    return {**compact(plan.model_dump(mode="json")), **({BRIEF_BASIS: basis} if basis else {})}


def plan_document(plan_json):
    """The stored plan without server bookkeeping: what clients read, rounds freeze and plan_hash covers."""
    if not plan_json or BRIEF_BASIS not in plan_json:
        return plan_json
    return {key: value for key, value in plan_json.items() if key != BRIEF_BASIS}


def _amount(value):
    return format(Decimal(value).quantize(Decimal("0.01")).normalize(), "f")


def with_system_checks(plan, basis=None, citation=None):
    """Replace system findings with the current result. They follow AI items so client indexes stay stable.

    Competitor findings (``citation``: the cited snapshot, #273) come last, so a projection for clients
    predating them drops them without moving any other item's index."""
    zh = plan.strategy_language == "zh-CN"
    system = []
    for n, (_, angle, members) in enumerate(indistinct_expressions(plan), 1):
        names = ("、".join(f"「{e.name[:80]}」" for _, e in members) if zh
                 else ", ".join(f'"{e.name[:80]}"' for _, e in members))
        mechanism, proposition = members[0][1].visual_mechanism[:120], angle.proposition[:120]
        system.append(ReviewItem(id=f"system-expression-duplicate-{n}", kind="expression_duplicate",
            severity="blocking", source="system", target_ids=[angle.id, *(e.id for _, e in members)],
            finding=(f"角度「{proposition}」下 {names} 的画面机制相同（{mechanism}）；仅换词、换形容词或卖点顺序不算不同表达。" if zh
                     else f'Under "{proposition}", {names} share the visual mechanism {mechanism}; rewording, adjectives or claim order alone do not make a different expression.'),
            suggestion=("为其中一个表达补充不同的讲法与画面机制，或合并为同一表达。" if zh
                        else "Give one of them a different way of telling and visual mechanism, or merge them into one expression.")))
    for n, (_, combo, angle, expression) in enumerate(stale_briefs(plan, basis or {}), 1):
        combination, written = f"{angle.proposition[:120]} / {expression.name[:80]}", combo.image_brief.expression[:120]
        system.append(ReviewItem(id=f"system-image-brief-stale-{n}", kind="image_brief_stale", severity="blocking",
            source="system", target_ids=[combo.id, expression.id],
            finding=(f"组合「{combination}」的英文画面要求写于表达方式「{expression.name[:80]}」最近一次修改之前，仍按「{written}」描述画面；"
                     "生图模型和 Meta 广告名称都读取这段英文画面要求，图片会沿用旧的表达方式。" if zh
                     else f'The English image brief of the combination "{combination}" was written before the expression "{expression.name[:80]}" '
                     f'last changed and still describes "{written}". The image model and the Meta ad name read this brief, so images would keep the old expression.'),
            suggestion=("按当前表达方式改写该组合的英文画面要求（表达方式（英文））和画面要求。" if zh
                        else "Rewrite this combination's English image brief (Expression in English) and visual brief for the current expression.")))
    for n, (angle, members) in enumerate(declared_gaps(plan), 1):
        proposition = angle.proposition[:120]
        gaps = ("；".join(f"「{e.name[:80]}」：{e.evidence_basis.gap[:200]}" for e in members) if zh
                else "; ".join(f'"{e.name[:80]}": {e.evidence_basis.gap[:200]}' for e in members))
        system.append(ReviewItem(id=f"system-evidence-gap-{n}", kind="evidence_gap", severity="advisory",
            source="system", target_ids=[angle.id, *(e.id for e in members)][:10],
            finding=(f"角度「{proposition}」下这些表达写明了证据缺口：{gaps}。" if zh
                     else f'Under "{proposition}", these expressions state an evidence gap: {gaps}.')[:2000],
            suggestion=("生成前补充或核对这些证据；在此之前，文案和画面不要把它写成事实，并把这些表达作为探索。" if zh
                        else "Supply or verify this evidence before generation; until then keep copy and visuals from presenting it as fact and treat these expressions as exploratory.")))
    shortfall = budget_shortfall(plan)
    if shortfall:
        each, need, purchases = map(_amount, shortfall)
        budget, groups, currency, cpa = _amount(plan.small_round_budget), len(plan.groups), plan.currency, plan.rules.business_metric == "cpa"
        threshold = _amount(plan.rules.threshold)
        system.append(ReviewItem(id="system-budget-insufficient", kind="budget_insufficient", severity="advisory",
            source="system", target_ids=[g.id for g in plan.groups][:10],
            finding=(f"小轮预算 {budget} {currency} 平均分给 {groups} 个测试组，每组约 {each} {currency}；按默认评估规则，每组至少需要 {need} {currency}"
                     + (f"（{purchases} 次购买 × 目标 CPA {threshold} {currency}）" if cpa else "的花费") + "才可能得出业务结论。" if zh
                     else f"The small-round budget of {budget} {currency} split evenly across {groups} test groups gives about {each} {currency} per group; "
                     f"under the default evaluation protocol a group needs at least {need} {currency}"
                     + (f" ({purchases} purchases at the target CPA of {threshold} {currency})" if cpa else " of spend")
                     + " before a business verdict is possible."),
            suggestion=("提高小轮预算、减少本轮比较的测试组，或在局限中说明本轮只做探索、不下业务结论。" if zh
                        else "Raise the small-round budget, compare fewer groups this round, or state in the limitations that this round is exploratory and will not reach a business verdict.")))
    from .competitor_review import review_items
    system += review_items(plan, citation)
    # Competitor kinds are the server's to compute from the snapshot; observations belong to system items only.
    ai = [r.model_copy(update={"observations": []}) for r in plan.plan_review
          if r.source != "system" and r.kind not in COMPETITOR_KINDS]
    return plan.model_copy(update={"plan_review": ai[:max(0, 20 - len(system))] + system[:20]})


def from_planner(plan):
    """A planner raises findings and questions; only the user resolves or answers them.

    Its style fields are written the way its style_variation means (#354)."""
    plan = with_style_variation(plan)
    return plan.model_copy(update={
        "plan_review": [r.model_copy(update={"resolved": False, "observations": []}) for r in plan.plan_review
                        if r.source == "ai" and r.kind not in COMPETITOR_KINDS],
        "guiding_questions": [q.model_copy(update={"selected": [], "custom_answer": ""}) for q in plan.guiding_questions]})


def _supplied(inputs):
    """What the user gave the planner: the source, earlier rounds' conditions and answers, never the model's own text."""
    return "\n".join([inputs.get("source_text", ""),
        *(str(r.get("conditions", {}).get(key, "")) for r in inputs.get("prior_rounds", []) for key in ("offer", "landing_page")),
        *(" ".join([*a.get("selected", []), a.get("custom_answer", "")]) for a in inputs.get("guiding_answers", []))])


def _known_url(value, supplied):
    supplied = supplied.casefold()
    return any(url.rstrip(".,;:!?/").casefold() in supplied for url in URL.findall(value))


def with_key_conditions(plan, inputs):
    """A paid draft may not invent the landing page or Offer the user never supplied (#239).

    Each unsupplied one is written as pending and asked first; AI questions keep the remaining slots."""
    language = "zh-CN" if plan.strategy_language == "zh-CN" else "en-US"
    supplied = _supplied(inputs)
    answered = {a.get("id") for a in inputs.get("guiding_answers", [])}
    pending = [key for key, unknown in (
        ("landing_page", not _known_url(plan.landing_page, supplied)),
        ("offer", not OFFER_TERMS.search(supplied) or plan.offer.casefold() == PENDING["offer"][language].casefold()))
        if unknown and KEY_QUESTIONS[key][0] not in answered]
    questions = []
    for key in pending:
        qid, multiple, texts = KEY_QUESTIONS[key]
        question, options, why = texts[language]
        questions.append(GuidingQuestion(id=qid, question=question, options=options, multiple=multiple, why_needed=why))
    reserved = {qid for qid, _, _ in KEY_QUESTIONS.values()}
    ai = [q for q in plan.guiding_questions if q.id not in reserved]
    return plan.model_copy(update={**{key: PENDING[key][language] for key in pending},
                                   "guiding_questions": (questions + ai)[:3]})


STYLE_LABELS = ("视觉风格", "visual style")
# The label a draft gains, in its strategy language: the web vocabulary's plan-label-visual-style.
STYLE_LABEL = {"zh-CN": "视觉风格", "en-US": "Visual style"}
STYLE_FIELDS = ("style_variation", "style_notes", "brand_kit")


def _style_items(items):
    return [i for i, text in enumerate(items) if any(label in text.casefold() for label in STYLE_LABELS)]


def with_style_variation(plan):
    """A planner draft's declared comparison, written as its style_variation means (spec §6.1, #354).

    The server's counterpart of the screen's withStyleVariation: held lists visual style as a held
    condition, never as a change, and clears every group's style; tested lists it as the change and
    keeps each group's style, never writing a missing one. Only label items move, so the model's other
    wording stays. A specific-factor test of another factor, or a result the plan contract refuses
    (style was the only change, a full list), stays as drafted for the user to resolve at confirmation."""
    label = STYLE_LABEL[plan.strategy_language]
    changed, held = _style_items(plan.changed), _style_items(plan.held_constant)
    document = plan.model_dump()
    if plan.style_variation == "held":
        document["changed"] = [text for i, text in enumerate(plan.changed) if i not in changed]
        document["held_constant"] = plan.held_constant if held else [*plan.held_constant, label]
        for group in document["groups"]:
            group["style"] = ""
    elif plan.comparison_kind == "specific_factor" and not changed:
        return plan
    else:
        document["held_constant"] = [text for i, text in enumerate(plan.held_constant) if i not in held]
        document["changed"] = plan.changed if changed else [*plan.changed, label]
    try:
        return type(plan).model_validate(document)
    except ValidationError:
        return plan


def style_violation(plan):
    """The first conflict between style_variation and the declared comparison, as (code, fields), or None.

    held lists visual style as a held condition shared by every group; tested lists it as the
    active change and writes each group's style. A specific-factor test never also tests style."""
    changed, held = _style_items(plan.changed), _style_items(plan.held_constant)
    if not plan.style_notes.strip():
        return "style_notes_required", ["body.plan.style_notes"]
    if plan.style_variation == "held":
        if not held or changed:
            return "style_not_held", ["body.plan.held_constant", *(f"body.plan.changed.{i}" for i in changed)]
        styled = [f"body.plan.groups.{i}.style" for i, g in enumerate(plan.groups) if g.style.strip()]
        return ("group_style_not_held", styled) if styled else None
    if plan.comparison_kind == "specific_factor" and not changed:
        return "style_variation_conflicts_factor", ["body.plan.style_variation", "body.plan.changed.0"]
    if not changed or held:
        return "style_variation_undeclared", ["body.plan.changed", *(f"body.plan.held_constant.{i}" for i in held)]
    missing = [f"body.plan.groups.{i}.style" for i, g in enumerate(plan.groups) if not g.style.strip()]
    return ("group_style_required", missing) if missing else None


def with_unsent_style(plan, stored):
    """A client predating the brand baseline neither reads nor sends its fields; keep the stored values."""
    if not stored:
        return plan
    kept = {key: stored[key] for key in STYLE_FIELDS if key not in plan.model_fields_set and key in stored}
    styles = {g["id"]: g["style"] for g in stored.get("groups", []) if "style" in g}
    unsent = [g for g in plan.groups if "style" not in g.model_fields_set and g.id in styles]
    if not kept and not unsent:
        return plan
    document = plan.model_dump(mode="json")
    for group in document["groups"]:
        if any(g.id == group["id"] for g in unsent):
            group["style"] = styles[group["id"]]
    return type(plan).model_validate({**document, **kept})


def legacy_style_projection(plan):
    """Retain the pre-brand-baseline wire shape for deployed strict clients."""
    if plan is None:
        return None
    result = deepcopy(plan)
    for key in STYLE_FIELDS:
        result.pop(key, None)
    for group in result.get("groups", []):
        group.pop("style", None)
    return result


def unresolved_fields(plan):
    return ([f"body.plan.plan_review.{i}" for i, r in enumerate(plan.plan_review) if r.severity == "blocking" and not r.resolved]
            + [f"body.plan.guiding_questions.{i}" for i, q in enumerate(plan.guiding_questions) if not q.answered])


def details_missing(plan_json):
    return any(not expression.get(key) for angle in plan_json["angles"]
               for expression in angle["expressions"] for key in DETAILS)


def guiding_answers(plan_json):
    """The user's answers on the stored plan, for an explicit re-draft."""
    return [{"id": q["id"], "question": q["question"], "selected": q.get("selected", []), "custom_answer": q.get("custom_answer", "")}
            for q in (plan_json or {}).get("guiding_questions", []) if q.get("selected") or q.get("custom_answer")]


def legacy_review_projection(plan):
    """Retain the pre-review wire shape for deployed strict clients."""
    if plan is None:
        return None
    result = deepcopy(plan)
    result.pop("plan_review", None)
    result.pop("guiding_questions", None)
    for angle in result.get("angles", []):
        for expression in angle.get("expressions", []):
            for key in DETAILS:
                expression.pop(key, None)
    return result
