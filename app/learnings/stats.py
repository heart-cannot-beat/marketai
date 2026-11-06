"""Dimension-value learnings: deterministic main effects with honest intervals.

Pure functions, no I/O, no clock: the same units and thresholds always give
the same JSON-safe result (bootstrap seeds derive from the input hash).

The unit of replication is the **asset** (one AssetVersion, summed over its
ads and days). Impressions inside one creative are not independent draws of
"the dimension value" -- a value tested on one image is indistinguishable
from that image -- so every comparison needs at least
:attr:`Thresholds.min_assets` assets on both sides, and every interval is
the hull of two intervals:

* a **parametric** one on the log rate ratio (Katz for CTR,
  ``1/c1 - 1/n1 + 1/c2 - 1/n2``; Poisson for CPA, ``1/C1 + 1/C2``) that
  covers the sampling noise of clicks and conversions, and
* a seeded **cluster bootstrap** that resamples assets within each side and
  covers creative-to-creative heterogeneity. With few assets the bootstrap
  spread is too narrow by ``(m - 1) / m`` in variance, so its log distance
  from the point estimate is widened by ``sqrt(m / (m - 1))`` (``m`` = the
  smaller side's asset count).

Comparisons (blueprint §6 "v0: 按属性分组 + 置信区间"): each value of a
factor against the *pooled* other values of that factor. For a factor with
``k > 2`` values the ``k`` comparisons share a Bonferroni-adjusted level
(``alpha / k``), so the reported "95%" interval is simultaneous within the
factor. Factors are separate questions and are not adjusted against each
other.

Confounding: pooled main effects are only unbiased when the other factors
are balanced across the compared groups. For every other factor (and the
direction) the total-variation distance between the two groups' weight
distributions (impressions for CTR, spend for CPA) is computed; at or above
:attr:`Thresholds.confounded_tvd` the verdict is withheld
(``insufficient_data`` + flag ``confounded``), at or above
:attr:`Thresholds.imbalance_tvd` it is kept with flag ``imbalanced``.

Verdicts: ``winner`` / ``loser`` only when the whole interval sits on one
side of "no difference"; ``no_difference`` when the data is sufficient but
the interval spans it; ``insufficient_data`` otherwise. Never a forced
winner (v9 §3.3).
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from statistics import NormalDist
from typing import Callable, Iterable

from app.experiments.judge import _bootstrap_ratios, _seed, _wilson

ALGORITHM_VERSION = "learnings-v1"

VERDICTS = ("winner", "loser", "no_difference", "insufficient_data")
DECIDED = frozenset({"winner", "loser", "no_difference"})
METRICS = ("ctr", "cpa")


@dataclass(frozen=True)
class Thresholds:
    """Configurable minimum samples and decision levels (documented defaults).

    * CTR needs, on *both* sides of a comparison, >= ``min_impressions``
      impressions, >= ``min_clicks`` link clicks and >= ``min_assets``
      assets with impressions.
    * CPA needs, on both sides, >= ``min_conversions`` conversions, spend
      > 0 and >= ``min_assets`` assets with known spend and conversions.
    """

    alpha: float = 0.05
    bootstrap_samples: int = 2000
    min_impressions: int = 1000
    min_clicks: int = 30
    min_conversions: int = 10
    min_assets: int = 2
    imbalance_tvd: float = 0.25
    confounded_tvd: float = 0.5

    def as_dict(self) -> dict:
        return {key: str(value) for key, value in asdict(self).items()}


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Unit:
    """One attributed asset, summed over its observations.

    ``spend`` / ``conversions`` are ``None`` when any of its observations
    did not report them (then the asset is left out of CPA, never zeroed).
    """

    unit_id: str
    direction: str
    cells: dict[str, str]
    impressions: int
    clicks: int
    spend: Decimal | None = None
    conversions: Decimal | None = None
    ads: int = 0


@dataclass
class Side:
    units: list[Unit] = field(default_factory=list)

    @property
    def impressions(self) -> int:
        return sum(unit.impressions for unit in self.units)

    @property
    def clicks(self) -> int:
        return sum(unit.clicks for unit in self.units)

    @property
    def spend(self) -> Decimal:
        return sum((unit.spend or Decimal("0") for unit in self.units), Decimal("0"))

    @property
    def conversions(self) -> Decimal:
        return sum((unit.conversions or Decimal("0") for unit in self.units), Decimal("0"))

    @property
    def ads(self) -> int:
        return sum(unit.ads for unit in self.units)

    def n(self) -> dict:
        return {
            "impressions": self.impressions,
            "clicks": self.clicks,
            "spend": _money(self.spend),
            "conversions": _num(self.conversions),
            "assets": len(self.units),
            "ads": self.ads,
        }


# ---------------------------------------------------------------------------
# Formatting (JSON-safe strings; no binary floats in snapshots)
# ---------------------------------------------------------------------------


def _ratio(value: float | Decimal | None) -> str | None:
    if value is None:
        return None
    number = float(value)
    if math.isinf(number) or math.isnan(number):
        return None
    return format(number, ".6f")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _num(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized == normalized.to_integral() else format(value.quantize(Decimal("0.01")), "f")


def percent(value: float | None, *, signed: bool = True) -> str:
    """``0.38`` -> ``+38%``; one decimal below 1% (``+0.4%``)."""
    if value is None:
        return "∞"
    scaled = value * 100
    text = f"{abs(scaled):.1f}" if abs(scaled) < 1 else f"{abs(scaled):.0f}"
    if not signed:
        return f"{text}%"
    if text in ("0", "0.0"):
        return "0%"
    return f"{'-' if scaled < 0 else '+'}{text}%"


def thousands(value: int | Decimal | str) -> str:
    number = Decimal(str(value))
    if number == number.to_integral():
        return f"{int(number):,}"
    return f"{number:,.2f}"


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def _eligible(units: Iterable[Unit], metric: str) -> list[Unit]:
    if metric == "ctr":
        return [unit for unit in units if unit.impressions > 0]
    return [unit for unit in units if unit.spend is not None and unit.conversions is not None]


def _rate(side: Side, metric: str) -> float | None:
    if metric == "ctr":
        return side.clicks / side.impressions if side.impressions else None
    conversions = float(side.conversions)
    if conversions <= 0:
        return math.inf if side.spend > 0 else None
    return float(side.spend) / conversions


def _shortfall(side: Side, metric: str, th: Thresholds) -> list[str]:
    missing = []
    if len(side.units) < th.min_assets:
        missing.append("assets")
    if metric == "ctr":
        if side.impressions < th.min_impressions:
            missing.append("impressions")
        if side.clicks < th.min_clicks:
            missing.append("clicks")
    else:
        if side.conversions < th.min_conversions:
            missing.append("conversions")
        if side.spend <= 0:
            missing.append("spend")
    return missing


def tvd(group: Side, rest: Side, key: Callable[[Unit], str | None], metric: str) -> float:
    """Total-variation distance of the two sides' weight over ``key``'s values."""

    def weights(side: Side) -> dict[str, float]:
        out: dict[str, float] = {}
        for unit in side.units:
            value = key(unit)
            if value is None:
                continue
            weight = float(unit.impressions) if metric == "ctr" else float(unit.spend or 0)
            out[value] = out.get(value, 0.0) + weight
        total = sum(out.values())
        return {k: v / total for k, v in out.items()} if total > 0 else {}

    a, b = weights(group), weights(rest)
    if not a or not b:
        return 0.0
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def _pairs(units: list[Unit], metric: str) -> list[tuple[float, float]]:
    """(numerator, denominator) per unit: clicks / impressions or spend / conversions."""
    if metric == "ctr":
        return [(float(unit.clicks), float(unit.impressions)) for unit in units]
    return [(float(unit.spend or 0), float(unit.conversions or 0)) for unit in units]


def _resampled_ratio(rng: random.Random, pairs: list[tuple[float, float]], metric: str) -> float | None:
    num = den = 0.0
    for numerator, denominator in rng.choices(pairs, k=len(pairs)):
        num += numerator
        den += denominator
    if den <= 0:
        return math.inf if metric == "cpa" and num > 0 else None
    return num / den


def cluster_bootstrap(
    group: list[Unit], rest: list[Unit], metric: str, *, samples: int, seed: int, alpha: float,
) -> tuple[float, float, float] | None:
    """Percentile interval of rate(group) / rate(rest), resampling assets.

    Returns ``(low, high, valid_fraction)`` in ratio space; ``inf`` marks an
    unbounded side (a resample with zero conversions or clicks).
    """
    rng = random.Random(seed)
    ratios: list[float] = []
    group_pairs, rest_pairs = _pairs(group, metric), _pairs(rest, metric)
    for _ in range(samples):
        g = _resampled_ratio(rng, group_pairs, metric)
        r = _resampled_ratio(rng, rest_pairs, metric)
        if g is None or r is None:
            continue
        if r == 0:
            ratios.append(math.inf if g > 0 else 1.0)
        elif math.isinf(r):
            ratios.append(0.0 if not math.isinf(g) else 1.0)
        else:
            ratios.append(g / r)
    if not ratios:
        return None
    return _quantile(ratios, alpha / 2), _quantile(ratios, 1 - alpha / 2), len(ratios) / samples


def _widen(point: float, bound: float, factor: float, upward: bool) -> float:
    """Widen a bootstrap bound's log distance from the point by ``factor``."""
    if point <= 0 or math.isinf(point):
        return bound
    if upward:
        if math.isinf(bound):
            return bound
        distance = max(0.0, math.log(bound) - math.log(point)) if bound > 0 else 0.0
        return math.exp(math.log(point) + factor * distance)
    if bound <= 0:
        return 0.0
    distance = max(0.0, math.log(point) - math.log(bound))
    return math.exp(math.log(point) - factor * distance)


