"""Deterministic statistical primitives for the Scale evaluator.

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md
§2.4 "Deterministic arithmetic".

This module provides the sole source of randomness and statistical
computation used by the Scale evaluator:

- `splitmix64_stream` / `rejection_sample_index`: the frozen, versioned,
  modulo-bias-free PRNG (`bootstrap-prng-v1`). The stdlib `random` module and
  any other source of randomness are forbidden anywhere in Scale.
- `wilson_interval` / `newcombe_method10`: the CTR confidence-interval
  primitives.
- `day_block_bootstrap` / `nearest_rank_percentile`: the day-block bootstrap
  used for CPA/ROAS and target-relative segment effects.
- `benjamini_hochberg`: the FDR correction used for breakdown significance
  filtering.

All arithmetic is Decimal. No value is ever converted to `float`. Every
function here is pure: no database, network, filesystem, clock, or logging
access, and no mutation of its inputs. Precision and rounding come from
whatever Decimal context is active in the caller (Phase 0 of `evaluate_scale`
establishes the Policy's local Decimal context before calling into this
module; `wilson_interval`/`newcombe_method10` take the Policy's exact `z`
directly since they do not otherwise need the Policy object).
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, ROUND_CEILING
from typing import Callable, Iterator

from app.scale._contracts import DailyMetricValues, DailyObservation, ScalePolicySnapshot

MASK = 0xFFFFFFFFFFFFFFFF

# The frozen `bootstrap-prng-v1` domain separator (spec §2.4).
_BOOTSTRAP_DOMAIN_SEPARATOR = b"scale-bootstrap-prng-v1"
_NUL = b"\x00"


class BootstrapUnavailable(Exception):
    """Raised by `day_block_bootstrap` when the valid-draw ratio falls below
    `policy.bootstrap_min_valid_draw_ratio` (spec §2.4: "If the valid-draw
    ratio is below the Policy floor, the signal is `unavailable`"). Callers
    (the Phase 4 evaluator, Task 9) catch this and set the corresponding
    `SignalResult.state` to `unavailable` rather than treating it as a
    structural input error."""

    def __init__(self, signal_id: str, valid_draws: int, total_draws: int):
        self.signal_id = signal_id
        self.valid_draws = valid_draws
        self.total_draws = total_draws
        super().__init__(
            f"bootstrap unavailable for {signal_id!r}: "
            f"{valid_draws}/{total_draws} valid draws"
        )


# ---------------------------------------------------------------------------
# SplitMix64 PRNG (spec §2.4 "the frozen unsigned transition")
# ---------------------------------------------------------------------------


def splitmix64_stream(seed: int) -> Iterator[int]:
    """Yield an unbounded stream of unsigned 64-bit words using the frozen
    SplitMix64-v1 transition. `seed` is masked to 64 bits before use so any
    non-negative Python int seed is accepted."""
    state = seed & MASK
    while True:
        state = (state + 0x9E3779B97F4A7C15) & MASK
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        yield (z ^ (z >> 31)) & MASK


def bootstrap_prng_seed(snapshot_hash: str, signal_key: str) -> int:
    """Derive the `bootstrap-prng-v1` seed for one signal/segment stream
    (spec §2.4): the first unsigned 64 bits, big-endian, of
    `SHA-256(domain_separator || NUL || snapshot_hash || NUL || signal_key)`.

    `snapshot_hash` must be the lowercase ASCII hexadecimal SHA-256 digest
    string; `signal_key` is encoded as UTF-8.
    """
    message = (
        _BOOTSTRAP_DOMAIN_SEPARATOR
        + _NUL
        + snapshot_hash.encode("ascii")
        + _NUL
        + signal_key.encode("utf-8")
    )
    digest = hashlib.sha256(message).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def rejection_sample_index(stream: Iterator[int], n: int) -> int:
    """Return a uniformly distributed index in `[0, n)` from `stream`,
    consuming as many words as rejection sampling requires so the result has
    no modulo bias (spec §2.4). `n` must be a positive integer."""
    if n <= 0:
        raise ValueError("rejection_sample_index: n must be positive")
    threshold = (2**64) - ((2**64) % n)
    while True:
        word = next(stream)
        if word < threshold:
            return word % n


# ---------------------------------------------------------------------------
# Wilson score interval and Newcombe method 10 (spec §2.4 "CTR comparison")
# ---------------------------------------------------------------------------


def wilson_interval(x: int, n: int, z: Decimal) -> tuple[Decimal, Decimal]:
    """The Wilson score interval for `x` successes out of `n` trials at
    Decimal `z`, without continuity correction, evaluated in the exact order
    written in spec §2.4."""
    if n <= 0:
        raise ValueError("wilson_interval: n must be positive")
    x_d = Decimal(x)
    n_d = Decimal(n)
    p = x_d / n_d
    z2 = z * z
    denom = Decimal(1) + (z2 / n_d)
    center = (p + (z2 / (Decimal(2) * n_d))) / denom
    radius = (
        z * (((p * (Decimal(1) - p)) / n_d) + (z2 / (Decimal(4) * n_d * n_d))).sqrt()
    ) / denom
    lower = max(Decimal(0), center - radius)
    upper = min(Decimal(1), center + radius)
    return lower, upper


def newcombe_method10(
    x1: int, n1: int, x2: int, n2: int, z: Decimal
) -> tuple[Decimal, Decimal]:
    """Newcombe method-10 confidence interval for the difference between two
    independent proportions, frozen as `d = baseline_ctr - current_ctr`
    (spec §2.4): `(x1, n1)` is the baseline window, `(x2, n2)` the current
    window. A positive `d_lower` means the interval supports decline."""
    baseline_ctr = Decimal(x1) / Decimal(n1)
    current_ctr = Decimal(x2) / Decimal(n2)
    lb, ub = wilson_interval(x1, n1, z)
    lc, uc = wilson_interval(x2, n2, z)
    d = baseline_ctr - current_ctr
    d_lower = d - (((baseline_ctr - lb) ** 2) + ((uc - current_ctr) ** 2)).sqrt()
    d_upper = d + (((ub - baseline_ctr) ** 2) + ((current_ctr - lc) ** 2)).sqrt()
    return d_lower, d_upper


# ---------------------------------------------------------------------------
# Nearest-rank percentile (spec §2.4 "nearest-rank percentile without
# interpolation")
# ---------------------------------------------------------------------------


def nearest_rank_percentile(sorted_values: list[Decimal], percentile: Decimal) -> Decimal:
    """The nearest-rank percentile of an ascending-sorted `sorted_values`,
    without interpolation. For rational `percentile = a / b`, the zero-based
    index is `max(0, ceil(a * n / b) - 1)` where `n = len(sorted_values)`
    (spec §2.4). Passing `percentile` as the already-reduced Decimal ratio
    (e.g. `Decimal("25") / Decimal("1000")`) is equivalent to that formula
    because `ceil(percentile * n) == ceil(a * n / b)` for exact rational
    `percentile`."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("nearest_rank_percentile: sorted_values must be non-empty")
    raw_index = (percentile * Decimal(n)).to_integral_value(rounding=ROUND_CEILING) - 1
    index = max(0, int(raw_index))
    index = min(index, n - 1)
    return sorted_values[index]


# ---------------------------------------------------------------------------
# Day-block bootstrap (spec §2.4 "Deterministic day-block bootstrap")
# ---------------------------------------------------------------------------

StatisticFn = Callable[
    [tuple[DailyMetricValues, ...], tuple[DailyMetricValues, ...]], "Decimal | None"
]


def day_block_bootstrap(
    baseline_days: tuple[DailyObservation, ...],
    current_days: tuple[DailyObservation, ...],
    statistic_fn: StatisticFn,
    n_draws: int,
    seed_hash: str,
    signal_id: str,
    policy: ScalePolicySnapshot,
) -> tuple[Decimal, Decimal]:
    """The day-block bootstrap confidence interval for one signal or segment
    (spec §2.4).

    `baseline_days` and `current_days` must already be the present daily
    observations in canonical local-date order (the caller — Phase 4 of the
    evaluator — is responsible for never entering the bootstrap when a
    missing slot has made either window quality-blocked). For each of
    `n_draws` draws, the evaluator resamples with replacement independently:
    every baseline slot in order, then every current slot in order, each
    drawn via `rejection_sample_index` on one signal-keyed `bootstrap-prng-v1`
    stream. `statistic_fn` receives the resampled `(baseline_metrics,
    current_metrics)` tuples and returns the direction-normalized effect for
    that draw, or `None` when its denominator is unavailable (excluded from
    the valid-draw set).

    Raises `BootstrapUnavailable` when the valid-draw ratio falls below
    `policy.bootstrap_min_valid_draw_ratio`; otherwise returns the nearest-
    rank `(lower, upper)` bounds of the sorted valid effects at
    `policy.bootstrap_lower_percentile` / `policy.bootstrap_upper_percentile`.
    """
    n_baseline = len(baseline_days)
    n_current = len(current_days)
    if n_baseline == 0 or n_current == 0:
        raise ValueError(
            "day_block_bootstrap: baseline_days and current_days must be non-empty"
        )

    stream = splitmix64_stream(bootstrap_prng_seed(seed_hash, signal_id))

    valid_draws: list[Decimal] = []
    for _ in range(n_draws):
        baseline_sample = tuple(
            baseline_days[rejection_sample_index(stream, n_baseline)].metrics
            for _ in range(n_baseline)
        )
        current_sample = tuple(
            current_days[rejection_sample_index(stream, n_current)].metrics
            for _ in range(n_current)
        )
        effect = statistic_fn(baseline_sample, current_sample)
        if effect is not None:
            valid_draws.append(effect)

    valid_ratio = Decimal(len(valid_draws)) / Decimal(n_draws)
    if valid_ratio < policy.bootstrap_min_valid_draw_ratio:
        raise BootstrapUnavailable(signal_id, len(valid_draws), n_draws)

    valid_draws.sort()
    lower = nearest_rank_percentile(valid_draws, policy.bootstrap_lower_percentile)
    upper = nearest_rank_percentile(valid_draws, policy.bootstrap_upper_percentile)
    return lower, upper


# ---------------------------------------------------------------------------
# Benjamini-Hochberg FDR correction (spec §2.4 "Breakdown hypothesis and
# FDR")
# ---------------------------------------------------------------------------


def benjamini_hochberg(
    p_values: list[tuple[str, Decimal]], fdr_limit: Decimal
) -> list[tuple[str, Decimal, bool]]:
    """Benjamini-Hochberg FDR correction over one sample-eligible family
    (spec §2.4). Sorted by `(p_value, segment_key)` for deterministic tie
    breaking. For sorted one-based rank `i` in a family of size `m`, the raw
    `q_i = min(1, p_i * m / i)`; the walk from `m` down to `1` then replaces
    each with `min(q_i, q_(i+1))`. Returns `(segment_key, adjusted_q_value,
    passed)` in the same `(p_value, segment_key)` order, where `passed` is
    `adjusted_q_value <= fdr_limit`."""
    m = len(p_values)
    if m == 0:
        return []

    ordered = sorted(p_values, key=lambda item: (item[1], item[0]))
    raw_q = [
        min(Decimal(1), p * Decimal(m) / Decimal(i))
        for i, (_key, p) in enumerate(ordered, start=1)
    ]

    adjusted_q = list(raw_q)
    for i in range(m - 2, -1, -1):
        adjusted_q[i] = min(adjusted_q[i], adjusted_q[i + 1])

    return [
        (key, q, q <= fdr_limit)
        for (key, _p), q in zip(ordered, adjusted_q)
    ]
