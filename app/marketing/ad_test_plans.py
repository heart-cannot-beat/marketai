"""The plan of a test of kept ads, written by template, and what such a test can show (ADR 0019 §2, #248).

Design §5.2 and §5.6. No AI call writes it: what the round compares follows the kept ads, the three answers
and the defaults the user changed go into the plan as they are, and the text items come from the two
templates below, in the ideas' strategy language. The plan must pass every check a confirmed plan-first plan
passes (``TestPlan`` and ``service.validate_plan(confirming=True)``); a plan they refuse is refused, never
loosened.

- Kept ads across two or more claims compare claims (``screen_angles``), one group per claim.
- Kept ads of one claim told two or more ways compare the ways of telling (``match_expressions``), one group each.
- One kept ad, or versions of one ad, are only checked against the goal (``confirm_combination`` with
  ``business_retest``; maintainer's decision on #248).

The combinations are the ideas' own, unchanged: the round freezes each kept ad with the combination its image was
made from (plan E4). Comparing claims, a claim kept with only one way of telling it makes the plan
``limited_expression_coverage`` and adds a limitation that names the claim (maintainer's decision on #248).

The numbers -- the budget each compared object gets, the orders that buys at the target cost, the three tiers and
the day of the result -- come from ``EvaluationProtocol()``'s defaults, the protocol the round freezes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal
from types import SimpleNamespace
from typing import Literal

from . import service
from .contracts import TestPlan, Understanding
from .ideas_contracts import Ideas
from .plan_review import STYLE_LABEL

#: Bumped whenever what a quote covers or how its plan is written changes: an old quote then no longer starts a test.
QUOTE_VERSION = "marketing-test-quote-v1"
Comparison = Literal["claims", "expressions", "single"]
STAGES = {"claims": "screen_angles", "expressions": "match_expressions", "single": "confirm_combination"}
#: The defaults the user can change (design §5.2): the whole test is at most three rounds and three times the
#: first round's budget, in USD; the ad account's time zone is the browser's, else UTC.
MAX_SMALL_ROUNDS = 3
CURRENCY = "USD"
ATTRIBUTION_WINDOW = "7d_click_1d_view"
ACCOUNT_TIMEZONE = "UTC"
CENT = Decimal("0.01")


@dataclass(frozen=True)
class KeptAd:
    """One kept ad, as a test takes it: the image, the combination it was made from and that intent's hash."""
    asset_version_id: str
    combination_id: str
    angle_id: str
    expression_id: str
    intent_hash: str
    execution_mode: Literal["byok", "mock"]


@dataclass(frozen=True)
class Conditions:
    """The delivery conditions after the defaults: ``None`` keeps the template's default text."""
    audience: str
    landing_page: str
    offer: str | None = None
    placement: str | None = None
    currency: str = CURRENCY
    max_small_rounds: int = MAX_SMALL_ROUNDS
    media_budget: Decimal | None = None