# ---------------------------------------------------------------------------
# One comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BalanceKey:
    key: str
    label: str
    getter: Callable[[Unit], str | None]


def compare(
    units: list[Unit],
    *,
    in_group: Callable[[Unit], bool],
    comparable: Callable[[Unit], bool],
    metric: str,
    balance: list[BalanceKey],
    tests: int,
    seed: int,
    th: Thresholds = DEFAULT_THRESHOLDS,
) -> dict:
    """``in_group`` units against the other ``comparable`` units on ``metric``."""
    eligible = [unit for unit in _eligible(units, metric) if comparable(unit)]
    group = Side([unit for unit in eligible if in_group(unit)])
    rest = Side([unit for unit in eligible if not in_group(unit)])
    alpha_adj = th.alpha / max(1, tests)
    result: dict = {
        "metric": metric,
        "verdict": "insufficient_data",
        "estimate": None,
        "interval_low": None,
        "interval_high": None,
        "baseline": _ratio(_rate(rest, metric)) if rest.units else None,
        "lift": None,
        "lift_low": None,
        "lift_high": None,
        "confidence": f"{1 - th.alpha:.2f}",
        "adjusted_alpha": f"{alpha_adj:.4f}",
        "reasons": [],
        "flags": [],
        "balance": None,
        "n": group.n(),
        "baseline_n": rest.n(),
        "shortfall": {},
    }

    # The value's own rate and interval (judge.py statistics).
    if group.units:
        if metric == "ctr" and group.impressions > 0:
            estimate, low, high = _wilson(group.clicks, group.impressions, th.alpha)
            result.update(estimate=_ratio(estimate), interval_low=_ratio(low), interval_high=_ratio(high))
        elif metric == "cpa" and group.spend > 0:
            rows = [{"spend": str(unit.spend), "purchases": str(unit.conversions)} for unit in group.units]
            interval = _bootstrap_ratios(
                rows, numerator="spend", denominator="purchases", samples=th.bootstrap_samples,
                seed=seed ^ 0x5EED, alpha=th.alpha, zero_denominator_is_infinite=True,
            )
            if interval is not None:
                estimate, low, high = interval
                result.update(estimate=_ratio(estimate), interval_low=_ratio(low), interval_high=_ratio(high))

    if not group.units:
        result["reasons"].append("no_data")
        return result
    if not rest.units:
        result["reasons"].append("no_baseline")
        return result
    group_short, rest_short = _shortfall(group, metric, th), _shortfall(rest, metric, th)
    if group_short or rest_short:
        result["reasons"].extend([f"value_low_{item}" for item in group_short] + [f"baseline_low_{item}" for item in rest_short])
        result["shortfall"] = {"value": group_short, "baseline": rest_short}
        return result

    # Balance of the other factors across the two sides.
    worst: tuple[float, BalanceKey] | None = None
    for item in balance:
        distance = tvd(group, rest, item.getter, metric)
        if worst is None or distance > worst[0]:
            worst = (distance, item)
    if worst is not None:
        result["balance"] = {"max_tvd": f"{worst[0]:.4f}", "factor_key": worst[1].key, "factor_label": worst[1].label}

    g_rate, r_rate = _rate(group, metric), _rate(rest, metric)
    if not g_rate or not r_rate or math.isinf(g_rate) or math.isinf(r_rate):
        result["reasons"].append("rate_undefined")
        return result
    point = g_rate / r_rate
    z = NormalDist().inv_cdf(1 - alpha_adj / 2)
    if metric == "ctr":
        variance = (1 / group.clicks - 1 / group.impressions) + (1 / rest.clicks - 1 / rest.impressions)
    else:
        variance = 1 / float(group.conversions) + 1 / float(rest.conversions)
    se = math.sqrt(max(variance, 0.0))
    param_low, param_high = point * math.exp(-z * se), point * math.exp(z * se)

    boot = cluster_bootstrap(
        group.units, rest.units, metric, samples=th.bootstrap_samples, seed=seed, alpha=alpha_adj,
    )
    if boot is None or boot[2] < 0.5:
        result["reasons"].append("bootstrap_unstable")
        return result
    m = min(len(group.units), len(rest.units))
    factor = math.sqrt(m / (m - 1)) if m > 1 else 1.0
    boot_low = _widen(point, boot[0], factor, upward=False)
    boot_high = _widen(point, boot[1], factor, upward=True)
    low, high = min(param_low, boot_low), max(param_high, boot_high)

    result["lift"] = _ratio(point - 1)
    result["lift_low"] = _ratio(low - 1)
    result["lift_high"] = _ratio(high - 1) if not math.isinf(high) else None
    if math.isinf(high):
        result["flags"].append("unbounded_interval")

    if worst is not None and worst[0] >= th.confounded_tvd:
        result["flags"].append("confounded")
        result["reasons"].append("confounded")
        return result
    if worst is not None and worst[0] >= th.imbalance_tvd:
        result["flags"].append("imbalanced")

    better_low, better_high = (low, high) if metric == "ctr" else (1 / high if high else math.inf, 1 / low if low else math.inf)
    # ``better_*`` is the interval of "how much better" (>1 = better) for both metrics.
    if better_low > 1:
        result["verdict"] = "winner"
    elif not math.isinf(better_high) and better_high < 1:
        result["verdict"] = "loser"
    else:
        result["verdict"] = "no_difference"
    return result


