"""Immutable value objects for the pure-function Scale evaluator.

Spec: docs/superpowers/specs/2026-09-08-scale-section-2-pure-function-evaluator.md
§2.2 "Immutable input contracts", §2.3 "Snapshot validation and error
modes", and §2.6 "Immutable output contracts".

Every dataclass here is `@dataclass(frozen=True)` and every member is itself
immutable: a frozen dataclass, `tuple`, `Decimal`, `str`, `int`, `bool`,
`None`, `date`, or `datetime`. No `MappingProxyType`, `dict`, `float`, or
`list` appears in any field. These are pure value objects only — no
validation, defaulting, or business logic lives here; Phase 0 of
`evaluate_scale()` (Task 9) owns validating instances of these types and
raises `InvalidScaleSnapshot` with a stable code on failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.scale.types import (
    AdviceSeverity,
    AdviceType,
    BreakdownSuppressionReason,
    BreakdownType,
    DailyEvidenceState,
    DataQualityImpact,
    DataQualityIssueCode,
    BreakdownEvidenceState,
    DeliveryDiagnosticState,
    EvidenceMaturity,
    DataQualityState,
    MetaMode,
    MetricContractType,
    PriorStreakAbsenceReason,
    ScaleHealth,
    SignalState,
    WindowRole,
)

# ---------------------------------------------------------------------------
# §2.3 Errors
# ---------------------------------------------------------------------------


class InvalidScaleSnapshot(ValueError):
    """Deterministic, stably-coded rejection of a structurally invalid
    `ScaleEvidenceSnapshot`. Spec §2.3 enumerates the required codes,
    including at least: `snapshot_not_canonical`, `algorithm_policy_mismatch`,
    `algorithm_version_unavailable`, `invalid_window_boundary`,
    `missing_or_duplicate_local_day`, `invalid_or_unsorted_contract_set`,
    `invalid_or_unsorted_breakdown`, `currency_mismatch`, `negative_metric`,
    `clicks_exceed_impressions`, `invalid_exact_window`,
    `invalid_prior_evaluation`, `invalid_freshness_reference`,
    `invalid_decimal`. `invalid_policy` rejects a frozen Policy that lacks a
    field its algorithm version reads (#132, #133).
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Canonical scalars and key/value evidence (§2.2)
# ---------------------------------------------------------------------------

CanonicalScalar = bool | int | str | Decimal | date | datetime | None


@dataclass(frozen=True)
class CanonicalKeyValue:
    key: str
    value: CanonicalScalar


# ---------------------------------------------------------------------------
# Daily resampling unit (§2.2 "Daily resampling unit")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyMetricValues:
    impressions: int
    link_clicks: int
    purchases: Decimal
    spend: Decimal
    purchase_value: Decimal
    delivered: bool


@dataclass(frozen=True)
class DailyObservation:
    local_date: date
    evidence_state: DailyEvidenceState
    metrics: DailyMetricValues | None


@dataclass(frozen=True)
class MetricWindow:
    role: WindowRole
    start_date: date
    end_date_exclusive: date
    daily: tuple[DailyObservation, ...]


# ---------------------------------------------------------------------------
# Exact-window evidence (§2.2 "Exact-window evidence")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExactWindowMetrics:
    role: WindowRole
    start_date: date
    end_date_exclusive: date
    reach: int | None
    frequency: Decimal | None
    observation_contract_version_id: str
    evidence_hash: str


# ---------------------------------------------------------------------------
# Breakdown evidence (§2.2 "Breakdown evidence")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakdownSegment:
    breakdown_type: BreakdownType
    segment_key: str
    evidence_state: BreakdownEvidenceState
    daily: tuple[DailyObservation, ...] | None
    suppression_reason: BreakdownSuppressionReason | None


# ---------------------------------------------------------------------------
# Data-quality evidence (§2.2 "Breakdown evidence" / quality)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataQualityLocator:
    contract_type: MetricContractType
    window_role: WindowRole | None
    local_date: date | None
    breakdown_type: BreakdownType | None
    segment_key: str | None
    source_evidence_hash: str


@dataclass(frozen=True)
class DataQualityItem:
    code: DataQualityIssueCode
    impact: DataQualityImpact
    locator: DataQualityLocator
    details: tuple[CanonicalKeyValue, ...]


# ---------------------------------------------------------------------------
# Policy, target, contracts, and delivery state
# (§2.2 "Policy, target, contracts, and delivery state")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScalePolicySnapshot:
    """Freezes every numerical and methodological choice used by the
    evaluator (spec §2.2). Field groups map 1:1 onto the spec's bullet list;
    the spec does not give this type a literal code block, so grouping and
    naming below are Task 7's concretization of that prose, made to be
    extended rather than reinterpreted by later tasks."""

    # Identity and algorithm selection.
    policy_version_id: str
    algorithm_version: str

    # Decimal precision and rounding mode.
    decimal_precision: int
    decimal_rounding_mode: str

    # Wilson/Newcombe confidence level and its exact Decimal z value.
    confidence_level: Decimal
    confidence_z: Decimal

    # Complete-day, active-day, impression, click, purchase, and spend floors.
    # `spend_floor` is the platform economic floor for one 7-day window, in
    # the snapshot's currency. A Policy that sets the floor per currency
    # (#132) freezes only the snapshot currency's entry, and `None` when that
    # currency is not in its table; only `beta-v2` accepts `None`.
    complete_day_floor: int
    active_day_floor: int
    impression_floor: int
    click_floor: int
    purchase_floor: int
    spend_floor: Decimal | None

    # Frequency, CTR, CPA, ROAS, CPM, and delivery thresholds.
    frequency_level_threshold: Decimal
    frequency_growth_threshold: Decimal
    ctr_decline_threshold: Decimal
    ctr_stability_tolerance: Decimal
    cvr_decline_threshold: Decimal
    cvr_stability_tolerance: Decimal
    cpm_increase_threshold: Decimal
    delivery_decline_threshold: Decimal
    cpa_worsening_threshold: Decimal
    roas_worsening_threshold: Decimal

    # Required consecutive-hit counts.
    consecutive_composite_fatigue_hits_required: int
    consecutive_healthy_evaluations_required: int

    # Bootstrap PRNG version, draw count, minimum valid-draw ratio, and
    # percentile rule.
    bootstrap_prng_version: str
    bootstrap_draw_count: int
    bootstrap_min_valid_draw_ratio: Decimal
    bootstrap_lower_percentile: Decimal
    bootstrap_upper_percentile: Decimal

    # Breakdown spend-share and sample gates.
    breakdown_min_spend_share: Decimal
    breakdown_min_sample_impressions: int
    breakdown_min_sample_clicks: int
    breakdown_min_sample_purchases: int

    # Benjamini-Hochberg family definition and FDR limit.
    fdr_limit: Decimal

    # Quality-warning permissions and streak-reset reason codes.
    quality_warning_permitted_codes: tuple[DataQualityIssueCode, ...]
    streak_reset_reason_codes: tuple[str, ...]

    # Advice-construction values needed to construct stable candidates.
    advice_construction_parameters: tuple[CanonicalKeyValue, ...]

    # Consecutive evaluations a placement or country segment must hit before
    # it forms `review_delivery_mix` (contract §13, #133). `None` for a
    # Policy that predates the field; snapshots of schema v1-v3 leave it out
    # of their hash payload (`app.scale.evidence._hash_payload`).
    consecutive_breakdown_hits_required: int | None = None


@dataclass(frozen=True)
class ScaleTargetSnapshot:
    """Optional Owner Target. When present, `currency` must equal the
    snapshot currency, and any `*_threshold_override` is only ever allowed
    to tighten (never loosen) the corresponding Policy threshold."""

    target_version_id: str
    currency: str
    target_cpa: Decimal | None
    breakeven_roas: Decimal | None
    minimum_economic_spend: Decimal

    frequency_level_threshold_override: Decimal | None
    frequency_growth_threshold_override: Decimal | None
    ctr_decline_threshold_override: Decimal | None
    cvr_decline_threshold_override: Decimal | None
    cpm_increase_threshold_override: Decimal | None
    delivery_decline_threshold_override: Decimal | None
    cpa_worsening_threshold_override: Decimal | None
    roas_worsening_threshold_override: Decimal | None


@dataclass(frozen=True)
class MetricContractRef:
    """Ordered by `(contract_type, ...)` in the snapshot's
    `metric_contracts` tuple (spec §2.2)."""

    contract_type: MetricContractType
    scale_contract_version: str
    observation_contract_generation: str
    observation_contract_version_id: str
    contract_hash: str
    connection_id: str
    meta_mode: MetaMode
    scenario_version_id: str | None
    timezone_name: str
    currency: str


@dataclass(frozen=True)
class DeliveryStatusEvent:
    """One entry in `DeliveryConfigSnapshot.status_history`: the applicable
    status history for the frozen windows (spec §2.2)."""

    observed_at: datetime
    entity_level: str
    status: str


@dataclass(frozen=True)
class DeliveryConfigSnapshot:
    baseline_configuration_hash: str
    current_configuration_hash: str
    effective_campaign_status: str
    effective_ad_set_status: str
    effective_ad_status: str
    status_history: tuple[DeliveryStatusEvent, ...]
    attribution_setting: str
    diagnostic_state: DeliveryDiagnosticState
    diagnostic_detail: tuple[CanonicalKeyValue, ...]


# ---------------------------------------------------------------------------
# Consecutive-hit lineage (§2.2 "Consecutive-hit lineage")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreakCounter:
    signal_key: str
    consecutive_hits: int


@dataclass(frozen=True)
class StreakCompatibility:
    policy_version_id: str
    target_version_id: str | None
    metric_contract_set_hash: str
    configuration_baseline_hash: str
    data_continuity_generation: str
    canonical_hash: str


@dataclass(frozen=True)
class PriorStreakState:
    evaluation_id: str
    evaluated_local_day: date
    compatibility: StreakCompatibility
    health: ScaleHealth | None
    healthy_evaluations: int
    counters: tuple[StreakCounter, ...]


# ---------------------------------------------------------------------------
# Complete snapshot (§2.2 "Complete snapshot")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaleEvidenceSnapshot:
    snapshot_id: str
    canonical_hash: str
    canonical_schema_version: str
    episode_id: str
    meta_mode: MetaMode
    meta_connection_id: str
    scenario_version_id: str | None
    timezone_name: str
    currency: str
    policy: ScalePolicySnapshot
    target: ScaleTargetSnapshot | None
    metric_contract_set_hash: str
    metric_contracts: tuple[MetricContractRef, ...]
    delivery_config: DeliveryConfigSnapshot
    current_7d: MetricWindow
    baseline_7d: MetricWindow
    current_exact_7d: ExactWindowMetrics
    baseline_exact_7d: ExactWindowMetrics
    rolling_exact_14d: ExactWindowMetrics
    breakdowns: tuple[BreakdownSegment, ...]
    data_quality_issues: tuple[DataQualityItem, ...]
    complete_local_days_in_episode: int
    active_delivery_days_in_episode: int
    data_continuity_generation: str
    prior_streak: PriorStreakState | None
    prior_streak_absence_reason: PriorStreakAbsenceReason | None
    last_complete_local_day: date
    last_sync_at: datetime
    freshness_reference_at: datetime
    as_of_local_day: date


# ---------------------------------------------------------------------------
# §2.6 Immutable output contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalResult:
    signal_id: str
    state: SignalState
    expected: Decimal | None
    observed: Decimal | None
    interval_lower: Decimal | None
    interval_upper: Decimal | None
    threshold: Decimal | None
    p_value: Decimal | None
    adjusted_q_value: Decimal | None
    reason_code: str


@dataclass(frozen=True)
class AdviceEvidenceItem:
    key: str
    value: CanonicalScalar


@dataclass(frozen=True)
class AdviceCandidate:
    advice_type: AdviceType
    reason_signature: str
    severity: AdviceSeverity
    signal_ids: tuple[str, ...]
    evidence: tuple[AdviceEvidenceItem, ...]


@dataclass(frozen=True)
class StreakReset:
    streak_id: str
    previous_value: int
    reason_code: str


@dataclass(frozen=True)
class StreakState:
    compatibility: StreakCompatibility
    healthy_evaluations: int
    counters: tuple[StreakCounter, ...]
    resets: tuple[StreakReset, ...]


@dataclass(frozen=True)
class ScaleEvaluation:
    snapshot_canonical_hash: str
    algorithm_version: str
    maturity: EvidenceMaturity
    data_quality_state: DataQualityState
    health: ScaleHealth | None
    health_reason: str
    signals: tuple[SignalResult, ...]
    advice_candidates: tuple[AdviceCandidate, ...]
    streak_state: StreakState
    result_hash: str
