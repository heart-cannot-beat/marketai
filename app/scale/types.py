"""Canonical Scale domain enums. Spec §1.2 and Section 2 §2.2."""
from __future__ import annotations

from enum import Enum


class MetaMode(str, Enum):
    CUSTOMER_SANDBOX = "customer_sandbox"
    CONTROLLED_MOCK = "controlled_mock"
    INTERNAL_VALIDATION = "internal_validation"


class EnrollmentLifecycle(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class EpisodePhase(str, Enum):
    COLLECTING = "collecting"
    EVALUATING = "evaluating"
    CLOSED = "closed"


class EvidenceMaturity(str, Enum):
    WARMING = "warming"
    MONITORABLE = "monitorable"
    ECONOMICS_READY = "economics_ready"
    INSUFFICIENT = "insufficient"


class DataQualityState(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    BLOCKED = "blocked"


class ScaleHealth(str, Enum):
    HEALTHY = "healthy"
    FATIGUE_WATCH = "fatigue_watch"
    CREATIVE_FATIGUE = "creative_fatigue"
    ECONOMICS_DECLINE = "economics_decline"
    DELIVERY_ANOMALY = "delivery_anomaly"
    NOT_DELIVERING = "not_delivering"


class AdviceLifecycle(str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class AdviceDispositionKind(str, Enum):
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    CONVERTED_TO_DRAFT = "converted_to_draft"


class SourceManifestState(str, Enum):
    COMPLETE = "complete"
    BLOCKED = "blocked"


class BreakdownEvidenceState(str, Enum):
    OBSERVED = "observed"
    PRIVACY_SUPPRESSED = "privacy_suppressed"


class AdviceType(str, Enum):
    REFRESH_CREATIVE = "refresh_creative"
    REVIEW_OFFER_OR_LANDING_PAGE = "review_offer_or_landing_page"
    REVIEW_DELIVERY_MIX = "review_delivery_mix"
    INVESTIGATE_DELIVERY_ANOMALY = "investigate_delivery_anomaly"
    CONSIDER_SCALE_TEST = "consider_scale_test"


class SignalState(str, Enum):
    HIT = "hit"
    MISS = "miss"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class WindowRole(str, Enum):
    CURRENT_7D = "current_7d"
    BASELINE_7D = "baseline_7d"
    ROLLING_14D = "rolling_14d"


class BreakdownType(str, Enum):
    PLACEMENT = "placement"
    COUNTRY = "country"
    AGE_GENDER = "age_gender"


class DeliveryDiagnosticState(str, Enum):
    UNKNOWN = "unknown"
    NONE_ = "none"
    LEARNING = "learning"
    LEARNING_LIMITED = "learning_limited"


class DataQualityImpact(str, Enum):
    WARNING = "warning"
    BLOCKING = "blocking"


class PriorStreakAbsenceReason(str, Enum):
    NO_EARLIER_DISTINCT_DAY = "no_earlier_distinct_day"


class BreakdownSuppressionReason(str, Enum):
    API_PRIVACY_THRESHOLD = "api_privacy_threshold"
    API_POLICY_OMISSION = "api_policy_omission"


class AdviceSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class TriggerKind(str, Enum):
    LIVE = "live"
    BACKFILL = "backfill"
    POLICY_CHANGE = "policy_change"
    TARGET_CHANGE = "target_change"
    FORWARD_REPLAY = "forward_replay"


class RunOutcomeKind(str, Enum):
    EVALUATED = "evaluated"
    ALREADY_EVALUATED = "already_evaluated"
    EPISODE_ROLLED_OVER = "episode_rolled_over"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED_INTEGRITY = "failed_integrity"


class DailyEvidenceState(str, Enum):
    """One account-local day in a snapshot window.

    ``BEFORE_EPISODE`` (snapshot schema v2, Issue #57) marks a window day
    that precedes the Episode's ``started_local_day``. The Episode's own
    start explains it (contract §8), so it carries no metrics and no
    DataQualityIssue; it can only open a window, never follow a day of the
    Episode.
    """

    PRESENT = "present"
    MISSING = "missing"
    BEFORE_EPISODE = "before_episode"


class MetricContractType(str, Enum):
    DAILY = "daily"
    EXACT_WINDOW = "exact_window"
    BREAKDOWN_PLACEMENT = "breakdown_placement"
    BREAKDOWN_COUNTRY = "breakdown_country"
    BREAKDOWN_AGE_GENDER = "breakdown_age_gender"
    DELIVERY_CONFIGURATION = "delivery_configuration"


class DataQualityIssueCode(str, Enum):
    """Stable typed codes for `DataQualityItem.code` (spec §2.2).

    The spec's prose names the concrete blocking/warning cases a sealed
    snapshot must represent (missing daily slots, unavailable exact-window
    values, unknown/omitted breakdown segments, delivery-configuration
    incompatibility, exceeded freshness SLO) plus the broader per-stage
    failure families carried from source assembly (pagination, contract,
    timezone, currency, binding, reconcile, mode). Unlike
    `InvalidScaleSnapshot`, §2.2/§2.4 do not give this enum a closed,
    literal list; this is the value set Task 7 defines to satisfy every
    named case. It is deliberately extensible by later Policy-facing work.
    """

    DAILY_SLOT_MISSING = "daily_slot_missing"
    EXACT_WINDOW_VALUE_UNAVAILABLE = "exact_window_value_unavailable"
    BREAKDOWN_SEGMENT_UNKNOWN = "breakdown_segment_unknown"
    DELIVERY_CONFIGURATION_INCOMPATIBLE = "delivery_configuration_incompatible"
    DELIVERY_DIAGNOSTIC_UNKNOWN = "delivery_diagnostic_unknown"
    FRESHNESS_SLO_EXCEEDED = "freshness_slo_exceeded"
    SOURCE_PAGINATION_INCOMPLETE = "source_pagination_incomplete"
    SOURCE_CONTRACT_MISMATCH = "source_contract_mismatch"
    SOURCE_TIMEZONE_MISMATCH = "source_timezone_mismatch"
    SOURCE_CURRENCY_MISMATCH = "source_currency_mismatch"
    SOURCE_BINDING_INCOMPLETE = "source_binding_incomplete"
    SOURCE_RECONCILE_PENDING = "source_reconcile_pending"
    SOURCE_MODE_MISMATCH = "source_mode_mismatch"
