"""Combination planner for Generation previews (Issue #157, I-03 of Epic #154).

Owns the three draft decisions the page used to write but the server never
read: the combination strategy (full product vs. deterministic coverage
sampling), the dimension-template constraints published with a template, and
the operator's manual exclusion rules. :func:`plan_combinations` applies them
in a fixed order and returns everything the preview freezes and shows::

    full product -> template constraints -> exclusion rules -> sampling

Sampling runs last so a ``coverage`` cap is filled with combinations that are
actually eligible (the acceptance criterion is "cap=12 -> 12 x variants API
calls", which a later exclusion pass would silently break).

Strategy shape
--------------
The canonical shape is ``{"kind": "full"}`` or ``{"kind": "coverage", "cap": n}``.
The pre-#157 UI wrote ``{"mode": "full" | "sampled", "sample_cap": n}``; that
shape is still read (``sampled`` maps to ``coverage``) and reported back as
``deprecated_shape`` so the page can rewrite it. Any other kind is rejected
instead of collapsing to a single default combination, which is what the old
``_enumerate_combinations`` did and what hid the bug.

``coverage`` is a port of the legacy ``app.pipeline._coverage_sample`` greedy:
cover every single dimension value first, then as many value *pairs* as
possible, until ``cap`` combinations are picked. It is fully deterministic
for a given candidate order, which in turn is the sorted-dimension Cartesian
product with values in template order.

Constraints
-----------
``requires`` / ``excludes`` reuse :func:`app.library.templates._collect_violations`
(the solver that validates a template at publish time) against each
combination's template-local ``{dimension_id: value_id}`` cells.
``max_occurrence`` adds no behaviour under ``one_per_dimension`` -- the only
cardinality the contract allows -- exactly as the template design spec states.

I-21 (#163) extends this module with test-plan planning; keep the public
functions below stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product as cartesian

from app.library.templates import COMBINATION_LIMIT, _collect_violations

STRATEGY_FULL = "full"
STRATEGY_COVERAGE = "coverage"
STRATEGY_KINDS = (STRATEGY_FULL, STRATEGY_COVERAGE)

#: Legacy ``mode`` values written by the pre-#157 draft page.
_LEGACY_MODE_TO_KIND = {"full": STRATEGY_FULL, "sampled": STRATEGY_COVERAGE}

#: ``exclusion_reason`` stamped on a combination a template constraint dropped.
CONSTRAINT_EXCLUSION_REASON = "template_constraint"
#: Fallback ``exclusion_reason`` for a manual rule without its own ``reason``.
DEFAULT_RULE_REASON = "excluded_by_rule"


class PlannerError(ValueError):
    """Carries a machine-readable ``code`` alongside the human ``detail``.

    ``app.generation.preview`` re-raises it as ``PreviewError`` so the HTTP
    layer keeps one problem-detail path for every preview admission failure.
    """

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class StrategyDecision:
    """The strategy actually applied, plus how it was requested."""

    kind: str
    cap: int | None
    deprecated_shape: bool
    requested: dict

    def as_recipe(self) -> dict:
        """Canonical shape frozen into the recipe (legacy shape never leaks)."""
        applied = {"kind": self.kind}
        if self.cap is not None:
            applied["cap"] = self.cap
        return applied


@dataclass
class CombinationPlan:
    strategy: StrategyDecision
    #: Combinations the preview will run (after constraints, rules, sampling).
    selected: list[dict]
    #: Combinations a template constraint dropped (``exclusion_reason`` set).
    constraint_dropped: list[dict] = field(default_factory=list)
    #: Combinations a manual rule excluded (``exclusion_reason`` set).
    rule_excluded: list[dict] = field(default_factory=list)
    #: Rules that could never match, written back as ``exclusions.invalid``.
    invalid_rules: list[dict] = field(default_factory=list)
    #: Normalised rules that were applied.
    applied_rules: list[dict] = field(default_factory=list)
    full_count: int = 0
    candidate_count: int = 0

    def strategy_applied(self) -> dict:
        return {
            **self.strategy.as_recipe(),
            "deprecated_shape": self.strategy.deprecated_shape,
            "full_count": self.full_count,
            "candidate_count": self.candidate_count,
            "selected_count": len(self.selected),
        }


# --------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------


def normalize_strategy(raw: dict | None) -> StrategyDecision:
    """Resolve a draft's ``combination_strategy`` into the strategy to apply.

    Accepts the canonical ``{kind, cap?}`` shape and the legacy
    ``{mode, sample_cap?}`` shape. Keys the planner does not own
    (``planner_version``, ``requested_variant_count``, ``batch_draft_ids`` ...)
    are ignored, never rejected -- other modules stash state there.
    """
    requested = dict(raw or {})
    deprecated_shape = False

    if "kind" in requested:
        kind = requested.get("kind")
        cap = requested.get("cap")
    elif "mode" in requested:
        deprecated_shape = True
        mode = requested.get("mode")
        if mode not in _LEGACY_MODE_TO_KIND:
            raise PlannerError(
                "combination_strategy_invalid",
                f"Unknown legacy combination_strategy.mode {mode!r}; "
                "use {kind: 'full' | 'coverage', cap}",
            )
        kind = _LEGACY_MODE_TO_KIND[mode]
        cap = requested.get("sample_cap")
    else:
        kind = STRATEGY_FULL
        cap = None

    if kind not in STRATEGY_KINDS:
        raise PlannerError(
            "combination_strategy_invalid",
            f"Unknown combination_strategy.kind {kind!r}; expected one of {list(STRATEGY_KINDS)}",
        )

    if kind == STRATEGY_FULL:
        return StrategyDecision(kind, None, deprecated_shape, requested)

    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise PlannerError(
            "combination_strategy_invalid",
            "combination_strategy.kind 'coverage' requires a positive integer cap",
        )
    return StrategyDecision(kind, cap, deprecated_shape, requested)


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------


def dimension_value_identity(dimension_key: str, raw: object) -> tuple[str, dict | None]:
    """Project one selector value into display data plus governed identity.

    Scalar selector values remain supported for ordinary Generation, but they
    intentionally carry no concept identity and therefore cannot later enter
    an Experiment.  An Experiment-capable Generation uses an explicit object
    with ``concept_key_id``; no concept is inferred from a label.
    """
    if not isinstance(raw, dict):
        return str(raw), None
    display = raw.get("value", raw.get("name", raw.get("value_id", "")))
    display_value = str(display).strip()
    concept_key_id = str(raw.get("concept_key_id") or "").strip()
    if not concept_key_id:
        return display_value, None
    if not display_value:
        raise PlannerError(
            "concept_value_missing",
            f"Dimension {dimension_key!r} has a concept_key_id but no value",
        )
    value = {key: value for key, value in raw.items() if key != "concept_key_id"}
    return display_value, {
        "dimension_key": dimension_key,
        "concept_key_id": concept_key_id,
        "value": value,
    }


def enumerate_full(dimensions: dict) -> list[dict]:
    """Cartesian product of ``dimensions`` (``{key: [values...]}``).

    Dimension keys are iterated sorted and values in declared order, so the
    output order -- which coverage sampling's tie-breaking depends on -- is a
    pure function of the frozen selectors. Returns the single ``default``
    combination when there are no dimensions. Raises
    ``combination_limit_exceeded`` before expanding more than
    :data:`app.library.templates.COMBINATION_LIMIT` combinations.
    """
    if not dimensions:
        return [{"fingerprint": "default", "dimensions": {}, "concepts": [], "cells": {}}]

    dim_names = sorted(dimensions.keys())
    axes: list[list[tuple[str, str, dict | None, object]]] = []
    total = 1
    for dim_name in dim_names:
        values = dimensions[dim_name]
        if not isinstance(values, list):
            values = [values]
        axis = []
        for raw in values:
            display_value, concept = dimension_value_identity(dim_name, raw)
            axis.append((dim_name, display_value, concept, raw))
        axes.append(axis)
        total *= max(len(axis), 1)
        if total > COMBINATION_LIMIT:
            raise PlannerError(
                "combination_limit_exceeded",
                f"Full combination count exceeds the limit of {COMBINATION_LIMIT}",
            )

    combos: list[dict] = []
    for cells in cartesian(*axes):
        dims: dict[str, str] = {}
        concepts: list[dict] = []
        local_cells: dict[str, tuple[str, str]] = {}
        for dim_name, display_value, concept, raw in cells:
            dims[dim_name] = display_value
            if concept is not None:
                concepts.append(dict(concept))
            if isinstance(raw, dict) and raw.get("template_entry_id"):
                # Template-local identity the constraint solver matches on.
                local_cells[dim_name] = (
                    str(raw.get("template_entry_id")),
                    str(raw.get("dimension_id") or ""),
                    str(raw.get("value_id") or ""),
                )
        fingerprint = ":".join(f"{k}={dims[k]}" for k in sorted(dims))
        for concept in concepts:
            concept["stable_dimension_signature"] = fingerprint
        combos.append({
            "fingerprint": fingerprint,
            "dimensions": dims,
            "concepts": concepts,
            "cells": local_cells,
        })
    return combos


# --------------------------------------------------------------------------
# Template constraints
# --------------------------------------------------------------------------


def apply_template_constraints(
    combinations: list[dict], template_constraints: list[dict] | None
) -> tuple[list[dict], list[dict]]:
    """Split ``combinations`` into (dropped, kept) by published template constraints.

    ``template_constraints`` is the list ``preview._materialize_dimension_
    template_concepts`` freezes: one entry per selected template with
    ``template_entry_id`` and that version's ``constraints``. Each dropped
    combination carries ``exclusion_reason="template_constraint"`` and the
    solver's violation strings under ``constraint_violations`` (the SkipReason
    the Library contract §9.3 requires).
    """
    by_entry = {
        str(item.get("template_entry_id")): list(item.get("constraints") or [])
        for item in (template_constraints or [])
        if isinstance(item, dict) and item.get("constraints")
    }
    if not by_entry:
        return [], combinations

    dropped: list[dict] = []
    kept: list[dict] = []
    for combo in combinations:
        local: dict[str, dict[str, str]] = {}
        for entry_id, dimension_id, value_id in (combo.get("cells") or {}).values():
            local.setdefault(entry_id, {})[dimension_id] = value_id
        violations: list[str] = []
        for entry_id, constraints in by_entry.items():
            cells = local.get(entry_id)
            if not cells:
                continue
            violations.extend(_collect_violations(cells, constraints))
        if violations:
            combo["exclusion_reason"] = CONSTRAINT_EXCLUSION_REASON
            combo["constraint_violations"] = violations
            dropped.append(combo)
        else:
            kept.append(combo)
    return dropped, kept


# --------------------------------------------------------------------------
# Manual exclusion rules
# --------------------------------------------------------------------------


def normalize_exclusions(
    exclusions: dict | None, dimensions: dict
) -> tuple[list[dict], list[dict]]:
    """Return (applicable rules, invalid entries) for ``exclusions``.

    A rule is ``{"dimensions": {<dimension key>: <value>}, "reason": str}``.
    A rule is *invalid* -- reported, never silently ignored -- when it cannot
    be parsed, names a dimension that is not part of this preview, or names a
    value that dimension does not offer. Legacy ``exclusions.manual`` strings
    (free text the old page wrote and nothing ever matched) are reported the
    same way so the operator sees they never took effect.
    """
    known_values: dict[str, set[str]] = {}
    for dim_name, values in (dimensions or {}).items():
        if not isinstance(values, list):
            values = [values]
        known_values[dim_name] = {
            dimension_value_identity(dim_name, raw)[0] for raw in values
        }

    rules: list[dict] = []
    invalid: list[dict] = []
    raw_rules = (exclusions or {}).get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        invalid.append({
            "rule": raw_rules,
            "code": "unparseable_rule",
            "detail": "exclusions.rules must be a list",
        })
        raw_rules = []

    for raw_rule in raw_rules:
        problem = _rule_problem(raw_rule, known_values)
        if problem is not None:
            invalid.append({"rule": raw_rule, **problem})
            continue
        rules.append({
            "dimensions": {str(k): str(v) for k, v in raw_rule["dimensions"].items()},
            "reason": str(raw_rule.get("reason") or DEFAULT_RULE_REASON),
        })

    for legacy in (exclusions or {}).get("manual") or []:
        invalid.append({
            "rule": legacy,
            "code": "legacy_manual_exclusion",
            "detail": "free-text exclusions are never matched; rebuild it as a dimension rule",
        })
    return rules, invalid


def _rule_problem(raw_rule: object, known_values: dict[str, set[str]]) -> dict | None:
    if not isinstance(raw_rule, dict):
        return {"code": "unparseable_rule", "detail": "a rule must be an object with dimensions"}
    match = raw_rule.get("dimensions")
    if not isinstance(match, dict) or not match:
        return {"code": "unparseable_rule", "detail": "rule.dimensions must be a non-empty object"}
    for key, value in match.items():
        if key not in known_values:
            return {
                "code": "unknown_dimension",
                "detail": f"dimension {key!r} is not part of this draft's selection",
            }
        if isinstance(value, (dict, list)):
            return {"code": "unparseable_rule", "detail": f"value for {key!r} must be a scalar"}
        if str(value) not in known_values[key]:
            return {
                "code": "unknown_value",
                "detail": f"dimension {key!r} has no value {str(value)!r}",
            }
    return None


def apply_exclusions(
    combinations: list[dict], rules: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Split combinations into (excluded, included) based on normalised rules."""
    if not rules:
        return [], combinations
    excluded: list[dict] = []
    included: list[dict] = []
    for combo in combinations:
        reason = _match_exclusion(combo, rules)
        if reason:
            combo["exclusion_reason"] = reason
            excluded.append(combo)
        else:
            included.append(combo)
    return excluded, included