# ---------------------------------------------------------------------------
# Sentences (Chinese, contract §3.3)
# ---------------------------------------------------------------------------

_METRIC_NAME = {"ctr": "CTR", "cpa": "CPA"}


def _n_text(comparison: dict) -> str:
    n = comparison["n"]
    if comparison["metric"] == "ctr":
        return f"n={thousands(n['impressions'])} 次展示 / {n['assets']} 张素材"
    return f"n={thousands(n['conversions'])} 次购买 / {n['assets']} 张素材"


def _shortfall_text(comparison: dict, th: Thresholds) -> str:
    n = comparison["n"]
    if comparison["metric"] == "ctr":
        have = f"目前 {thousands(n['impressions'])} 次展示、{thousands(n['clicks'])} 次点击、{n['assets']} 张素材"
        need = f"每组至少 {thousands(th.min_impressions)} 次展示、{th.min_clicks} 次点击、{th.min_assets} 张素材"
    else:
        have = f"目前 {thousands(n['conversions'])} 次购买、{n['assets']} 张素材"
        need = f"每组至少 {th.min_conversions} 次购买、{th.min_assets} 张素材"
    return f"{have}；{need}"


def sentence(label: str, compared_with: str, comparison: dict, th: Thresholds = DEFAULT_THRESHOLDS) -> str:
    """One line a seller can read: verdict, lift, 95% interval and n."""
    metric = comparison["metric"]
    name = _METRIC_NAME[metric]
    verdict = comparison["verdict"]
    if verdict == "insufficient_data":
        reasons = comparison.get("reasons") or []
        if "no_data" in reasons:
            return f"{label}：尚无投放数据"
        if "no_baseline" in reasons:
            return f"{label}：没有可对比的其他取值，尚不能下结论"
        if "confounded" in reasons:
            balance = comparison.get("balance") or {}
            if balance.get("factor_key") == "direction":
                return f"{label}：只出现在部分创意方向里，和方向本身的效果分不开，暂不能单独下结论"
            other = balance.get("factor_label") or "其他因素"
            return f"{label}：总是和「{other}」的特定取值一起出现，效果分不开，暂不能单独下结论"
        return f"{label}：样本不足，尚不能下结论（{_shortfall_text(comparison, th)}）"
    lift = float(comparison["lift"])
    low = float(comparison["lift_low"])
    high = float(comparison["lift_high"]) if comparison["lift_high"] is not None else None
    interval = f"95% 区间 {percent(low)}~{percent(high) if high is not None else '无上限'}，{_n_text(comparison)}"
    caveat = "；其他因素分布不均，仅供参考" if "imbalanced" in (comparison.get("flags") or []) else ""
    if verdict == "no_difference":
        return f"{label} 与 {compared_with} 的 {name} 差异不显著（{interval}{caveat}）"
    direction = "高" if lift > 0 else "低"
    return f"{label} 比 {compared_with} {name} {direction} {percent(lift, signed=False)}（{interval}{caveat}）"


