"""The governed LLM plan drafter (Issue #164): three answers -> 3-5 directions.

Contract §3.2 / §5.1 / §6. The drafter asks the governed ``llm_chat``
primitive (``app.providers.llm_chat``, semantic ``test_plan_draft``) for one
JSON object -- factors with Chinese labels and English positive / negative
fragments (always including a 「钩子」 hook factor), 3-5 direction cards
(title, "what this tests", hypothesis, scene, copy) that each pick values of
every factor, and optional exclusions -- then validates it and maps it into
the provider-neutral :class:`~app.plans.drafting.PlanDraft` the rest of
``app.plans`` already persists and plans.

Inputs (all frozen on the plan's recipe at admission):

* the published ProductVersion's facts -- name, description, selling points,
  must-match items, a few specs;
* the goal (``explore`` / ``optimize``; an optimize plan lists its seed
  assets' factor/value labels as the baseline);
* the chosen audience card (or the user's own wording);
* the scale (sample / small / full -- the planner, not the model, decides how
  many combinations each scale materialises);
* winners / losers value labels (Issue #166 produces them; empty is fine):
  exploit winners, avoid losers, keep at least one exploratory direction.

Governance (§6, ADR 0005)
-------------------------
``service.create_plan`` builds the *same* request (:func:`build_request`,
deterministic in the frozen recipe) and reserves its exact maximum cost with
``reserve_llm_chat(request=...)`` in the admission transaction; the Worker's
call (``call_name`` :data:`CALL_NAME` on the plan's Job) settles it at
reported usage. A re-run Job replays the stored answer instead of paying
again, so a replayed plan maps the same answer to the same factor keys,
value ids and direction ids (:func:`map_llm_draft` is a pure function of the
answer).

Stable ids
----------
The model names factors with an English ``key`` hint and values by their
Chinese label; directions reference factors by label (or key) and values by
label. :func:`map_llm_draft` derives factor keys deterministically -- the
hook factor is always ``hook``; any other factor is its slugged key hint, or
``f_<hash>`` of its label when the hint is unusable or taken -- and value keys
as ``v_<hash>`` of the label; ``normalize_plan_draft`` then assigns the
positional ``<factor>-N`` value ids and ``dir-N`` direction ids. Nothing is
random, so the same answer always yields the same template ids and labels.

Strict structured outputs
-------------------------
The schema stays inside the subset OpenAI strict mode accepts (every property
required, ``additionalProperties: false`` everywhere, no open objects and no
length/count bounds), so direction -> value mappings travel as
``[{factor, values}]`` pairs, the way product intake carries specs. Counts
(3-5 directions, 2-4 factors, one hook) are checked here. The prompt aims
for 2-4 values per factor; valid answers may use up to the domain's eight
values without discarding referenced choices or paying for a second call.

Something to compare (Issue #193)
---------------------------------
The prompt requires every direction to give at least one non-hook factor two
values and the plan to span 6-32 combinations. A model that still answers
with single values everywhere is not failed and paid again: the mapping
ends with :func:`app.plans.drafting.widen_plan_draft`, which deterministically
adds a value another direction already uses and records ``auto_widened`` in
the drafting evidence. It is part of the pure mapping, so a replayed answer
widens the same way.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from app.platform.tenancy import TenantContext
from app.providers.llm_chat import LlmChatClient, LlmChatError, LlmChatRequest
from app.providers.llm_types import LlmOutputValidationError
from app.providers.llm_mock import TEST_PLAN_DRAFT_SCHEMA_NAME, TEST_PLAN_DRAFT_SEMANTIC

from . import drafting
from .drafting import (
    AudienceChoice,
    DirectionDraft,
    DraftInvalid,
    FactorDraft,
    FactorValueDraft,
    PlanDraft,
    ProductFacts,
    WinnersLosers,
    clean_fragments,
    clean_text,
)

SEMANTIC = TEST_PLAN_DRAFT_SEMANTIC
SCHEMA_NAME = TEST_PLAN_DRAFT_SCHEMA_NAME
CALL_NAME = "draft"

LLM_DRAFTER = "llm"
LLM_DRAFTER_VERSION = "llm-plan-drafter/v4"

MIN_DIRECTIONS = 3
MAX_DIRECTIONS = 5
MIN_FACTORS = 2
MAX_FACTORS = 4
MIN_VALUES = 2
# Four is the prompt's design target, not a separate domain validity rule.
# Keep the acceptance ceiling aligned with normalization and user edits.
MAX_VALUES = drafting.MAX_VALUES_PER_FACTOR

HOOK_FACTOR_KEY = drafting.HOOK_FACTOR_KEY
HOOK_LABEL = "钩子"

#: Output ceiling of the one call (and its one schema retry): five
#: directions in Chinese plus a 4x4 factor catalog with English fragments,
#: with headroom for a reasoning model's hidden tokens.
MAX_OUTPUT_TOKENS = 6_000
#: The user prompt is cut to this many UTF-8 bytes (a guard against a huge
#: product description; admission reserves the exact request either way).
MAX_USER_PROMPT_BYTES = 16_000
MAX_SELLING_POINTS_IN_PROMPT = 8
MAX_MUST_MATCH_IN_PROMPT = 10
MAX_SPECS_IN_PROMPT = 12
MAX_SIGNALS_IN_PROMPT = 12


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEST_PLAN_DRAFT_SCHEMA: dict[str, Any] = _object({
    "factors": {
        "type": "array",
        "items": _object({
            "key": {"type": "string"},
            "label": {"type": "string"},
            "is_hook": {"type": "boolean"},
            "values": {
                "type": "array",
                "items": _object({
                    "label": {"type": "string"},
                    "positive_fragments": _STRINGS,
                    "negative_fragments": _STRINGS,
                }),
            },
        }),
    },
    "directions": {
        "type": "array",
        "items": _object({
            "title": {"type": "string"},
            "what_it_tests": {"type": "string"},
            "hypothesis": {"type": "string"},
            "scene_description": {"type": "string"},
            "copy": _object({"headline": {"type": "string"}, "primary_text": {"type": "string"}}),
            "factor_values": {
                "type": "array",
                "items": _object({"factor": {"type": "string"}, "values": _STRINGS}),
            },
            "exploratory": {"type": "boolean"},
            "positive_fragments": _STRINGS,
            "negative_fragments": _STRINGS,
        }),
    },
    "exclusions": {
        "type": "array",
        "items": _object({
            "factor_a": {"type": "string"},
            "value_a": {"type": "string"},
            "factor_b": {"type": "string"},
            "value_b": {"type": "string"},
        }),
    },
    "notes": {"type": "string"},
})


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是资深的 Meta 广告效果创意策略师。给定一个产品、本次客群、目的和规模，你要起草一份"创意组合测试"方案：几个值得验证的创意方向，以及让这些方向可以拆开归因的因素（维度）设计。

因素（factors）规则：
- 输出 2~4 个因素，每个因素 2~4 个取值（value）。
- 因素之间相互独立、可自由组合（任意取值组合都能构成一张合理的广告图）；覆盖不同层面：人物有无/类型、使用场景、产品呈现方式（如是否出现汽车等关联物）、氛围光线。
- **必须包含且只包含一个「钩子」因素**（is_hook = true，label 写「钩子」）：营销角度如何进画面——痛点场景 / 核心卖点直给 / 省钱实惠 / 社会证明……营销变量与画面变量同等重要，只测画面最后只能学到"暖光比冷光好"。
- key：该因素的英文 snake_case 短名（如 people、scene、car、hook）。label、取值 label 用简体中文（给运营看），简短、互不重复。
- 每个取值给 positive_fragments（1~3 条英文摄影/广告语言，具体、可视觉化，直接描述画面里该出现什么）和 negative_fragments（0~3 条英文，画面里不该出现什么）。片段只描述画面，不写模型名、不写参数、不写"generate"之类的指令。

方向（directions）规则：
- 输出 3~5 个方向。每个方向是一个可被投放数据证伪的假设，而不是一张图。
- title：中文短标题（≤ 16 字）。
- what_it_tests：一句大白话中文，说清"这组在验证什么"，必须是可证伪的假设（例如"用省电费的钩子是否比充得快更能打动通勤族"）。
- hypothesis：为什么这么想（一两句中文）。scene_description：画面描述（中文）。
- copy：headline（中文广告标题，≤ 20 字）与 primary_text（中文正文，1~2 句）。
- factor_values：对**每一个**因素给出该方向使用的取值（按因素 label 引用因素，按取值 label 引用取值），每个因素选 1~2 个取值。
- **每个方向必须至少有一个非钩子因素选 2 个取值**：这是该方向内部要对比的画面元素，否则"完整测试"和样图一模一样、测不出元素之间的差别。例如「人物」选「无人物」和「单人操作」，其余因素各选 1 个，这个方向就有 2 个组合。
- 一个方向的组合数 = 它各因素所选取值个数的乘积；整个方案的组合总数（各方向组合数之和）必须在 6~32 之间。例如 4 个方向、每个方向 2~4 个组合，共 8~16 个组合。
- 不同方向应在钩子或主要画面因素上有明显差异，便于归因。
- positive_fragments / negative_fragments：该方向整体画面的英文片段（各 0~2 条）。
- exploratory：该方向是否在探索尚无数据支持的新取值。

已有结论（winners / losers）：
- 多用胜出的取值（exploit），避开落败的取值；但至少保留一个 exploratory = true 的方向去探索新取值。
- 没有已有结论时，所有方向都是探索。
- 目的为"优化已有广告"时，以基线素材的取值为出发点，每个方向只改变一两个因素。

exclusions：明显不合理、不能同时出现在一张图里的取值对（按因素 label 与取值 label 引用），没有则为空数组。

必须遵守产品的"外观必须一致"项；不得编造产品没有的功能或规格；不要出现价格、折扣与促销信息。
严格只输出符合 JSON schema 的对象。"""