def _match_exclusion(combo: dict, rules: list[dict]) -> str | None:
    dims = combo.get("dimensions", {})
    for rule in rules:
        match_dims = rule.get("dimensions", {})
        if match_dims and all(dims.get(k) == str(v) for k, v in match_dims.items()):
            return rule.get("reason") or DEFAULT_RULE_REASON
    return None


# --------------------------------------------------------------------------
# Coverage sampling (port of app.pipeline._coverage_sample)
# --------------------------------------------------------------------------


def coverage_sample(combinations: list[dict], cap: int) -> list[dict]:
    """Greedy coverage sample: every single value first, then as many value
    pairs as possible, up to ``cap`` combinations.

    This is the lightweight orthogonal-design approximation the legacy
    pipeline used: every dimension value appears at least once (when ``cap``
    allows) and the picked combinations differ from each other as much as
    possible. Ties resolve to the earliest candidate, so the result is a
    deterministic function of ``combinations`` order.
    """
    if cap >= len(combinations):
        return list(combinations)

    def singles(combo: dict) -> set[tuple[str, str]]:
        return set(combo.get("dimensions", {}).items())

    def pairs(combo: dict) -> set[tuple[tuple[str, str], tuple[str, str]]]:
        cells = sorted(combo.get("dimensions", {}).items())
        return {(a, b) for i, a in enumerate(cells) for b in cells[i + 1:]}

    covered_singles: set = set()
    covered_pairs: set = set()
    remaining = list(combinations)
    picked: list[dict] = []
    while remaining and len(picked) < cap:
        best_idx = 0
        best_score = (-1, -1)
        for idx, combo in enumerate(remaining):
            score = (
                len(singles(combo) - covered_singles),
                len(pairs(combo) - covered_pairs),
            )
            if score > best_score:
                best_idx, best_score = idx, score
        best = remaining.pop(best_idx)
        picked.append(best)
        covered_singles |= singles(best)
        covered_pairs |= pairs(best)
    return picked


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def plan_combinations(
    selectors: dict,
    strategy: dict | None,
    exclusions: dict | None,
) -> CombinationPlan:
    """Enumerate, constrain, exclude and sample a draft's combinations.

    ``selectors`` is the preview's *resolved* selector dict (templates already
    materialised into ``dimensions`` plus ``dimension_template_constraints``).
    """
    decision = normalize_strategy(strategy)
    dimensions = selectors.get("dimensions") or {}
    full = enumerate_full(dimensions)

    constraint_dropped, candidates = apply_template_constraints(
        full, selectors.get("dimension_template_constraints")
    )
    rules, invalid_rules = normalize_exclusions(exclusions, dimensions)
    rule_excluded, candidates = apply_exclusions(candidates, rules)

    if decision.kind == STRATEGY_COVERAGE and decision.cap is not None:
        selected = coverage_sample(candidates, decision.cap)
    else:
        selected = list(candidates)

    # ``cells`` is the solver's working identity, not part of the frozen
    # combination (the stable fingerprint + concepts are); drop it so recipe
    # hashes and RunItem identities stay free of planner internals.
    for combo in full:
        combo.pop("cells", None)

    return CombinationPlan(
        strategy=decision,
        selected=selected,
        constraint_dropped=constraint_dropped,
        rule_excluded=rule_excluded,
        invalid_rules=invalid_rules,
        applied_rules=rules,
        full_count=len(full),
        candidate_count=len(candidates),
    )