def primary(ctr: dict, cpa: dict) -> dict:
    """The comparison the headline shows.

    A clear CPA result (the business outcome) first, then a clear CTR
    result, then "no difference" on CTR (the tighter interval), then on CPA
    -- so a real CTR winner is never hidden behind a CPA that is merely not
    significant yet.
    """
    for candidate, verdicts in ((cpa, ("winner", "loser")), (ctr, ("winner", "loser")),
                                (ctr, ("no_difference",)), (cpa, ("no_difference",))):
        if candidate["verdict"] in verdicts:
            return candidate
    return ctr


def secondary_note(main: dict, ctr: dict, cpa: dict) -> str:
    """A short clause when the other metric was decided differently."""
    other = cpa if main is ctr else ctr
    if other["verdict"] not in DECIDED or other["verdict"] == main["verdict"]:
        return ""
    name = _METRIC_NAME[other["metric"]]
    if other["verdict"] == "no_difference":
        return f"；{name} 差异尚不显著"
    lift = float(other["lift"])
    return f"；{name} {'高' if lift > 0 else '低'} {percent(lift, signed=False)}"


# ---------------------------------------------------------------------------
# A whole round (one test plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorSpec:
    key: str
    label: str
    values: list[tuple[str, str]]  # (value id, label)


@dataclass(frozen=True)
class RoundInput:
    """Everything one round's analysis reads: the plan's factor catalog, its
    directions (``(key, title)``) and the attributed units."""

    round_id: str
    factors: list[FactorSpec]
    directions: list[tuple[str, str]]
    units: list[Unit]