_GOAL_COPY = {
    "explore": "探索新方向",
    "optimize": "优化已有广告（在基线素材上做改进）",
}
_SCALE_COPY = {
    "sample": "先出样图（每个方向先出 1 张，看过样图再展开完整测试）",
    "small": "小测（全部取值至少出现一次，约 ≤ 12 个组合）",
    "full": "完整测试（全因子组合，≤ 32 个组合）",
}


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _tags(asset: drafting.SeedAsset) -> dict[str, str]:
    """An asset's factor -> value *labels* (``dimension_labels``), falling
    back to its raw ``dimension_tags`` when no label was resolved."""
    labels = dict(getattr(asset, "dimension_labels", None) or {})
    return labels or dict(asset.dimension_tags or {})


def _signal_lines(assets: list[drafting.SeedAsset]) -> list[str]:
    """One line per distinct factor = value pair, most frequent first."""
    counts: dict[tuple[str, str], int] = {}
    order: list[tuple[str, str]] = []
    for asset in assets:
        for factor, value in _tags(asset).items():
            pair = (clean_text(factor, 40), clean_text(value, 80))
            if not pair[0] or not pair[1]:
                continue
            if pair not in counts:
                order.append(pair)
                counts[pair] = 0
            counts[pair] += 1
    ranked = sorted(order, key=lambda pair: (-counts[pair], order.index(pair)))
    return [
        f"- {factor} = {value}" + (f"（{counts[(factor, value)]} 个素材）" if counts[(factor, value)] > 1 else "")
        for factor, value in ranked[:MAX_SIGNALS_IN_PROMPT]
    ]