# ==========================================================================
# Test-plan planning (Issue #163, I-21 of Epic #154)
# ==========================================================================
#
# Front-flow contract §5.3: the planner that turns a test plan's directions
# into combinations is deterministic code, not an LLM.
#
# A plan is a set of *directions*. Each direction declares, per factor, the
# factor values it wants to test (``{"scene": ["scene-1", "scene-2"],
# "angle": ["angle-1"]}``); its space is the Cartesian product of those
# values, and the plan space is the union of the direction spaces (directions
# never share a combination: ``direction`` is itself a dimension). Optional
# plan-level ``excludes`` constraints prune the space first, exactly like a
# published template's constraints prune a preview.
#
# Three scales pick from that space:
#
# * ``sample`` -- one combination per direction. Directions are visited in
#   ordinal order; each takes the combination that adds the most factor
#   values (then value pairs) not yet shown by an earlier direction's sample,
#   earliest candidate on ties.
# * ``small``  -- every single value covered, then as many value pairs as
#   possible, capped at 12 (``coverage_sample``).
# * ``full``   -- the whole space when it has at most 32 combinations;
#   otherwise the constraint-pruned space is coverage-sampled down to 32.
#
# Every result lists the combinations it does *not* test (count and a
# per-direction summary) so the plan card can say so. Enumeration order is a
# pure function of the directions' ordinals, the sorted factor keys and the
# declared value order, so the same plan always yields the same selection.

