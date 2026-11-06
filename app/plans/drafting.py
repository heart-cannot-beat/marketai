"""Drafting a test plan: the ``PlanDrafter`` seam and the mock drafter.

Contract §3.2: the AI drafts 3-5 direction cards (title, "what this tests",
scene, copy, folded factors and values); the user keeps, edits or adds
directions; the deterministic planner (``app.generation.planner``) turns
them into combinations. §5.1: the draft is provider-neutral -- hypotheses and
a factor design with positive / negative English fragments, never a
provider-specific prompt.

The interface Issue #164 implements
-----------------------------------
:class:`PlanDrafter` is the only thing the governed LLM drafter has to
provide::

    async def draft_plan(
        self,
        product_version: ProductFacts,
        goal: str,                     # "explore" | "optimize"
        audience: AudienceChoice,
        scale: str,                    # "sample" | "small" | "full"
        winners_losers: WinnersLosers,
    ) -> PlanDraft

``winners_losers.value_verdicts`` (Issue #166) carries the product's learned
dimension-value conclusions -- factor / value *labels* with ``winner`` /
``loser`` / ``no_difference``, the metric, lift and interval -- from the
latest production learnings snapshot, or from the snapshot the user chose
for "按结论生成下一轮" (``recipe.learnings`` records which). Exploit
winners, skip losers, keep at least one exploratory direction.

It returns *keys and labels*; :func:`normalize_plan_draft` validates the
draft and assigns the stable ids (``dir-N``, ``<factor>-N``) the plan, the
Library template and the combination signatures use, so a drafter never has
to invent identifiers. A drafter raises :class:`DraftingFailed` with a
stable ``code`` when it cannot produce a plan (the Job ends the plan
``failed`` with that code).

:func:`resolve_plan_drafter` is the factory the Worker calls with the plan's
drafting ``execution_mode``: ``byok`` gets the governed
:class:`app.plans.llm_drafter.LlmPlanDrafter` (Issue #164), ``mock`` keeps
the deterministic, zero-cost :class:`MockPlanDrafter` below (product-aware,
no paid call). A ``byok`` plan's maximum LLM cost is reserved at admission
(``service.create_plan`` -> ``reserve_llm_chat(request=...)`` with the exact
request :func:`app.plans.llm_drafter.build_request` builds from the frozen
recipe), the drafter settles it through ``LlmChatClient`` inside the Job,
and a plan that fails before its call releases it.

Winners / losers (Issue #166 produces them)
-------------------------------------------
:class:`WinnersLosers` lists :class:`SeedAsset` items. The drafter reads
each item's ``dimension_labels`` -- ``{factor label: value label}``, e.g.
``{"钩子": "谷电省钱", "场景": "生活"}`` -- falling back to the raw
``dimension_tags`` when no labels were resolved. ``winners`` items with
``verdict="seed"`` are an optimize plan's baseline assets; any other verdict
(``"winner"``) is a learned winner; ``losers`` are values to avoid. Empty is
valid (every direction is then exploratory).
"""
from __future__ import annotations

import itertools
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Protocol

GOALS = ("explore", "optimize")
MAX_DIRECTIONS = 8
MAX_FACTORS = 4
MAX_VALUES_PER_FACTOR = 8
MAX_LABEL_CHARS = 80
MAX_TITLE_CHARS = 120
MAX_TEXT_CHARS = 1000
MAX_FRAGMENTS = 6
MAX_FRAGMENT_CHARS = 200

FACTOR_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_FACTOR_KEYS = frozenset({"direction"})

MOCK_DRAFTER = "mock"
MOCK_DRAFTER_VERSION = "mock-plan-drafter/v2"

#: The 「钩子」 factor's key (``app.plans.llm_drafter`` always maps it here).
#: Widening never touches it: the contrast inside a direction is a picture
#: element, while the hook is what tells directions apart.
HOOK_FACTOR_KEY = "hook"
#: Issue #193: a drafted plan's combination count (after exclusions) should
#: land in this range so a full test can measure more than the sample does.
MIN_PLAN_COMBINATIONS = 6
MAX_PLAN_COMBINATIONS = 32


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductFacts:
    """The published ProductVersion a plan is drafted for (frozen at create)."""

    product_id: str
    entry_id: str
    version_id: str
    name: str
    description: str = ""
    selling_points: list[str] = field(default_factory=list)
    audiences: list[dict] = field(default_factory=list)
    must_match: list[dict] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)
    reference_version_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "ProductFacts":
        return cls(**{key: raw[key] for key in cls.__dataclass_fields__ if key in raw})