TEXT = {
    "en-US": {
        "question": {
            "claims": "Which of these {n} buying claims brings orders at a cost per order of {target} {currency} or less?",
            "expressions": 'Which of these {n} ways of telling "{claim}" brings orders at a cost per order of {target} {currency} or less?',
            "single": "Does this ad bring orders at a cost per order of {target} {currency} or less?",
        },
        "changed": {
            "claims": "The buying claim each group of ads makes",
            "expressions": "The way each group of ads tells the same buying claim",
            "single": "Nothing is compared: this round checks one ad against the goal",
        },
        "allowed": {
            "claims": "Headline, body copy and picture follow each group's claim and its way of telling it",
            "expressions": "Headline, body copy and picture follow each group's way of telling the claim",
        },
        "held": ["Product", "Audience", "Placements", "Landing page", "Offer"],
        "held_split": "Budget split evenly across the groups",
        "subject": {"claims": "claim", "expressions": "way of telling", "single": "ad"},
        "scope": "The result covers these {n} ads only; ads that were not in this test stay unknown.",
        "coverage": 'The claim "{claim}" is tried with one way of telling it only; if it misses the goal, that does not show the claim itself does not work.',
        "tier": {
            "direction": "At the target cost the budget buys about {orders} orders per {subject}, fewer than the {need} a verdict needs: this round can show a direction only.",
            "rough": "At the target cost the budget buys about {orders} orders per {subject}, under half the {need} a verdict needs: this round gives a rough read only.",
        },
        "placement": "Meta Advantage+ placements (automatic)",
        "offer": "No promotion (regular price)",
        "baseline": "No baseline: each group is judged against the business goal (cost per order).",
        "allocation": {"split": "Equal budget for each group", "single": "The whole round budget goes to this ad"},
        "goal": "Orders at a cost per order of {target} {currency} or less.",
        "evidence": ("Before a verdict each {subject} needs at least {purchases} purchases, {clicks} clicks, {impressions} "
                     "impressions and {spend} {currency} of spend over at least {days} full days."),
        "schedule": "Run for {days} days from the start date, then wait {lag} days for attribution; the result is read the day after.",
        "stop": "Stop when this round's budget is spent or its {days} days end; no winner is called before the result.",
        "group": {"claims": "The kept ads that make this claim.", "expressions": "The kept ads that tell the claim this way.",
                  "single": "The kept ad checked against the goal."},
        "style": "Hold the visual look the kept ads were made with; nothing about the style is tested.",
    },
    "zh-CN": {
        "question": {
            "claims": "这 {n} 个主张里，哪个能以每单不高于 {target} {currency} 的成本带来订单？",
            "expressions": "主张“{claim}”的这 {n} 种讲法里，哪种能以每单不高于 {target} {currency} 的成本带来订单？",
            "single": "这张广告能不能以每单不高于 {target} {currency} 的成本带来订单？",
        },
        "changed": {
            "claims": "每组广告讲的主张",
            "expressions": "每组广告讲同一个主张的方式",
            "single": "这一轮不比较不同的广告，只看这张广告是否达标",
        },
        "allowed": {
            "claims": "标题、正文和画面跟着各组的主张和讲法变",
            "expressions": "标题、正文和画面跟着各组的讲法变",
        },
        "held": ["产品", "受众", "版位", "落地页", "优惠"],
        "held_split": "各组平均分配预算",
        "subject": {"claims": "主张", "expressions": "讲法", "single": "广告"},
        "scope": "结论只适用于这 {n} 张广告；没放进这次测试的广告仍是未知。",
        "coverage": "主张“{claim}”只试了一种讲法，没达标也不能说明这个主张不行。",
        "tier": {
            "direction": "按目标成本，这笔预算每个{subject}约能带来 {orders} 单，不到下结论需要的 {need} 单：这一轮只能看出方向。",
            "rough": "按目标成本，这笔预算每个{subject}约能带来 {orders} 单，不到下结论需要的 {need} 单的一半：这一轮只能看个大概。",
        },
        "placement": "Meta 自动版位（Advantage+ 版位）",
        "offer": "无优惠（原价）",
        "baseline": "没有基准：每组按业务目标（每单成本）判断。",
        "allocation": {"split": "各组平均分配预算", "single": "这一轮的预算全部给这张广告"},
        "goal": "每单成本不高于 {target} {currency}。",
        "evidence": "下结论之前，每个{subject}至少要有 {purchases} 单、{clicks} 次点击、{impressions} 次展示、{spend} {currency} 花费，并投满 {days} 天。",
        "schedule": "从开始日期起投放 {days} 天，之后等 {lag} 天归因期，第二天出结论。",
        "stop": "这一轮的预算花完或 {days} 天投满就停；出结论之前不判胜负。",
        "group": {"claims": "讲这个主张的留下的广告。", "expressions": "用这种讲法讲主张的留下的广告。",
                  "single": "要看是否达标的这张广告。"},
        "style": "Hold the visual look the kept ads were made with; nothing about the style is tested.",
    },
}


def money(value: Decimal) -> str:
    return format(Decimal(value).quantize(CENT), "f")


def comparison_of(kept_ads) -> Comparison:
    """Claims when the kept ads make two or more claims; ways of telling when one claim is told two or more
    ways; otherwise one ad (or versions of it) checked against the goal."""
    if len({ad.angle_id for ad in kept_ads}) > 1:
        return "claims"
    if len({(ad.angle_id, ad.expression_id) for ad in kept_ads}) > 1:
        return "expressions"
    return "single"