PLAN_SCALE_SAMPLE = "sample"
PLAN_SCALE_SMALL = "small"
PLAN_SCALE_FULL = "full"
PLAN_SCALES = (PLAN_SCALE_SAMPLE, PLAN_SCALE_SMALL, PLAN_SCALE_FULL)

#: Upper bound on combinations a ``small`` test generates.
SMALL_TEST_CAP = 12
#: Upper bound on combinations a ``full`` test generates.
FULL_TEST_CAP = 32

#: Template dimension id carrying the direction itself.
DIRECTION_DIMENSION_ID = "direction"
TEST_PLANNER_VERSION = "test-plan-planner/v1"

#: Per-direction and per-plan space ceilings: a plan beyond these is a
#: drafting mistake, not something to enumerate.
MAX_DIRECTION_SPACE = 64
MAX_PLAN_SPACE = 256


@dataclass(frozen=True)
class PlanDirectionSpace:
    """One direction's factor design: ``key`` is the stable direction id."""

    key: str
    factor_values: dict[str, list[str]]


@dataclass(frozen=True)
class PlanCombination:
    direction: str
    #: ``(factor_key, value_id)`` pairs sorted by factor key.
    cells: tuple[tuple[str, str], ...]

    @property
    def signature(self) -> str:
        """Plan-local combination signature (``direction=..|factor=value``)."""
        parts = [f"{DIRECTION_DIMENSION_ID}={self.direction}"]
        parts.extend(f"{factor}={value}" for factor, value in self.cells)
        return "|".join(parts)

    def dimensions(self) -> dict[str, str]:
        return {DIRECTION_DIMENSION_ID: self.direction, **dict(self.cells)}