@dataclass(frozen=True)
class AudienceChoice:
    """Question 2: the chosen audience card (or the user's own wording)."""

    text: str
    card_index: int | None = None
    card: dict | None = None


@dataclass(frozen=True)
class SeedAsset:
    """An asset an ``optimize`` plan builds on, with its dimension tags."""

    asset_version_id: str
    #: ``seed`` (an optimize plan's baseline), ``winner`` or ``loser``.
    verdict: str = "seed"
    #: Raw combination tags (template-scoped dimension id -> value id).
    dimension_tags: dict[str, str] = field(default_factory=dict)
    #: Human-readable ``{factor label: value label}`` the drafter prompts with.
    dimension_labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValueVerdict:
    """A dimension-value conclusion learned from ad results (Issue #166).

    Labels, not ids: ``factor_label`` / ``value_label`` are what the
    previous round called the factor and value (value ids are plan-local).
    ``verdict`` is ``winner`` / ``loser`` / ``no_difference`` (insufficient
    data never reaches a drafter); ``metric`` is ``ctr`` or ``cpa``;
    ``lift*`` are ratio-minus-one strings (CPA: negative is better);
    ``sentence`` is the one line the learnings card shows. A drafter should
    exploit winners, skip losers and keep at least one exploratory direction.
    """

    factor_label: str
    value_label: str
    verdict: str
    metric: str = "ctr"
    lift: str | None = None
    lift_low: str | None = None
    lift_high: str | None = None
    sentence: str = ""
    factor_key: str = ""
    value_id: str = ""
    plan_id: str = ""
    round_number: int | None = None
    evidence_mode: str = "production"