def subjects(doc: Ideas, kept_ads, comparison: Comparison) -> list[dict]:
    """The compared objects, in the ideas' order: one per claim, per way of telling, or the one ad.

    Each names its claim (and way of telling), its combinations and its kept images, oldest kept first."""
    order = {c.id: n for n, c in enumerate(doc.combinations)}
    angles = {a.id: a for a in doc.angles}
    found = {}
    for ad in sorted(kept_ads, key=lambda ad: order.get(ad.combination_id, len(order))):
        key = ad.angle_id if comparison == "claims" else (ad.angle_id, ad.expression_id) if comparison == "expressions" else ""
        item = found.setdefault(key, {"kind": {"claims": "claim", "expressions": "expression", "single": "ad"}[comparison],
                                      "angle_id": ad.angle_id,
                                      "expression_id": None if comparison == "claims" else ad.expression_id,
                                      "combination_ids": [], "asset_version_ids": []})
        if ad.combination_id not in item["combination_ids"]:
            item["combination_ids"].append(ad.combination_id)
        item["asset_version_ids"].append(ad.asset_version_id)
    for item in found.values():
        angle = angles[item["angle_id"]]
        expression = next((e for e in angle.expressions if e.id == item["expression_id"]), None)
        item["title"] = expression.name if comparison == "expressions" and expression else angle.proposition
    return list(found.values())


def question(doc: Ideas, comparison: Comparison, items: list[dict], target: Decimal, currency: str) -> str:
    """The round's question, in the ideas' strategy language: what it compares and the goal."""
    claim = next(a.proposition for a in doc.angles if a.id == items[0]["angle_id"])
    return TEXT[doc.strategy_language]["question"][comparison].format(
        n=len(items), claim=claim[:300], target=money(target), currency=currency)


def estimate(count: int, round_budget: Decimal, target_cost: Decimal, protocol) -> dict:
    """What ``count`` compared objects sharing ``round_budget`` can show at exactly the target cost (design §5.6).

    Each gets an equal share; the orders it buys decide the tier: at least ``min_purchases`` is enough for a
    verdict, at least half of it shows a direction, less gives a rough read only. Short of enough, the money a
    verdict needs, and how many objects this budget would carry to enough (``None`` when not fewer than now, or
    not even one)."""
    need = protocol.min_purchases
    exact = round_budget / count / target_cost
    tier = "enough" if exact >= need else "direction" if exact * 2 >= need else "rough"
    fits = int((round_budget / (need * target_cost)).to_integral_value(rounding=ROUND_FLOOR))
    return {"budget": format((round_budget / count).quantize(CENT, rounding=ROUND_DOWN), "f"),
            "expected_orders": int(exact.to_integral_value(rounding=ROUND_FLOOR)), "tier": tier,
            "needed_budget": money(count * need * target_cost),
            "suggested_subjects": fits if tier != "enough" and 1 <= fits < count else None}


def schedule(start: date, duration_days: int, protocol) -> tuple[date, date, date]:
    """Start, last day and the day of the result (plan E9): the round matures once its attribution wait has
    passed after the last day (``today > end_date + attribution_lag_days``), so a 7-day test has its result on
    the sixteenth day counted from the start."""
    end = start + timedelta(days=duration_days - 1)
    return start, end, end + timedelta(days=protocol.attribution_lag_days + 1)


def understanding(ideas_row) -> dict:
    """The test's product understanding: the ideas' one in the plan-first shape, without the claims the AI read
    with no source (plan E1)."""
    document = {key: value for key, value in (ideas_row.understanding_json or {}).items() if key != "unsupported_claims"}
    return Understanding.model_validate(document).model_dump(mode="json")


def _group_ids(doc: Ideas, count: int) -> list[str]:
    taken = {a.id for a in doc.angles} | {e.id for a in doc.angles for e in a.expressions} | {c.id for c in doc.combinations}
    ids = []
    for n in range(1, count + 1):
        group_id = f"group_{n}"
        while group_id in taken:
            group_id = f"test_{group_id}"
        ids.append(group_id)
    return ids