def _verdict_lines(verdicts: list, verdict: str) -> list[str]:
    """Learned value verdicts (``WinnersLosers.value_verdicts``) as prompt
    lines: ``- 人物 = 真人使用：<the learnings sentence>``."""
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in verdicts:
        if item.verdict != verdict:
            continue
        pair = (clean_text(item.factor_label, 40), clean_text(item.value_label, 80))
        if not pair[0] or not pair[1] or pair in seen:
            continue
        seen.add(pair)
        sentence = clean_text(item.sentence, 160)
        lines.append(f"- {pair[0]} = {pair[1]}" + (f"：{sentence}" if sentence else ""))
    return lines[:MAX_SIGNALS_IN_PROMPT]


def build_user_prompt(
    product: ProductFacts,
    goal: str,
    audience: AudienceChoice,
    scale: str,
    winners_losers: WinnersLosers,
) -> str:
    """The user message: product facts, the three answers, the learnings."""
    parts: list[str] = []
    lines = [f"产品名称：{clean_text(product.name, 200) or '（未填写）'}"]
    lines.append(f"产品描述：{clean_text(product.description, 1000) or '（未填写）'}")
    points = [clean_text(point, 200) for point in product.selling_points][:MAX_SELLING_POINTS_IN_PROMPT]
    points = [point for point in points if point]
    lines.append("核心卖点：" + ("\n" + "\n".join(f"- {point}" for point in points) if points else "（未填写）"))
    specs = [
        f"- {clean_text(name, 60)}：{clean_text(value, 120)}"
        for name, value in list((product.specs or {}).items())[:MAX_SPECS_IN_PROMPT]
    ]
    if specs:
        lines.append("规格（仅供参考，不要编造其他规格）：\n" + "\n".join(specs))
    must_match = [
        f"- [{clean_text(item.get('type'), 20) or 'other'}] {clean_text(item.get('description'), 200)}"
        for item in (product.must_match or [])[:MAX_MUST_MATCH_IN_PROMPT]
        if isinstance(item, dict) and clean_text(item.get("description"), 200)
    ]
    lines.append("外观必须一致（画面里不能改变）：" + ("\n" + "\n".join(must_match) if must_match else "（无）"))
    parts.append("\n".join(lines))

    who = clean_text(audience.text, 500) or "（未指定）"
    audience_lines = [f"本次客群：{who}"]
    card = audience.card if isinstance(audience.card, dict) else None
    if card:
        for key, label in (("who", "谁"), ("why", "为什么买"), ("pain", "痛点")):
            value = clean_text(card.get(key), 300)
            if value:
                audience_lines.append(f"- {label}：{value}")
    parts.append("\n".join(audience_lines))

    baseline = [asset for asset in winners_losers.winners if asset.verdict == "seed"]
    winners = [asset for asset in winners_losers.winners if asset.verdict != "seed"]
    verdicts = list(getattr(winners_losers, "value_verdicts", None) or [])
    goal_copy = _GOAL_COPY.get(goal, goal)
    if goal == "optimize" and verdicts and not baseline:
        goal_copy = "按上一轮的投放结论生成下一轮"
    parts.append(
        f"本次目的：{goal_copy}\n"
        f"生成规模：{_SCALE_COPY.get(scale, scale)}"
    )
    if goal == "optimize" and (baseline or not verdicts):
        baseline_lines = _signal_lines(baseline)
        parts.append(
            f"基线素材（{len(baseline)} 个，以它们的取值为出发点）：\n"
            + ("\n".join(baseline_lines) if baseline_lines else "- （素材没有可用的取值标签）")
        )
    # Asset-level signals first, then the learned value verdicts (Issue #166):
    # each line carries the measured lift, interval and n from the snapshot.
    winner_lines = _signal_lines(winners) + _verdict_lines(verdicts, "winner")
    loser_lines = _signal_lines(list(winners_losers.losers)) + _verdict_lines(verdicts, "loser")
    flat_lines = _verdict_lines(verdicts, "no_difference")
    if winner_lines or loser_lines or flat_lines:
        parts.append(
            "已有结论（来自投放数据，按维度取值归因）：\n"
            + "胜出的取值（多用）：\n" + ("\n".join(winner_lines) if winner_lines else "- （无）") + "\n"
            + "落败的取值（避开）：\n" + ("\n".join(loser_lines) if loser_lines else "- （无）") + "\n"
            + ("差异不显著的取值（可保留，也可换成新取值）：\n" + "\n".join(flat_lines) + "\n" if flat_lines else "")
            + "请至少保留一个 exploratory 的方向。"
        )
    else:
        parts.append("已有结论：暂无投放结论，所有方向都是探索。")
    parts.append("请起草这次测试方案。")
    return _truncate_utf8("\n\n".join(parts), MAX_USER_PROMPT_BYTES)