@dataclass
class StagePlan:
    """What one scale of a plan generates and what it leaves untested."""

    scale: str
    cap: int | None
    selected: list[PlanCombination]
    space: list[PlanCombination]
    constraint_excluded: list[PlanCombination]

    @property
    def untested(self) -> list[PlanCombination]:
        chosen = {combo.signature for combo in self.selected}
        return [combo for combo in self.space if combo.signature not in chosen]

    def untested_summary(self, direction_titles: dict[str, str] | None = None) -> dict:
        """Count + per-direction breakdown + one human sentence for the card."""
        titles = direction_titles or {}
        untested = self.untested
        per_direction: dict[str, int] = {}
        for combo in untested:
            per_direction[combo.direction] = per_direction.get(combo.direction, 0) + 1
        order = list(dict.fromkeys(combo.direction for combo in self.space))
        by_direction = [
            {
                "direction_id": key,
                "title": titles.get(key, key),
                "untested_count": per_direction[key],
            }
            for key in order
            if per_direction.get(key)
        ]
        if not untested:
            text = f"方案中的 {len(self.space)} 个组合都会被测到。"
        else:
            detail = "，".join(
                f"「{item['title']}」{item['untested_count']} 个" for item in by_direction
            )
            text = (
                f"共 {len(self.space)} 个组合，本次测 {len(self.selected)} 个，"
                f"未测到 {len(untested)} 个（{detail}）。"
            )
        if self.constraint_excluded:
            text += f"另有 {len(self.constraint_excluded)} 个组合因约束被排除。"
        return {
            "count": len(untested),
            "space_count": len(self.space),
            "constraint_excluded_count": len(self.constraint_excluded),
            "by_direction": by_direction,
            "examples": [combo.dimensions() for combo in untested[:5]],
            "summary": text,
        }