def build_plan(ideas_row, kept_ads, answers, conditions: Conditions, protocol=None) -> TestPlan:
    """The whole plan of a test of ``kept_ads`` of ``ideas_row``; ``pydantic.ValidationError`` when ``TestPlan``
    refuses it. ``answers`` is a ``MarketingTestAnswers``; ``conditions`` the defaults after the user's changes."""
    from .workflow_contracts import EvaluationProtocol
    protocol = protocol or EvaluationProtocol()
    doc = Ideas.model_validate(ideas_row.ideas_json)
    text = TEXT[doc.strategy_language]
    comparison = comparison_of(kept_ads)
    items = subjects(doc, kept_ads, comparison)
    currency = conditions.currency
    used = {cid for item in items for cid in item["combination_ids"]}
    combos = [c for c in doc.combinations if c.id in used]
    pairs = {(c.angle_id, c.expression_id) for c in combos}
    angles = []
    for angle in doc.angles:
        expressions = [e for e in angle.expressions if (angle.id, e.id) in pairs]
        if expressions:
            angles.append({**angle.model_dump(mode="json"), "competitor_overlap": [],
                           "expressions": [e.model_dump(mode="json") for e in expressions]})
    # Comparing claims, a claim tried one way only is the plan's stated limit (#248).
    limited = [a for a in angles if len(a["expressions"]) < 2] if comparison == "claims" else []
    subject = text["subject"][comparison]
    share = estimate(len(items), answers.round_budget, answers.target_cost, protocol)
    limitations = [text["scope"].format(n=len(kept_ads)),
                   *(text["coverage"].format(claim=a["proposition"][:300]) for a in limited)]
    if share["tier"] != "enough":
        limitations.append(text["tier"][share["tier"]].format(orders=share["expected_orders"], subject=subject,
                                                               need=protocol.min_purchases))
    target = money(answers.target_cost)
    split = len(items) > 1
    document = {
        "stage": STAGES[comparison], "question": question(doc, comparison, items, answers.target_cost, currency),
        "comparison_kind": "business_retest" if comparison == "single" else "whole_combinations",
        "changed": [text["changed"][comparison]],
        "allowed_changes": [text["allowed"][comparison]] if comparison in text["allowed"] else [],
        # Visual style is held: every kept ad was made with the ideas' one style (``ideas.ideas_intent``).
        "held_constant": [*text["held"], STYLE_LABEL[doc.strategy_language], *([text["held_split"]] if split else [])],
        "limitations": limitations, "limited_expression_coverage": bool(limited), "conclusion_scope": "combination",
        "audience": conditions.audience, "market": ideas_row.market, "ad_language": doc.ad_language,
        "strategy_language": doc.strategy_language, "language_confirmation_required": False,
        "placement": conditions.placement or text["placement"], "offer": conditions.offer or text["offer"],
        "landing_page": conditions.landing_page, "baseline": text["baseline"], "currency": currency,
        "media_budget": conditions.media_budget or answers.round_budget * conditions.max_small_rounds,
        "small_round_budget": answers.round_budget, "duration_days": answers.duration_days,
        "allocation": text["allocation"]["split" if split else "single"],
        "rules": {"business_metric": "cpa", "threshold": answers.target_cost,
                  "business_goal": text["goal"].format(target=target, currency=currency),
                  "evidence_requirements": text["evidence"].format(
                      subject=subject, purchases=protocol.min_purchases, clicks=protocol.min_clicks,
                      impressions=protocol.min_impressions, spend=money(protocol.min_spend), currency=currency,
                      days=protocol.min_days),
                  "evaluation_schedule": text["schedule"].format(days=answers.duration_days, lag=protocol.attribution_lag_days),
                  "stop_rule": text["stop"].format(days=answers.duration_days),
                  "max_small_rounds": conditions.max_small_rounds},
        # A test makes no image: one planned execution per combination, so no slot waits for a generation.
        "executions_per_combination": 1,
        "angles": angles, "combinations": [c.model_dump(mode="json", exclude_unset=True) for c in combos],
        "groups": [{"id": group_id, "name": item["title"][:2000], "combination_ids": item["combination_ids"],
                    "rationale": text["group"][comparison]} for group_id, item in zip(_group_ids(doc, len(items)), items)],
        "plan_review": [], "guiding_questions": [], "style_variation": "held",
        # The style notes reach the image model in English, as the ideas' own do.
        "style_notes": doc.style_notes or text["style"],
        "brand_kit": doc.brand_kit.model_dump(mode="json") if doc.brand_kit else None,
    }
    return TestPlan.model_validate(document)


def check_plan(ideas_row, plan: TestPlan) -> None:
    """The checks a plan-first plan passes when it is confirmed, unchanged; ``MarketingError`` when one refuses."""
    service.validate_plan(SimpleNamespace(understanding_json=understanding(ideas_row)), plan, confirming=True)


def quote_hash(ideas_revision: int, kept_ads, answers, conditions: dict, checks: dict, protocol, plan) -> str:
    """Plan E8: what starting the test must still match -- the ideas' revision, each kept ad and the intent its
    image was made from, the answers, the conditions after the defaults, the check maximum, the protocol and the
    plan written from them."""
    return service.digest([QUOTE_VERSION, ideas_revision,
                           sorted([ad.asset_version_id, ad.intent_hash] for ad in kept_ads),
                           answers.model_dump(mode="json"), conditions, checks,
                           protocol.model_dump(mode="json"), plan])


def as_dict(conditions: Conditions) -> dict:
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(conditions).items()}