def build_request(
    product: ProductFacts,
    goal: str,
    audience: AudienceChoice,
    scale: str,
    winners_losers: WinnersLosers,
) -> LlmChatRequest:
    """The one governed request; deterministic in the plan's frozen recipe,
    so admission reserves exactly what the Worker later sends."""
    return LlmChatRequest.build(
        semantic=SEMANTIC,
        system=SYSTEM_PROMPT,
        user=build_user_prompt(product, goal, audience, scale, winners_losers),
        json_schema=TEST_PLAN_DRAFT_SCHEMA,
        schema_name=SCHEMA_NAME,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        output_validation_policy=LLM_DRAFTER_VERSION,
        output_validator=validate_plan_output,
    )


def prompt_digest() -> str:
    """Version evidence for the prompt and schema this drafter sends."""
    payload = json.dumps(
        {"system": SYSTEM_PROMPT, "schema": TEST_PLAN_DRAFT_SCHEMA}, sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Mapping: model answer -> PlanDraft (deterministic)
# ---------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _short_hash(text: str, length: int = 6) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _slug(text: object) -> str:
    slug = _SLUG_STRIP.sub("_", str(text or "").strip().lower()).strip("_")
    if slug and not slug[0].isalpha():
        slug = f"f_{slug}"
    return slug[:24].rstrip("_")


def _is_hook(raw: dict) -> bool:
    return bool(raw.get("is_hook")) or HOOK_LABEL in clean_text(raw.get("label"), drafting.MAX_LABEL_CHARS)


def _norm(text: object) -> str:
    return clean_text(text, drafting.MAX_LABEL_CHARS).casefold()


def _reference_label(text: object) -> str:
    """Conservative display-label aliases, never fuzzy semantic matching.

    The model sometimes omits 整体 before a view, e.g. 整体侧面展示 ->
    侧面展示. Keep colors, numbers, negation, brand text and the view itself
    intact. A collision between catalog values is rejected by the resolver.
    """
    label = unicodedata.normalize("NFKC", _norm(text)).casefold()
    if label.startswith("整体") and label[2:].startswith(
        ("正面", "侧面", "背面", "俯视", "仰视", "斜侧", "顶部", "底部")
    ):
        return label[2:]
    return label


def factor_key_for(raw: dict, *, hook: bool, taken: set[str]) -> str:
    """A factor's stable key: ``hook``; else the slugged key hint; else (or
    on a clash) ``<slug or f>_<hash of label>``."""
    if hook:
        return HOOK_FACTOR_KEY
    label = clean_text(raw.get("label"), drafting.MAX_LABEL_CHARS)
    slug = _slug(raw.get("key"))
    if (
        slug
        and drafting.FACTOR_KEY_PATTERN.match(slug)
        and slug not in taken
        and slug not in drafting.RESERVED_FACTOR_KEYS
        and slug != HOOK_FACTOR_KEY
    ):
        return slug
    base = slug if slug and slug not in drafting.RESERVED_FACTOR_KEYS and slug != HOOK_FACTOR_KEY else "f"
    candidate = f"{base[:20]}_{_short_hash(label or slug or 'factor')}"
    counter = 2
    while candidate in taken:
        candidate = f"{base[:20]}_{_short_hash(f'{label}#{counter}')}"
        counter += 1
    return candidate


def value_key_for(label: str) -> str:
    return f"v_{_short_hash(label, 10)}"


def map_llm_draft(data: dict) -> PlanDraft:
    """Validate the model's answer and map it into a :class:`PlanDraft`.

    Raises :class:`DraftInvalid` (``draft_invalid`` on the plan) for an
    answer that is schema-valid but unusable: wrong counts, no 「钩子」
    factor, duplicate labels, a direction naming an unknown factor or value
    or leaving a factor out. Directions beyond :data:`MAX_DIRECTIONS` are
    dropped (in order); exclusions naming unknown labels are ignored. A
    uniquely matching display-label abbreviation is resolved and recorded
    in ``resolved_references``; substantive or ambiguous differences fail.
    New calls validate the full plan inside the governed call's retry budget.
    A direction with nothing to compare is widened
    (:func:`app.plans.drafting.widen_plan_draft`, ``auto_widened``).
    """
    if not isinstance(data, dict):
        raise DraftInvalid("draft_not_object", "the answer is not a JSON object")
    raw_factors = [item for item in data.get("factors") or [] if isinstance(item, dict)]
    raw_directions = [item for item in data.get("directions") or [] if isinstance(item, dict)]
    if not MIN_FACTORS <= len(raw_factors) <= MAX_FACTORS:
        raise DraftInvalid(
            "factor_count_invalid", f"{len(raw_factors)} factors; a plan needs {MIN_FACTORS}-{MAX_FACTORS}",
        )
    hooks = [item for item in raw_factors if _is_hook(item)]
    if len(hooks) != 1:
        raise DraftInvalid(
            "hook_factor_missing" if not hooks else "hook_factor_ambiguous",
            f"a plan needs exactly one 「{HOOK_LABEL}」 factor, the answer has {len(hooks)}",
        )
    if len(raw_directions) < MIN_DIRECTIONS:
        raise DraftInvalid(
            "direction_count_invalid", f"{len(raw_directions)} directions; a plan needs {MIN_DIRECTIONS}-{MAX_DIRECTIONS}",
        )
    raw_directions = raw_directions[:MAX_DIRECTIONS]

    factors: list[FactorDraft] = []
    #: casefolded factor label / key hint / derived key -> factor key
    factor_refs: dict[str, str] = {}
    #: (factor key, casefolded value label) -> value key
    value_refs: dict[tuple[str, str], str] = {}
    resolved_references: list[dict] = []
    taken: set[str] = set()
    labels_seen: set[str] = set()
    for raw in raw_factors:
        label = clean_text(raw.get("label"), drafting.MAX_LABEL_CHARS)
        if not label:
            raise DraftInvalid("factor_label_missing", "a factor has no label")
        if _norm(label) in labels_seen:
            raise DraftInvalid("duplicate_factor", f"factor {label!r} appears twice")
        labels_seen.add(_norm(label))
        hook = raw is hooks[0]
        key = factor_key_for(raw, hook=hook, taken=taken)
        taken.add(key)
        raw_values = [item for item in raw.get("values") or [] if isinstance(item, dict)]
        if not MIN_VALUES <= len(raw_values) <= MAX_VALUES:
            raise DraftInvalid(
                "factor_values_invalid",
                f"factor {label!r} has {len(raw_values)} values; a factor needs {MIN_VALUES}-{MAX_VALUES}",
            )
        values: list[FactorValueDraft] = []
        for raw_value in raw_values:
            value_label = clean_text(raw_value.get("label"), drafting.MAX_LABEL_CHARS)
            if not value_label:
                raise DraftInvalid("factor_value_invalid", f"factor {label!r} has a value without a label")
            if (key, _norm(value_label)) in value_refs:
                raise DraftInvalid("duplicate_factor_value", f"factor {label!r} repeats value {value_label!r}")
            value_key = value_key_for(value_label)
            value_refs[(key, _norm(value_label))] = value_key
            values.append(FactorValueDraft(
                key=value_key,
                label=value_label,
                positive_fragments=clean_fragments(raw_value.get("positive_fragments")),
                negative_fragments=clean_fragments(raw_value.get("negative_fragments")),
            ))
        factors.append(FactorDraft(key=key, label=HOOK_LABEL if hook else label, values=values))
        for ref in (label, raw.get("key"), key):
            norm = _norm(ref)
            if norm and norm not in factor_refs:
                factor_refs[norm] = key
        if hook:
            factor_refs.setdefault(_norm(HOOK_LABEL), key)

    def resolve_factor(ref: object, where: str) -> str:
        key = factor_refs.get(_norm(ref))
        if key is None:
            raise DraftInvalid("unknown_factor", f"{where} names unknown factor {clean_text(ref, 80)!r}")
        return key

    def resolve_value(factor_key: str, ref: object, where: str) -> str:
        value_key = value_refs.get((factor_key, _norm(ref)))
        if value_key is None:
            factor = next(item for item in factors if item.key == factor_key)
            candidates = [value for value in factor.values if _reference_label(value.label) == _reference_label(ref)]
            if len(candidates) == 1:
                value = candidates[0]
                correction = {"factor": factor_key, "reference": clean_text(ref, drafting.MAX_LABEL_CHARS),
                              "value_label": value.label}
                if correction not in resolved_references:
                    resolved_references.append(correction)
                return value.key
            if candidates:
                raise DraftInvalid(
                    "ambiguous_factor_value", f"{where} names ambiguous {factor_key} value {clean_text(ref, 80)!r}",
                )
            raise DraftInvalid(
                "unknown_factor_value", f"{where} names unknown {factor_key} value {clean_text(ref, 80)!r}",
            )
        return value_key

    directions: list[DirectionDraft] = []
    for index, raw in enumerate(raw_directions, start=1):
        title = clean_text(raw.get("title"), drafting.MAX_TITLE_CHARS)
        if not title:
            raise DraftInvalid("direction_title_missing", f"direction {index} has no title")
        where = f"direction {title!r}"
        mapped: dict[str, list[str]] = {}
        for pair in raw.get("factor_values") or []:
            if not isinstance(pair, dict):
                continue
            factor_key = resolve_factor(pair.get("factor"), where)
            refs = [resolve_value(factor_key, value, where) for value in pair.get("values") or []]
            mapped[factor_key] = list(dict.fromkeys(mapped.get(factor_key, []) + refs))
        missing = [factor.label for factor in factors if not mapped.get(factor.key)]
        if missing:
            raise DraftInvalid("direction_factor_missing", f"{where} picks no value for {missing}")
        copy = raw.get("copy") if isinstance(raw.get("copy"), dict) else {}
        directions.append(DirectionDraft(
            title=title,
            what_it_tests=clean_text(raw.get("what_it_tests"), drafting.MAX_TEXT_CHARS),
            hypothesis=clean_text(raw.get("hypothesis"), drafting.MAX_TEXT_CHARS),
            scene_description=clean_text(raw.get("scene_description"), drafting.MAX_TEXT_CHARS),
            headline=clean_text(copy.get("headline"), 200),
            primary_text=clean_text(copy.get("primary_text"), drafting.MAX_TEXT_CHARS),
            factor_values={factor.key: mapped[factor.key] for factor in factors},
            positive_fragments=clean_fragments(raw.get("positive_fragments")),
            negative_fragments=clean_fragments(raw.get("negative_fragments")),
        ))
        if not directions[-1].what_it_tests:
            raise DraftInvalid("what_it_tests_missing", f"{where} does not say what it tests")

    constraints: list[dict] = []
    ignored = 0
    for raw in data.get("exclusions") or []:
        if not isinstance(raw, dict):
            continue
        try:
            factor_a = resolve_factor(raw.get("factor_a"), "an exclusion")
            factor_b = resolve_factor(raw.get("factor_b"), "an exclusion")
            value_a = resolve_value(factor_a, raw.get("value_a"), "an exclusion")
            value_b = resolve_value(factor_b, raw.get("value_b"), "an exclusion")
        except DraftInvalid:
            ignored += 1
            continue
        if factor_a == factor_b:
            ignored += 1
            continue
        constraint = {"factor_a": factor_a, "value_a": value_a, "factor_b": factor_b, "value_b": value_b}
        if constraint not in constraints:
            constraints.append(constraint)

    exploratory = sum(1 for raw in raw_directions if raw.get("exploratory"))
    notes = clean_text(data.get("notes"), drafting.MAX_TEXT_CHARS)
    return drafting.widen_plan_draft(PlanDraft(
        factors=factors,
        directions=directions,
        constraints=constraints,
        drafter=LLM_DRAFTER,
        drafter_version=LLM_DRAFTER_VERSION,
        notes=notes,
        evidence={
            "exploratory_directions": exploratory,
            "ignored_exclusions": ignored,
            "dropped_directions": max(0, len(data.get("directions") or []) - MAX_DIRECTIONS),
            "resolved_references": resolved_references,
        },
    ))


def validate_plan_output(data: dict) -> None:
    """Check domain usability inside the call's existing one-retry budget.

    Pure and provider-independent: callbacks carry no credentials or writes.
    Replayed answers still pass the same mapper in the Worker.
    """
    from app.generation.planner import PlanDirectionSpace, PlannerError, enumerate_plan_space

    try:
        normalized = drafting.normalize_plan_draft(map_llm_draft(data))
        space, _ = enumerate_plan_space([
            PlanDirectionSpace(item["direction_key"], item["factor_values"])
            for item in normalized.directions
        ], normalized.constraints)
        available = {combo.direction for combo in space}
        if any(item["direction_key"] not in available for item in normalized.directions):
            raise DraftInvalid("direction_no_combinations", "constraints leave a direction with no combination")
    except (DraftInvalid, PlannerError) as invalid:
        raise LlmOutputValidationError("draft_invalid", f"{invalid.code}: {invalid.detail}") from invalid


# ---------------------------------------------------------------------------
# The drafter
# ---------------------------------------------------------------------------


class LlmPlanDrafter:
    """:class:`~app.plans.drafting.PlanDrafter` over the governed ``llm_chat``.

    Bound to the plan's Job (``job_id`` / ``operation_id``): the call key is
    (Job, :data:`CALL_NAME`), whose reservation admission made. Under
    ``execution_mode="mock"`` the controlled mock answers with the EV-charger
    example (``app.providers.llm_mock``), exercising the same mapping.
    ``call_evidence`` holds the last call's evidence (also on failure).
    """

    name = LLM_DRAFTER

    def __init__(
        self,
        ctx: TenantContext,
        *,
        llm_client: LlmChatClient,
        job_id: str,
        operation_id: str,
        execution_mode: str,
    ) -> None:
        self._ctx = ctx
        self._client = llm_client
        self._job_id = job_id
        self._operation_id = operation_id
        self._mode = execution_mode
        self.call_evidence: dict[str, Any] = {}

    async def draft_plan(
        self,
        product_version: ProductFacts,
        goal: str,
        audience: AudienceChoice,
        scale: str,
        winners_losers: WinnersLosers,
    ) -> PlanDraft:
        request = build_request(product_version, goal, audience, scale, winners_losers)
        try:
            result = await self._client.chat(
                self._ctx,
                job_id=self._job_id,
                operation_id=self._operation_id,
                call_name=CALL_NAME,
                request=request,
                execution_mode=self._mode,
            )
        except LlmChatError as error:
            self.call_evidence = {
                "call_name": CALL_NAME,
                "attempt_id": error.attempt_id,
                "execution_mode": self._mode,
                "prompt_digest": prompt_digest(),
                "error": error.as_dict(),
                **({"usage": error.usage} if error.usage else {}),
                **({"cost": error.cost} if error.cost else {}),
            }
            raise drafting.DraftingFailed(
                error.code, error.detail,
                dispatch_certainty=error.dispatch_certainty,
                attempt_id=error.attempt_id,
            ) from error
        self.call_evidence = {
            "call_name": CALL_NAME,
            "attempt_id": result.attempt_id,
            "provider": result.provider,
            "model": result.model,
            "model_version": result.model_version,
            "execution_mode": result.execution_mode,
            "usage": result.usage,
            "cost": result.cost,
            "replayed": result.replayed,
            "prompt_digest": prompt_digest(),
        }
        draft = map_llm_draft(result.data)
        draft.evidence = {**draft.evidence, "call": self.call_evidence}
        return draft


__all__ = [
    "CALL_NAME",
    "LLM_DRAFTER",
    "LLM_DRAFTER_VERSION",
    "LlmPlanDrafter",
    "MAX_OUTPUT_TOKENS",
    "SCHEMA_NAME",
    "SEMANTIC",
    "SYSTEM_PROMPT",
    "TEST_PLAN_DRAFT_SCHEMA",
    "build_request",
    "build_user_prompt",
    "factor_key_for",
    "map_llm_draft",
    "prompt_digest",
    "value_key_for",
]