def crossed_factors(spec: RoundInput) -> set[str]:
    """Factors whose observed values do not depend on the direction.

    A direction is a bundle (copy, hypothesis *and* the factor values it
    declares); the direction comparison is only checked for balance on
    factors that every observed direction spans with the same values.
    """
    by_direction: dict[str, dict[str, set[str]]] = {}
    for unit in spec.units:
        cells = by_direction.setdefault(unit.direction, {})
        for key, value in unit.cells.items():
            cells.setdefault(key, set()).add(value)
    crossed = set()
    for factor in spec.factors:
        sets = [cells.get(factor.key, set()) for cells in by_direction.values()]
        if sets and all(item == sets[0] for item in sets):
            crossed.add(factor.key)
    return crossed


def _both_metrics(
    units: list[Unit], *, in_group, comparable, balance, tests: int, seed_key: str, input_hash: str,
    th: Thresholds,
) -> dict[str, dict]:
    return {
        metric: compare(
            units, in_group=in_group, comparable=comparable, metric=metric, balance=balance,
            tests=tests, seed=_seed(input_hash, f"{seed_key}:{metric}"), th=th,
        )
        for metric in METRICS
    }


def _row(label: str, compared_with: str, metrics: dict[str, dict], th: Thresholds) -> dict:
    main = primary(metrics["ctr"], metrics["cpa"])
    text = sentence(label, compared_with, main, th)
    if main["verdict"] in DECIDED:
        text += secondary_note(main, metrics["ctr"], metrics["cpa"])
    return {
        "compared_with": compared_with,
        "verdict": main["verdict"],
        "verdict_metric": main["metric"],
        "flags": sorted(set(metrics["ctr"]["flags"]) | set(metrics["cpa"]["flags"])),
        "sentence": text,
        "n": metrics["ctr"]["n"],
        "ctr": metrics["ctr"],
        "cpa": metrics["cpa"],
    }