@dataclass(frozen=True)
class WinnersLosers:
    winners: list[SeedAsset] = field(default_factory=list)
    losers: list[SeedAsset] = field(default_factory=list)
    #: Learned dimension-value verdicts (Issue #166), newest round first.
    value_verdicts: list[ValueVerdict] = field(default_factory=list)

    def label_set(self, verdict: str) -> set[tuple[str, str]]:
        """``{(factor_label, value_label)}`` with ``verdict``."""
        return {(item.factor_label, item.value_label) for item in self.value_verdicts if item.verdict == verdict}

    def as_dict(self) -> dict:
        return {
            "winners": [asdict(item) for item in self.winners],
            "losers": [asdict(item) for item in self.losers],
            "value_verdicts": [asdict(item) for item in self.value_verdicts],
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "WinnersLosers":
        raw = raw or {}
        known = set(ValueVerdict.__dataclass_fields__)
        return cls(
            winners=[SeedAsset(**item) for item in raw.get("winners") or []],
            losers=[SeedAsset(**item) for item in raw.get("losers") or []],
            value_verdicts=[
                ValueVerdict(**{key: value for key, value in item.items() if key in known})
                for item in raw.get("value_verdicts") or []
            ],
        )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass
class FactorValueDraft:
    key: str
    label: str
    positive_fragments: list[str] = field(default_factory=list)
    negative_fragments: list[str] = field(default_factory=list)


@dataclass
class FactorDraft:
    key: str
    label: str
    values: list[FactorValueDraft]


@dataclass
class DirectionDraft:
    title: str
    what_it_tests: str
    hypothesis: str
    scene_description: str
    headline: str
    primary_text: str
    #: factor key -> value keys (from that factor's ``values``).
    factor_values: dict[str, list[str]]
    positive_fragments: list[str] = field(default_factory=list)
    negative_fragments: list[str] = field(default_factory=list)


@dataclass
class PlanDraft:
    factors: list[FactorDraft]
    directions: list[DirectionDraft]
    #: ``{"factor_a", "value_a", "factor_b", "value_b"}`` excludes pairs in
    #: factor / value *keys* (``direction`` is not addressable by a drafter).
    constraints: list[dict] = field(default_factory=list)
    drafter: str = MOCK_DRAFTER
    drafter_version: str = MOCK_DRAFTER_VERSION
    notes: str = ""
    #: Drafter evidence recorded on the plan (e.g. the governed call).
    evidence: dict = field(default_factory=dict)


class DraftingFailed(Exception):
    """A drafter could not produce a usable plan.

    ``dispatch_certainty`` is ``uncertain`` when a paid call's outcome is
    unknown (the Job is then left for reconciliation, never retried blindly).
    """

    def __init__(self, code: str, detail: str, *, dispatch_certainty: str = "not_dispatched",
                 attempt_id: str | None = None):
        self.code = code
        self.detail = detail
        self.dispatch_certainty = dispatch_certainty
        self.attempt_id = attempt_id
        super().__init__(f"{code}: {detail}")


class PlanDrafter(Protocol):
    name: str

    async def draft_plan(
        self,
        product_version: ProductFacts,
        goal: str,
        audience: AudienceChoice,
        scale: str,
        winners_losers: WinnersLosers,
    ) -> PlanDraft: ...


# ---------------------------------------------------------------------------
# Mock drafter
# ---------------------------------------------------------------------------

_MOCK_SCENES = (
    ("居家日常", "bright cozy home living room, natural daylight"),
    ("户外出行", "outdoor urban street scene, golden hour"),
    ("办公桌面", "clean modern office desk, soft daylight"),
    ("棚拍特写", "studio macro close-up, seamless backdrop"),
)
_MOCK_ANGLES = (
    ("痛点对比", "before-and-after split composition showing the problem solved"),
    ("卖点直给", "hero product shot with one clear feature callout area"),
)
_MOCK_PEOPLE = (
    ("真人使用", "a real person naturally using the product, candid"),
    ("纯产品", "product only, no people, minimal props"),
)
_MOCK_FALLBACK_TITLES = ("生活方式代入", "问题与解决方案", "产品质感特写", "口碑与信任")


class MockPlanDrafter:
    """Deterministic, zero-cost drafter used until #164 (and under Mock).

    3-4 directions (one per selling point, padded with fixed themes), three
    factors -- scene (4 values), copy angle (2) and people (2). Direction
    *i* tests two adjacent scenes x both angles x both people options, so
    each direction spans 8 combinations: 3 directions -> 24, 4 -> 32.
    """

    name = MOCK_DRAFTER

    async def draft_plan(
        self,
        product_version: ProductFacts,
        goal: str,
        audience: AudienceChoice,
        scale: str,
        winners_losers: WinnersLosers,
    ) -> PlanDraft:
        product = product_version.name or "这款产品"
        points = [point for point in product_version.selling_points if str(point).strip()][:4]
        count = min(4, max(3, len(points)))
        who = (audience.text or "目标客群").split("：")[0][:40]
        factors = [
            FactorDraft("scene", "场景", [
                FactorValueDraft(f"s{index}", label, [fragment], [])
                for index, (label, fragment) in enumerate(_MOCK_SCENES)
            ]),
            FactorDraft("angle", "文案切入", [
                FactorValueDraft(f"a{index}", label, [fragment], [])
                for index, (label, fragment) in enumerate(_MOCK_ANGLES)
            ]),
            FactorDraft("people", "人物", [
                FactorValueDraft(f"p{index}", label, [fragment], [])
                for index, (label, fragment) in enumerate(_MOCK_PEOPLE)
            ]),
        ]
        seed_note = ""
        if goal == "optimize" and winners_losers.winners:
            seed_note = f"以已选的 {len(winners_losers.winners)} 个素材为基线，"
        # Learned value verdicts (Issue #166): keep winners, skip losers in
        # every direction but the last, which stays exploratory.
        winners = winners_losers.label_set("winner")
        losers = winners_losers.label_set("loser")
        labels = {(factor.key, value.key): (factor.label, value.label) for factor in factors for value in factor.values}
        kept_winners = [label for _key, label in sorted(labels.items()) if label in winners]
        if kept_winners:
            seed_note += "沿用上一轮胜出的" + "、".join(f"「{value}」" for _f, value in kept_winners) + "，"

        def values(factor_key: str, keys: list[str], exploratory: bool) -> list[str]:
            if exploratory or not losers:
                return keys
            kept = [key for key in keys if labels[(factor_key, key)] not in losers]
            return kept or keys

        directions: list[DirectionDraft] = []
        for index in range(count):
            point = points[index] if index < len(points) else _MOCK_FALLBACK_TITLES[index]
            scene_a = _MOCK_SCENES[index % len(_MOCK_SCENES)][0]
            exploratory = bool(winners_losers.value_verdicts) and index == count - 1
            directions.append(DirectionDraft(
                title=f"主打「{point}」"[:MAX_TITLE_CHARS],
                what_it_tests=f"验证「{point}」是否比其他方向更能打动{who}"
                + ("（探索方向：不受上一轮结论限制）" if exploratory else ""),
                hypothesis=f"{seed_note}突出「{point}」的画面会带来更高的点击率",
                scene_description=f"在{scene_a}等场景中展示{product}，画面聚焦「{point}」",
                headline=str(point)[:40],
                primary_text=f"{product}：{point}。专为{who}设计。",
                factor_values={
                    "scene": values("scene", [f"s{index % 4}", f"s{(index + 1) % 4}"], exploratory),
                    "angle": values("angle", ["a0", "a1"], exploratory),
                    "people": values("people", ["p0", "p1"], exploratory),
                },
                positive_fragments=[
                    f"advertising photograph of the product, emphasising selling point {index + 1}",
                ],
                negative_fragments=["distorted product shape", "unreadable text"],
            ))
        # Same post-condition as the governed drafter (Issue #193); a no-op
        # unless loser filtering left a direction with nothing to compare.
        return widen_plan_draft(PlanDraft(factors=factors, directions=directions, notes="mock draft"))


def resolve_plan_drafter(
    execution_mode: str,
    *,
    ctx=None,
    llm_client=None,
    job_id: str | None = None,
    operation_id: str | None = None,
) -> PlanDrafter:
    """The drafter a plan's Job uses: the governed LLM drafter for ``byok``,
    the deterministic mock drafter otherwise."""
    if execution_mode != "byok":
        return MockPlanDrafter()
    if ctx is None or llm_client is None or not job_id or not operation_id:
        raise DraftingFailed(
            "llm_unavailable", "a byok plan needs the Worker's llm_chat client, Job and Operation",
        )
    from .llm_drafter import LlmPlanDrafter

    return LlmPlanDrafter(
        ctx, llm_client=llm_client, job_id=job_id, operation_id=operation_id, execution_mode="byok",
    )


# ---------------------------------------------------------------------------
# Normalization: keys -> stable ids, limits, provider neutrality
# ---------------------------------------------------------------------------


class DraftInvalid(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def clean_fragments(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    cleaned = [clean_text(item, MAX_FRAGMENT_CHARS) for item in values]
    return [item for item in cleaned if item][:MAX_FRAGMENTS]


# ---------------------------------------------------------------------------
# Widening: every direction compares something (Issue #193)
# ---------------------------------------------------------------------------


def _combination_count(
    factor_values: dict[str, list[str]], factor_keys: list[str], constraints: list[dict],
) -> int:
    """Combinations one direction spans after the draft's exclusions (keys)."""
    axes = [factor_values.get(key) or [] for key in factor_keys]
    count = 0
    for cells in itertools.product(*axes):
        combo = dict(zip(factor_keys, cells))
        if not any(
            combo.get(str(rule.get("factor_a"))) == str(rule.get("value_a"))
            and combo.get(str(rule.get("factor_b"))) == str(rule.get("value_b"))
            for rule in constraints
        ):
            count += 1
    return count


def widen_plan_draft(draft: PlanDraft) -> PlanDraft:
    """Give every direction something to compare, deterministically.

    Contract §3.2 / §5.3: the planner is deterministic code and a full test
    only measures more than the sample when a direction spans more than one
    combination. A model that picks one value of every factor in every
    direction leaves "完整测试" identical to the sample, so after drafting:

    1. every direction whose non-hook factors are all single-valued gets one
       more value of one non-hook factor -- preferably a value another
       direction already uses (the first such value, factors in catalog
       order, directions in order), else the factor's next catalog value;
    2. while the plan spans fewer than :data:`MIN_PLAN_COMBINATIONS`, the
       direction with the fewest combinations is widened the same way.

    A candidate is skipped when the draft's exclusions swallow the new
    combinations or when it would take the plan past
    :data:`MAX_PLAN_COMBINATIONS`. The result is a pure function of the
    draft (no randomness), so a replayed answer widens identically.
    ``evidence["auto_widened"]`` lists what was added (empty when nothing
    was) and ``evidence["drafted_combinations"]`` the count before widening.
    """
    factor_keys = [factor.key for factor in draft.factors]
    contrast_keys = [key for key in factor_keys if key != HOOK_FACTOR_KEY]
    catalog = {factor.key: [value.key for value in factor.values] for factor in draft.factors}
    labels = {(factor.key, value.key): value.label for factor in draft.factors for value in factor.values}
    constraints = list(draft.constraints or [])
    chosen = [
        {key: list(dict.fromkeys((direction.factor_values or {}).get(key) or [])) for key in factor_keys}
        for direction in draft.directions
    ]
    counts = [_combination_count(values, factor_keys, constraints) for values in chosen]
    drafted = sum(counts)
    widened: list[dict] = []

    def candidates(index: int):
        own = chosen[index]
        for key in contrast_keys:
            for other, values in enumerate(chosen):
                if other == index:
                    continue
                for value in values[key]:
                    if value not in own[key]:
                        yield key, value, "other_direction"
        for key in contrast_keys:
            for value in catalog[key]:
                if value not in own[key]:
                    yield key, value, "catalog"

    def widen(index: int, reason: str) -> bool:
        rest = sum(counts) - counts[index]
        tried: set[tuple[str, str]] = set()
        for key, value, source in candidates(index):
            if (key, value) in tried:
                continue
            tried.add((key, value))
            trial = {**chosen[index], key: [*chosen[index][key], value]}
            count = _combination_count(trial, factor_keys, constraints)
            if count <= counts[index] or rest + count > MAX_PLAN_COMBINATIONS:
                continue
            chosen[index] = trial
            counts[index] = count
            widened.append({
                "direction": f"dir-{index + 1}",
                "title": draft.directions[index].title,
                "factor": key,
                "value": value,
                "value_label": labels.get((key, value), value),
                "source": source,
                "reason": reason,
            })
            return True
        return False

    if contrast_keys:
        for index in range(len(chosen)):
            if all(len(chosen[index][key]) < 2 for key in contrast_keys):
                widen(index, "single_valued")
        while sum(counts) < MIN_PLAN_COMBINATIONS:
            order = sorted(range(len(chosen)), key=lambda index: (counts[index], index))
            if not any(widen(index, "below_minimum") for index in order):
                break

    directions = [
        replace(direction, factor_values={**dict(direction.factor_values or {}), **chosen[index]}) if any(
            item["direction"] == f"dir-{index + 1}" for item in widened
        ) else direction
        for index, direction in enumerate(draft.directions)
    ]
    return replace(
        draft,
        directions=directions,
        evidence={**dict(draft.evidence or {}), "auto_widened": widened, "drafted_combinations": drafted},
    )


@dataclass
class NormalizedPlan:
    """A draft with stable ids, ready to persist.

    ``factors`` is the recipe's factor catalog (``[{key, label, values:
    [{id, label, positive_fragments, negative_fragments}]}]``);
    ``directions`` carry ``factor_values`` as value ids; ``constraints`` use
    value ids.
    """

    factors: list[dict]
    directions: list[dict]
    constraints: list[dict]


def normalize_plan_draft(draft: PlanDraft) -> NormalizedPlan:
    if not draft.directions:
        raise DraftInvalid("no_directions", "the draft has no direction")
    if len(draft.directions) > MAX_DIRECTIONS:
        raise DraftInvalid("too_many_directions", f"at most {MAX_DIRECTIONS} directions")
    if len(draft.factors) > MAX_FACTORS:
        raise DraftInvalid("too_many_factors", f"at most {MAX_FACTORS} factors")

    catalog: list[dict] = []
    value_ids: dict[tuple[str, str], str] = {}
    for factor in draft.factors:
        key = str(factor.key or "").strip()
        if not FACTOR_KEY_PATTERN.match(key) or key in RESERVED_FACTOR_KEYS:
            raise DraftInvalid("factor_key_invalid", f"factor key {key!r} is not a valid id")
        if any(item["key"] == key for item in catalog):
            raise DraftInvalid("duplicate_factor", f"factor {key!r} appears twice")
        if not factor.values or len(factor.values) > MAX_VALUES_PER_FACTOR:
            raise DraftInvalid(
                "factor_values_invalid", f"factor {key!r} needs 1-{MAX_VALUES_PER_FACTOR} values",
            )
        values = []
        for index, value in enumerate(factor.values, start=1):
            value_key = str(value.key or "").strip()
            label = clean_text(value.label, MAX_LABEL_CHARS)
            if not value_key or not label:
                raise DraftInvalid("factor_value_invalid", f"factor {key!r} has a value without key or label")
            if (key, value_key) in value_ids:
                raise DraftInvalid("duplicate_factor_value", f"factor {key!r} repeats value {value_key!r}")
            value_id = f"{key}-{index}"
            value_ids[(key, value_key)] = value_id
            values.append({
                "id": value_id,
                "label": label,
                "positive_fragments": clean_fragments(value.positive_fragments) or [label],
                "negative_fragments": clean_fragments(value.negative_fragments),
            })
        catalog.append({"key": key, "label": clean_text(factor.label, MAX_LABEL_CHARS) or key, "values": values})

    factor_keys = [item["key"] for item in catalog]
    directions: list[dict] = []
    for ordinal, direction in enumerate(draft.directions):
        title = clean_text(direction.title, MAX_TITLE_CHARS)
        if not title:
            raise DraftInvalid("direction_title_missing", f"direction {ordinal + 1} has no title")
        mapped: dict[str, list[str]] = {}
        for key in factor_keys:
            refs = list(dict.fromkeys((direction.factor_values or {}).get(key) or []))
            if not refs:
                raise DraftInvalid(
                    "direction_factor_missing", f"direction {title!r} has no value for factor {key!r}",
                )
            ids = []
            for ref in refs:
                value_id = value_ids.get((key, str(ref)))
                if value_id is None:
                    raise DraftInvalid(
                        "unknown_factor_value", f"direction {title!r} names unknown {key} value {ref!r}",
                    )
                ids.append(value_id)
            mapped[key] = ids
        unknown = set(direction.factor_values or {}) - set(factor_keys)
        if unknown:
            raise DraftInvalid("unknown_factor", f"direction {title!r} names unknown factors {sorted(unknown)}")
        directions.append({
            "direction_key": f"dir-{ordinal + 1}",
            "ordinal": ordinal,
            "title": title,
            "what_it_tests": clean_text(direction.what_it_tests, MAX_TEXT_CHARS),
            "hypothesis": clean_text(direction.hypothesis, MAX_TEXT_CHARS),
            "scene_description": clean_text(direction.scene_description, MAX_TEXT_CHARS),
            "copy": {
                "headline": clean_text(direction.headline, 200),
                "primary_text": clean_text(direction.primary_text, MAX_TEXT_CHARS),
            },
            "factor_values": mapped,
            "fragments": {
                "positive": clean_fragments(direction.positive_fragments),
                "negative": clean_fragments(direction.negative_fragments),
            },
        })

    constraints: list[dict] = []
    for raw in draft.constraints or []:
        try:
            a = value_ids[(str(raw["factor_a"]), str(raw["value_a"]))]
            b = value_ids[(str(raw["factor_b"]), str(raw["value_b"]))]
        except (KeyError, TypeError):
            raise DraftInvalid("constraint_invalid", f"constraint {raw!r} names unknown factor values") from None
        if raw["factor_a"] == raw["factor_b"]:
            raise DraftInvalid("constraint_invalid", "a constraint must relate two different factors")
        constraints.append({
            "type": "excludes",
            "factor_a": str(raw["factor_a"]), "value_a": a,
            "factor_b": str(raw["factor_b"]), "value_b": b,
        })
    return NormalizedPlan(factors=catalog, directions=directions, constraints=constraints)
