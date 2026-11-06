"""Pure, deterministic experiment judge.

The sole interface is ``judge(snapshot) -> result``.  It performs no I/O,
reads no clock or environment, and derives every bootstrap seed from the input.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from decimal import Decimal, InvalidOperation
from statistics import NormalDist
from typing import Any


ALGORITHM_VERSION = "judge-v1"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _fmt(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(number.quantize(Decimal("0.000000000001")), "f")


def _seed(snapshot_hash: str, entry_id: str) -> int:
    return int(hashlib.sha256(f"{snapshot_hash}:{entry_id}".encode()).hexdigest()[:16], 16)


def _wilson(clicks: int, impressions: int, alpha: float) -> tuple[Decimal, Decimal, Decimal]:
    p = clicks / impressions
    z = NormalDist().inv_cdf(1 - alpha / 2)
    denominator = 1 + z * z / impressions
    center = (p + z * z / (2 * impressions)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * impressions)) / impressions) / denominator
    return Decimal(str(p)), Decimal(str(max(0.0, center - margin))), Decimal(str(min(1.0, center + margin)))


def _quantile(values: list[Decimal | None], probability: float) -> Decimal | None:
    ordered = sorted(values, key=lambda value: (value is None, value or Decimal("0")))
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def _bootstrap_ratios(
    daily: list[dict], *, numerator: str, denominator: str, samples: int, seed: int,
    alpha: float, zero_denominator_is_infinite: bool = False,
) -> tuple[Decimal, Decimal | None, Decimal | None] | None:
    if not daily:
        return None
    randomizer = random.Random(seed)
    ratios: list[Decimal | None] = []
    size = len(daily)
    for _ in range(samples):
        selected = [daily[randomizer.randrange(size)] for _ in range(size)]
        num = sum((_decimal(row.get(numerator)) for row in selected), Decimal("0"))
        den = sum((_decimal(row.get(denominator)) for row in selected), Decimal("0"))
        if den > 0:
            ratios.append(num / den)
        elif zero_denominator_is_infinite and num > 0:
            ratios.append(None)
    if not ratios:
        return None
    point_num = sum((_decimal(row.get(numerator)) for row in daily), Decimal("0"))
    point_den = sum((_decimal(row.get(denominator)) for row in daily), Decimal("0"))
    if point_den <= 0:
        return None
    return point_num / point_den, _quantile(ratios, alpha / 2), _quantile(ratios, 1 - alpha / 2)


def _inconclusive(entry_id: str, metric: str, reasons: list[str], totals: dict) -> dict:
    return {
        "entry_id": entry_id,
        "verdict": "inconclusive",
        "metric": metric,
        "estimate": None,
        "interval_low": None,
        "interval_high": None,
        "reason_codes": sorted(set(reasons)),
        "totals": totals,
    }


def judge(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Judge an immutable snapshot; equal input produces equal JSON-safe output."""
    config = snapshot["experiment"]["config"]
    metric = str(config["primary_metric"]).lower()
    if metric not in {"ctr", "cpa", "roas"}:
        raise ValueError(f"unsupported primary_metric: {metric}")
    alpha = float(config.get("alpha", "0.05"))
    bootstrap_samples = int(config.get("bootstrap_samples", 2000))
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    if bootstrap_samples < 200:
        raise ValueError("bootstrap_samples must be at least 200")

    results: list[dict] = []
    for entry in sorted(snapshot["entries"], key=lambda item: (item.get("position", 0), item["id"])):
        daily = list(entry.get("daily") or [])
        impressions = sum(int(row.get("impressions") or 0) for row in daily)
        clicks = sum(int(row.get("clicks") or 0) for row in daily)
        spend = sum((_decimal(row.get("spend")) for row in daily), Decimal("0"))
        purchases = sum((_decimal(row.get("purchases")) for row in daily), Decimal("0"))
        purchase_value = sum((_decimal(row.get("purchase_value")) for row in daily), Decimal("0"))
        totals = {
            "days": len(daily), "impressions": impressions, "clicks": clicks,
            "spend": _fmt(spend), "purchases": _fmt(purchases), "purchase_value": _fmt(purchase_value),
        }
        reasons = list(entry.get("data_quality_reasons") or [])
        if impressions < 0 or clicks < 0 or clicks > impressions or spend < 0 or purchases < 0 or purchase_value < 0:
            reasons.append("invalid_metric_values")
        if len(daily) < int(config.get("min_days", 1)):
            reasons.append("insufficient_days")
        if impressions < int(config.get("min_impressions", 0)):
            reasons.append("insufficient_impressions")

        if metric == "ctr":
            if clicks < int(config.get("min_clicks", 0)):
                reasons.append("insufficient_clicks")
            if impressions <= 0 or reasons:
                results.append(_inconclusive(entry["id"], metric, reasons or ["no_impressions"], totals))
                continue
            estimate, low, high = _wilson(clicks, impressions, alpha)
            threshold = _decimal(config["ctr_threshold"])
            verdict = "win" if low >= threshold else "lose" if high < threshold else "learn"
        else:
            if purchases < _decimal(config.get("min_purchases", 1)):
                reasons.append("insufficient_purchases")
            if spend <= 0:
                reasons.append("no_spend")
            if reasons:
                results.append(_inconclusive(entry["id"], metric, reasons, totals))
                continue
            if metric == "cpa":
                interval = _bootstrap_ratios(
                    daily, numerator="spend", denominator="purchases", samples=bootstrap_samples,
                    seed=_seed(snapshot["snapshot_hash"], entry["id"]), alpha=alpha,
                    zero_denominator_is_infinite=True,
                )
                threshold = _decimal(config["cpa_threshold"])
                if interval is None:
                    results.append(_inconclusive(entry["id"], metric, ["bootstrap_undefined"], totals))
                    continue
                estimate, low, high = interval
                verdict = "win" if high is not None and high <= threshold else \
                    "lose" if low is not None and low > threshold else "learn"
            else:
                interval = _bootstrap_ratios(
                    daily, numerator="purchase_value", denominator="spend", samples=bootstrap_samples,
                    seed=_seed(snapshot["snapshot_hash"], entry["id"]), alpha=alpha,
                )
                threshold = _decimal(config["roas_threshold"])
                if interval is None:
                    results.append(_inconclusive(entry["id"], metric, ["bootstrap_undefined"], totals))
                    continue
                estimate, low, high = interval
                verdict = "win" if low is not None and low >= threshold else \
                    "lose" if high is not None and high < threshold else "learn"

        results.append({
            "entry_id": entry["id"], "verdict": verdict, "metric": metric,
            "estimate": _fmt(estimate), "interval_low": _fmt(low), "interval_high": _fmt(high),
            "reason_codes": [], "totals": totals,
        })

    summary = {name: sum(1 for row in results if row["verdict"] == name)
               for name in ("win", "learn", "lose", "inconclusive")}
    return {"algorithm_version": ALGORITHM_VERSION, "entries": results, "summary": summary}


def canonical_hash(value: dict[str, Any]) -> str:
    from ..platform.canonical import canonical_hash as _shared
    return _shared(value)