def _direction_space(direction: PlanDirectionSpace) -> list[PlanCombination]:
    axes = []
    total = 1
    for factor in sorted(direction.factor_values):
        values = list(dict.fromkeys(direction.factor_values[factor] or []))
        if not values:
            raise PlannerError(
                "direction_factor_empty",
                f"Direction {direction.key!r} has no value for factor {factor!r}",
            )
        axes.append([(factor, value) for value in values])
        total *= len(values)
        if total > MAX_DIRECTION_SPACE:
            raise PlannerError(
                "direction_space_too_large",
                f"Direction {direction.key!r} spans more than {MAX_DIRECTION_SPACE} combinations",
            )
    return [PlanCombination(direction.key, tuple(cells)) for cells in cartesian(*axes)]


def _violates(combo: PlanCombination, constraints: list[dict]) -> bool:
    dims = combo.dimensions()
    for constraint in constraints:
        if (
            dims.get(str(constraint.get("factor_a"))) == str(constraint.get("value_a"))
            and dims.get(str(constraint.get("factor_b"))) == str(constraint.get("value_b"))
        ):
            return True
    return False


def enumerate_plan_space(
    directions: list[PlanDirectionSpace],
    constraints: list[dict] | None = None,
) -> tuple[list[PlanCombination], list[PlanCombination]]:
    """Return (space, constraint_excluded) for ``directions`` in ordinal order.

    Every direction must declare the same factor keys -- a combination needs
    a value for every dimension of the template it materialises into.
    ``constraints`` are ``{"factor_a", "value_a", "factor_b", "value_b"}``
    excludes pairs (``direction`` is a valid factor key there).
    """
    if not directions:
        return [], []
    keys = [direction.key for direction in directions]
    if len(set(keys)) != len(keys):
        raise PlannerError("duplicate_direction", "Direction ids must be unique")
    factor_keys = set(directions[0].factor_values)
    for direction in directions[1:]:
        if set(direction.factor_values) != factor_keys:
            raise PlannerError(
                "direction_factor_mismatch",
                f"Direction {direction.key!r} must declare the same factors as the others "
                f"({sorted(factor_keys)})",
            )
    rules = list(constraints or [])
    space: list[PlanCombination] = []
    excluded: list[PlanCombination] = []
    for direction in directions:
        for combo in _direction_space(direction):
            (excluded if _violates(combo, rules) else space).append(combo)
    if len(space) + len(excluded) > MAX_PLAN_SPACE:
        raise PlannerError(
            "plan_space_too_large",
            f"The plan spans more than {MAX_PLAN_SPACE} combinations; remove factor values",
        )
    return space, excluded