def _quoted(title: str) -> str:
    return title if "「" in title else f"「{title}」"


def analyze_round(spec: RoundInput, *, input_hash: str, th: Thresholds = DEFAULT_THRESHOLDS) -> dict:
    """Per factor value and per direction: CTR and CPA against the rest."""
    units = sorted(spec.units, key=lambda unit: unit.unit_id)
    direction_titles = dict(spec.directions)
    factor_labels = {factor.key: factor.label for factor in spec.factors}

    def cell(key: str) -> Callable[[Unit], str | None]:
        return lambda unit: unit.cells.get(key)

    factors_out = []
    for factor in spec.factors:
        value_labels = dict(factor.values)
        observed = sorted({unit.cells.get(factor.key) for unit in units if unit.cells.get(factor.key) in value_labels})
        tests = len(observed) if len(observed) > 2 else 1
        balance = [BalanceKey("direction", "创意方向", lambda unit: unit.direction)] + [
            BalanceKey(other.key, other.label, cell(other.key)) for other in spec.factors if other.key != factor.key
        ]
        values_out = []
        for value_id, label in factor.values:
            others = [value_labels[v] for v in observed if v != value_id]
            compared_with = others[0] if len(others) == 1 else "其他取值"
            metrics = _both_metrics(
                units,
                in_group=(lambda key, vid: lambda unit: unit.cells.get(key) == vid)(factor.key, value_id),
                comparable=(lambda key, labels: lambda unit: unit.cells.get(key) in labels)(factor.key, value_labels),
                balance=balance, tests=tests, seed_key=f"{spec.round_id}:{factor.key}:{value_id}",
                input_hash=input_hash, th=th,
            )
            values_out.append({"value_id": value_id, "label": label, **_row(label, compared_with, metrics, th)})
        factors_out.append({"key": factor.key, "label": factor.label, "values": values_out})

    crossed = crossed_factors(spec)
    observed_directions = sorted({unit.direction for unit in units if unit.direction in direction_titles})
    direction_tests = len(observed_directions) if len(observed_directions) > 2 else 1
    direction_balance = [BalanceKey(key, factor_labels[key], cell(key)) for key in sorted(crossed)]
    directions_out = []
    for key, title in spec.directions:
        others = [direction_titles[d] for d in observed_directions if d != key]
        compared_with = _quoted(others[0]) if len(others) == 1 else "其他方向"
        metrics = _both_metrics(
            units,
            in_group=(lambda k: lambda unit: unit.direction == k)(key),
            comparable=lambda unit: unit.direction in direction_titles,
            balance=direction_balance, tests=direction_tests, seed_key=f"{spec.round_id}:direction:{key}",
            input_hash=input_hash, th=th,
        )
        directions_out.append({"direction_key": key, "title": title, **_row(_quoted(title), compared_with, metrics, th)})

    total = Side(units)
    cpa_side = Side(_eligible(units, "cpa"))
    cpa_rate = _rate(cpa_side, "cpa") if cpa_side.units else None
    return {
        "round_id": spec.round_id,
        "totals": {
            **total.n(),
            "ctr": _ratio(total.clicks / total.impressions) if total.impressions else None,
            "cpa": _ratio(cpa_rate) if cpa_rate is not None else None,
            "cpa_assets": len(cpa_side.units),
        },
        "factors": factors_out,
        "directions": directions_out,
    }