def _as_coverage_items(combos: list[PlanCombination]) -> list[dict]:
    return [{"dimensions": combo.dimensions(), "_combo": combo} for combo in combos]


def _sample_one_per_direction(space: list[PlanCombination]) -> list[PlanCombination]:
    def singles(combo: PlanCombination) -> set[tuple[str, str]]:
        return set(combo.cells)

    def pairs(combo: PlanCombination) -> set:
        cells = list(combo.cells)
        return {(a, b) for i, a in enumerate(cells) for b in cells[i + 1:]}

    covered_singles: set = set()
    covered_pairs: set = set()
    picked: list[PlanCombination] = []
    for direction in dict.fromkeys(combo.direction for combo in space):
        best: PlanCombination | None = None
        best_score = (-1, -1)
        for combo in space:
            if combo.direction != direction:
                continue
            score = (
                len(singles(combo) - covered_singles),
                len(pairs(combo) - covered_pairs),
            )
            if score > best_score:
                best, best_score = combo, score
        if best is None:  # pragma: no cover - every listed direction has a combo
            continue
        picked.append(best)
        covered_singles |= singles(best)
        covered_pairs |= pairs(best)
    return picked


def plan_test_stage(
    directions: list[PlanDirectionSpace],
    scale: str,
    constraints: list[dict] | None = None,
) -> StagePlan:
    """Select the combinations one ``scale`` of a test plan generates."""
    if scale not in PLAN_SCALES:
        raise PlannerError(
            "plan_scale_invalid", f"Unknown scale {scale!r}; expected one of {list(PLAN_SCALES)}"
        )
    space, excluded = enumerate_plan_space(directions, constraints)
    if scale == PLAN_SCALE_SAMPLE:
        selected, cap = _sample_one_per_direction(space), None
    elif scale == PLAN_SCALE_SMALL:
        cap = SMALL_TEST_CAP
        selected = [item["_combo"] for item in coverage_sample(_as_coverage_items(space), cap)]
    else:
        cap = FULL_TEST_CAP
        selected = (
            list(space)
            if len(space) <= cap
            else [item["_combo"] for item in coverage_sample(_as_coverage_items(space), cap)]
        )
    return StagePlan(
        scale=scale, cap=cap, selected=selected, space=space, constraint_excluded=excluded,
    )


def plan_all_stages(
    directions: list[PlanDirectionSpace],
    constraints: list[dict] | None = None,
) -> dict[str, StagePlan]:
    """Run :func:`plan_test_stage` for every scale (one enumeration each)."""
    return {scale: plan_test_stage(directions, scale, constraints) for scale in PLAN_SCALES}
