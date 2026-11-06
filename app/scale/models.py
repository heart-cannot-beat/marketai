"""Scale-domain SQLAlchemy record classes (spec §1.4-§1.6).

Enrollment, Episode, Policy, Target, and metric-semantic-contract-set ORM
records. Every tenant-owned table carries `workspace_id NOT NULL`; every
evidence-bearing table also carries `meta_mode NOT NULL` and the
`(workspace_id, meta_mode)` provenance pair. `ScalePolicyVersionRecord` is the
one platform-global exception: it has neither, per spec §1.6.

PostgreSQL enforcement still lives in the Task 6 migration, but every partial
unique index is mirrored in this module's SQLAlchemy metadata.  That keeps
Alembic autogenerate honest: a database index omitted from metadata is treated
as drift and proposed for removal during every release check.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import new_id, utcnow


META_MODE_VALUES = "('customer_sandbox', 'controlled_mock', 'internal_validation')"

SCENARIO_BICONDITIONAL_SQL = (
    "(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') "
    "OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')"
)

# Shared CHECK value lists for §1.7 source evidence and manifest records.
# Kept as string constants (mirroring META_MODE_VALUES above) so every
# CheckConstraint that restricts to one of these canonical sets stays in sync.
DAILY_EVIDENCE_STATE_VALUES = "('present', 'missing')"
WINDOW_ROLE_VALUES = "('current_7d', 'baseline_7d', 'rolling_14d')"
BREAKDOWN_EVIDENCE_STATE_VALUES = "('observed', 'privacy_suppressed')"
BREAKDOWN_SUPPRESSION_REASON_VALUES = "('api_privacy_threshold', 'api_policy_omission')"
BREAKDOWN_CONTRACT_TYPE_VALUES = (
    "('breakdown_placement', 'breakdown_country', 'breakdown_age_gender')"
)
METRIC_CONTRACT_TYPE_VALUES = (
    "('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', "
    "'breakdown_age_gender', 'delivery_configuration')"
)
SOURCE_MANIFEST_STATE_VALUES = "('complete', 'blocked')"
DATA_QUALITY_IMPACT_VALUES = "('warning', 'blocking')"
EVIDENCE_MATURITY_VALUES = "('warming', 'monitorable', 'economics_ready', 'insufficient')"
DATA_QUALITY_STATE_VALUES = "('pass', 'warning', 'blocked')"

# Shared CHECK value lists for §1.8-§1.9 snapshot and evaluation records.
SCALE_HEALTH_VALUES = (
    "('healthy', 'fatigue_watch', 'creative_fatigue', 'economics_decline', "
    "'delivery_anomaly', 'not_delivering')"
)
SIGNAL_STATE_VALUES = "('hit', 'miss', 'unavailable', 'blocked')"
ADVICE_TYPE_VALUES = (
    "('refresh_creative', 'review_offer_or_landing_page', 'review_delivery_mix', "
    "'investigate_delivery_anomaly', 'consider_scale_test')"
)
ADVICE_SEVERITY_VALUES = "('info', 'warning', 'critical')"
# Shared CHECK value lists for §1.10-§1.11 Advice, conversion, and outcome
# records.
ADVICE_LIFECYCLE_VALUES = "('active', 'resolved', 'superseded', 'expired')"
ADVICE_DISPOSITION_KIND_VALUES = "('acknowledged', 'dismissed', 'converted_to_draft')"
ADVICE_THREAD_EVENT_TYPE_VALUES = "('created', 'resolved', 'superseded', 'expired')"
ADVICE_NOTIFICATION_DECISION_VALUES = "('send', 'suppress')"
TRIGGER_KIND_VALUES = (
    "('live', 'backfill', 'policy_change', 'target_change', 'forward_replay')"
)
# Run checkpoint progress (internal orchestration state), distinct from the
# terminal RunOutcomeKind vocabulary below -- 'requested' and 'snapshot_sealed'
# are non-terminal and never carry an outcome_kind.
RUN_STATE_VALUES = (
    "('requested', 'snapshot_sealed', 'evaluated', 'episode_rolled_over', "
    "'blocked', 'cancelled', 'failed_integrity')"
)
RUN_OUTCOME_KIND_VALUES = (
    "('evaluated', 'already_evaluated', 'episode_rolled_over', 'blocked', "
    "'cancelled', 'failed_integrity')"
)
BREAKDOWN_TYPE_VALUES = "('placement', 'country', 'age_gender')"
PRIOR_STREAK_ABSENCE_REASON_VALUES = "('no_earlier_distinct_day')"

# Reserved marker for an explicit "no Target" identity, mirroring
# `__no_scenario__` above: nullable `target_version_id` is never relied on for
# uniqueness by itself, so every table that freezes a Target also freezes a
# non-null `normalized_target_key` equal to the reserved marker when absent.
NO_TARGET_MARKER = "__no_target__"
TARGET_BICONDITIONAL_SQL = (
    f"(target_version_id IS NULL AND normalized_target_key = '{NO_TARGET_MARKER}') "
    "OR (target_version_id IS NOT NULL AND normalized_target_key = target_version_id)"
)


# ---------------------------------------------------------------------------
# §1.4 ScaleEnrollment aggregate
# ---------------------------------------------------------------------------

class ScaleEnrollmentRecord(Base):
    """The stable user intent to monitor one contextual Winner against one
    normalized remote delivery binding. Spec §1.4."""

    __tablename__ = "scale_enrollments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_senr_ws_mode_id"),
        CheckConstraint(
            "lifecycle IN ('draft', 'active', 'paused', 'closed')",
            name="ck_senr_lifecycle",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_senr_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_senr_scenario_biconditional",
        ),
        # Deferred to Task 6 (migration): partial unique index
        #   ix_senr_active_binding on
        #   (workspace_id, meta_mode, meta_connection_id, ad_account_id,
        #    remote_binding_key)
        #   WHERE lifecycle IN ('active', 'paused')
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("senr"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    winner_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    winner_confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_code: Mapped[str] = mapped_column(String(10), nullable=False)
    landing_page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_asset_binding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Normalized before the binding key is calculated; a campaign-only or
    # campaign/ad-set-only binding leaves the deeper identifiers null.
    campaign_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_set_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Server-derived from the immutable connection/account/campaign/ad-set/ad
    # identity. The browser never supplies this.
    remote_binding_key: Mapped[str] = mapped_column(String(200), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    transition_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    current_episode_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    original_initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    evaluation_cancellation_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- Server-derived lineage provenance (issue #86 hardening, Task 9) ----
    # Nullable: the pre-#86 raw-ID creation path
    # (``app.scale.enrollment.create_draft_enrollment_from_request``, kept
    # for internal/test composition only -- see that function's docstring)
    # populates none of these. Every Enrollment created through the public,
    # candidate-gated ``create_draft_enrollment`` populates all five.
    enrollment_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lineage_guard_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guard_revision_at_draft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ScaleEnrollmentEventRecord(Base):
    """Append-only history of every Enrollment lifecycle transition. The
    Enrollment row is a CAS-maintained projection of this history. Spec §1.4."""

    __tablename__ = "scale_enrollment_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sene_enrollment",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "deduplication_key",
            name="uq_sene_workspace_mode_dedup",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sene_meta_mode",
        ),
        CheckConstraint(
            "previous_lifecycle IS NULL "
            "OR previous_lifecycle IN ('draft', 'active', 'paused', 'closed')",
            name="ck_sene_previous_lifecycle",
        ),
        CheckConstraint(
            "current_lifecycle IN ('draft', 'active', 'paused', 'closed')",
            name="ck_sene_current_lifecycle",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sene"))
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    previous_lifecycle: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_lifecycle: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    original_initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    revision_at_event: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.5 ScaleEpisode aggregate
# ---------------------------------------------------------------------------

class ScaleEpisodeRecord(Base):
    """One configuration-comparable observation interval. All context and
    provenance fields are immutable; phase and the projection fields are a
    recoverable mutable projection. Spec §1.5."""

    __tablename__ = "scale_episodes"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sep_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sep_enrollment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "phase IN ('collecting', 'evaluating', 'closed')",
            name="ck_sep_phase",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sep_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sep_scenario_biconditional",
        ),
        CheckConstraint(
            "(phase = 'closed') = (ended_at IS NOT NULL)",
            name="ck_sep_closed_ended_at",
        ),
        CheckConstraint(
            "(phase = 'closed') = (closure_reason IS NOT NULL)",
            name="ck_sep_closed_closure_reason",
        ),
        # Deferred to Task 6 (migration): partial unique index enforcing at
        # most one non-closed Episode per Enrollment,
        #   ix_sep_open_per_enrollment on (workspace_id, meta_mode,
        #   enrollment_id) WHERE phase != 'closed'
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sep"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    winner_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    winner_confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_code: Mapped[str] = mapped_column(String(10), nullable=False)
    landing_page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_asset_binding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    campaign_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_set_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_config_baseline_source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_comparability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_semantic_contract_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    attribution_semantics_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="collecting")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    started_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    projection_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projection_as_of_local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    projection_source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projection_semantic_event_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    projection_lineage_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    accepted_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_source_manifest_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fencing token: sealing a projection-eligible live run stores the run id
    # here in the same transaction that moves phase to `evaluating`, so a
    # stale worker retry cannot clear another run's phase.
    phase_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ScaleEpisodeEventRecord(Base):
    """Append-only history of every Episode phase and closure transition.
    Spec §1.5."""

    __tablename__ = "scale_episode_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sevt_enrollment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sevt_episode",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "deduplication_key",
            name="uq_sevt_workspace_mode_dedup",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sevt_meta_mode",
        ),
        CheckConstraint(
            "previous_phase IS NULL "
            "OR previous_phase IN ('collecting', 'evaluating', 'closed')",
            name="ck_sevt_previous_phase",
        ),
        CheckConstraint(
            "current_phase IN ('collecting', 'evaluating', 'closed')",
            name="ck_sevt_current_phase",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sevt"))
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    previous_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_phase: Mapped[str] = mapped_column(String(20), nullable=False)
    closure_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    original_initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.6 Policy, Target, and semantic contracts
# ---------------------------------------------------------------------------

class ScalePolicyVersionRecord(Base):
    """Immutable-after-publication evaluator configuration. Platform-global:
    no `workspace_id`/`meta_mode` (spec §1.6). Every evaluator-used choice is
    a typed column; `JSON` columns hold nested, internally-versioned config
    the evaluator reads as part of the frozen policy snapshot (bootstrap
    per-metric overrides, breakdown family definitions, per-signal threshold
    rules, streak carry/reset reasons, advice severity bands, and mode
    permissions)."""

    __tablename__ = "scale_policy_versions"
    __table_args__ = (
        CheckConstraint(
            "decimal_rounding IN "
            "('round_half_even', 'round_half_up', 'round_down', 'round_ceiling')",
            name="ck_spol_decimal_rounding",
        ),
        CheckConstraint(
            "bootstrap_percentile_method IN ('nearest_rank', 'linear_interpolation')",
            name="ck_spol_bootstrap_percentile_method",
        ),
        CheckConstraint(
            "bootstrap_draw_order IN ('sequential', 'shuffled')",
            name="ck_spol_bootstrap_draw_order",
        ),
        CheckConstraint(
            "(NOT is_published) OR (published_at IS NOT NULL)",
            name="ck_spol_published_at_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("spol"))
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    decimal_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    decimal_rounding: Mapped[str] = mapped_column(String(30), nullable=False)
    z_value: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    evidence_floor_minimum_spend: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    evidence_floor_minimum_days: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_floor_minimum_conversions: Mapped[int] = mapped_column(Integer, nullable=False)
    activity_floor_minimum_impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_floor_minimum_clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    spend_floor_minimum_daily_spend: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    freshness_floor_maximum_lag_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    # Per-signal thresholds and their inclusive/strict comparison rule.
    signal_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Streak carry/reset rules, keyed by typed reason.
    streak_rules: Mapped[dict] = mapped_column(JSON, nullable=False)
    bootstrap_prng_algorithm: Mapped[str] = mapped_column(String(40), nullable=False)
    bootstrap_draw_count: Mapped[int] = mapped_column(Integer, nullable=False)
    bootstrap_valid_draw_ratio: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    bootstrap_percentile_method: Mapped[str] = mapped_column(
        String(30), nullable=False, default="nearest_rank",
    )
    bootstrap_draw_order: Mapped[str] = mapped_column(String(20), nullable=False)
    # Breakdown families in scope and their sample/share gates.
    breakdown_families: Mapped[dict] = mapped_column(JSON, nullable=False)
    breakdown_sample_gate_minimum: Mapped[int] = mapped_column(Integer, nullable=False)
    breakdown_share_gate_minimum_ratio: Mapped[str] = mapped_column(
        Numeric(6, 5), nullable=False,
    )
    bh_fdr_alpha: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    # Advice severity bands keyed by ScaleHealth/signal outcome.
    advice_severity_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    notification_cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    # Values a downstream Advice-to-draft conversion may safely carry.
    conversion_safe_values: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Which MetaMode values may evaluate under this Policy.
    mode_permissions: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScaleTargetVersionRecord(Base):
    """Immutable economic target values for one exact ProductVersion,
    OfferVersion, market, and currency context. Workspace-scoped,
    mode-independent. Lifecycle and publication/withdrawal are expressed by
    the revisioned projection here plus the append-only event history in
    `ScaleTargetLifecycleEventRecord`. Spec §1.6."""

    __tablename__ = "scale_target_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_stgt_ws_id"),
        CheckConstraint(
            "lifecycle_state IN ('draft', 'active', 'withdrawn')",
            name="ck_stgt_lifecycle_state",
        ),
        CheckConstraint(
            "target_cpa IS NOT NULL OR breakeven_roas IS NOT NULL",
            name="ck_stgt_at_least_one_target",
        ),
        # Deferred to Task 6 (migration): partial unique index for one active
        # Target per exact context,
        #   ix_stgt_active_context on
        #   (workspace_id, product_version_id, offer_version_id, market_code,
        #    currency)
        #   WHERE lifecycle_state = 'active'
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stgt"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_code: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_cpa: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    breakeven_roas: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    minimum_economic_spend: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    lifecycle_state: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScaleTargetLifecycleEventRecord(Base):
    """Append-only publication/withdrawal history for a Target version.
    Advances the ordered semantic-event sequence evaluation projection
    ordering relies on. Spec §1.6."""

    __tablename__ = "scale_target_lifecycle_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "target_version_id"],
            ["scale_target_versions.workspace_id", "scale_target_versions.id"],
            name="fk_stge_target_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('publish', 'withdraw')",
            name="ck_stge_event_type",
        ),
        CheckConstraint(
            "previous_lifecycle_state IS NULL "
            "OR previous_lifecycle_state IN ('draft', 'active', 'withdrawn')",
            # Kept short: the project's "ck" naming convention re-prefixes
            # this literal name with ck_<table_name>_, and the combined
            # identifier must stay within PostgreSQL's 63-char limit.
            name="ck_stge_prev_state",
        ),
        CheckConstraint(
            "current_lifecycle_state IN ('draft', 'active', 'withdrawn')",
            name="ck_stge_curr_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stge"))
    target_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_lifecycle_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_lifecycle_state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    semantic_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    original_initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    revision_at_event: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Target publication/withdrawal previews (issue #87, Task 1)
# ---------------------------------------------------------------------------

class ScaleTargetPublicationPreviewRecord(Base):
    """A short-lived, server-derived proof of the impact of publishing a new
    Target for one exact ProductVersion/OfferVersion/market/currency context.
    Mode-independent, like `ScaleTargetVersionRecord` itself (spec §1.6).
    `publish_scale_target` consumes the preview, re-derives the impact, and
    compares impact hashes to detect drift between preview and commit."""

    __tablename__ = "scale_target_publication_previews"
    __table_args__ = (
        # Single-column FK on workspace_id is already declared on the
        # mapped_column below (mirrors ScaleTargetVersionRecord and every
        # other workspace-scoped table with no composite relation here) --
        # no redundant ForeignKeyConstraint duplicating it.
        CheckConstraint(
            "target_cpa IS NOT NULL OR breakeven_roas IS NOT NULL",
            # Kept short: the "ck" naming convention re-prefixes this literal
            # name with "ck_scale_target_publication_previews_", and the
            # combined identifier must stay within PostgreSQL's 63-char
            # limit (see ScaleTargetLifecycleEventRecord's ck_stge_prev_state
            # for the same constraint).
            name="ck_stpp_has_target",
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (result_target_version_id IS NULL)",
            name="ck_stpp_consumed_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stpp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_code: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_cpa: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    breakeven_roas: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    minimum_economic_spend: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    affected_enrollment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_floor_check_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    result_target_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleTargetWithdrawalPreviewRecord(Base):
    """A short-lived, server-derived proof of the impact of withdrawing an
    active Target version. Mode-independent, like `ScaleTargetVersionRecord`
    itself (spec §1.6). `withdraw_scale_target` consumes the preview and
    re-validates the Target's lifecycle state before committing."""

    __tablename__ = "scale_target_withdrawal_previews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "target_version_id"],
            ["scale_target_versions.workspace_id", "scale_target_versions.id"],
            name="fk_stwp_target_version", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (withdrawn_at IS NULL)",
            name="ck_stwp_consumed_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stwp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    target_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    affected_enrollment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dependent_advice_thread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleMetricSemanticContractSetRecord(Base):
    """Immutable parent that freezes the Scale meaning of daily, exact-window,
    breakdown-family, and delivery-configuration contracts for one
    Workspace/mode context. Spec §1.6."""

    __tablename__ = "scale_metric_semantic_contract_sets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_smcs_ws_mode_id"),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_smcs_meta_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("smcs"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)


class ScaleMetricSemanticContractSetMemberRecord(Base):
    """Immutable child wrapping one exact #23 physical contract generation and
    its hash inside a `ScaleMetricSemanticContractSetRecord`. Compatibility
    between physical generations is explicit; absence of a declaration means
    incompatible. Spec §1.6."""

    __tablename__ = "scale_metric_semantic_contract_set_members"
    __table_args__ = (
        # No workspace_id/meta_mode on this table (child-only, like
        # dimension_values -> dimensions): scoped entirely through
        # contract_set_id -> its parent's Workspace/mode. A single-column FK
        # is the whole story here -- there is no composite key to compose.
        ForeignKeyConstraint(
            ["contract_set_id"],
            ["scale_metric_semantic_contract_sets.id"],
            name="fk_smcm_contract_set",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "contract_set_id", "contract_type",
            name="uq_smcm_contract_set_contract_type",
        ),
        CheckConstraint(
            "contract_type IN "
            "('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', "
            "'breakdown_age_gender', 'delivery_configuration')",
            # Kept short: the project's "ck" naming convention re-prefixes
            # this literal name with ck_<table_name>_, and the combined
            # identifier must stay within PostgreSQL's 63-char limit.
            name="ck_smcm_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("smcm"))
    contract_set_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contract_type: Mapped[str] = mapped_column(String(40), nullable=False)
    observation_contract_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.7 Immutable source evidence and terminal manifests
#
# Scale never seals from mutable "latest" projections; it retains immutable
# source revisions linked to #23 SyncRun, occurrence, call intent, page
# attempts, reconciliation, raw hashes, contract generation, account,
# timezone, currency, mode, connection, and scenario scope. Cross-table
# references to those #23 records (and to sibling scale_* rows) are plain
# indexed `String` columns without an FK, matching Task 2's convention: a
# single-column FK would only check id existence, not the workspace/mode/
# connection/scenario composite scoping the spec requires, so composite FKs
# are deferred to the Task 6 migration alongside partial unique indexes.
#
# All source records, manifest parents/items, and source quality issues are
# append-only and ON DELETE RESTRICT once referenced (spec §1.7) -- no
# update/delete path is modeled here; that immutability is enforced at the
# transaction-service layer in a later task.
# ---------------------------------------------------------------------------

class ScaleDailyMetricSourceRecord(Base):
    """One immutable daily evidence slot per requested complete local day.

    Observed zero delivery is a present slot with zero primitives and
    `delivered=false`; missing evidence is not a row of zeros -- `state`
    discriminates the two, and the shape CHECK below forbids storing
    primitive values on a `missing` slot. Spec §1.7."""

    __tablename__ = "scale_daily_metric_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sdms_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sdms_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sdms_scenario_biconditional",
        ),
        CheckConstraint(
            f"state IN {DAILY_EVIDENCE_STATE_VALUES}",
            name="ck_sdms_state",
        ),
        CheckConstraint(
            "(state = 'present' AND delivered IS NOT NULL "
            "AND spend IS NOT NULL AND impressions IS NOT NULL "
            "AND link_clicks IS NOT NULL AND purchases IS NOT NULL "
            "AND purchase_value IS NOT NULL) "
            "OR "
            "(state = 'missing' AND delivered IS NULL "
            "AND spend IS NULL AND impressions IS NULL "
            "AND link_clicks IS NULL AND purchases IS NULL "
            "AND purchase_value IS NULL)",
            name="ck_sdms_state_shape",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "source_revision", "local_day",
            name="uq_sdms_revision_day",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sdms"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    delivered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spend: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(String(200), nullable=False)
    call_intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    page_attempt_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reconciliation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleExactWindowSourceRecord(Base):
    """One immutable exact-window evidence aggregate for `current_7d`,
    `baseline_7d`, or `rolling_14d`. Deduplicated metrics (reach, frequency)
    can only come from an exact-window query; they are never derived by
    summing or averaging daily rows. Spec §1.7."""

    __tablename__ = "scale_exact_window_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sews_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sews_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sews_scenario_biconditional",
        ),
        CheckConstraint(
            f"window_role IN {WINDOW_ROLE_VALUES}",
            name="ck_sews_window_role",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "source_revision", "window_role",
            name="uq_sews_revision_window",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sews"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    window_role: Mapped[str] = mapped_column(String(20), nullable=False)
    window_start_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    window_end_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    reach: Mapped[int] = mapped_column(Integer, nullable=False)
    frequency: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    spend: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    link_clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    purchases: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_value: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(String(200), nullable=False)
    call_intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciliation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleBreakdownSourceRecord(Base):
    """One immutable breakdown-family evidence slot for one segment,
    discriminated between `observed` (owns daily children, see
    `ScaleBreakdownDailyChildRecord`) and `privacy_suppressed` (owns a typed
    suppression reason, see `ScaleBreakdownPrivacyRecord`, and forbids metric
    children). Spec §1.7-§1.8."""

    __tablename__ = "scale_breakdown_sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sbds_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sbds_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sbds_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sbds_scenario_biconditional",
        ),
        CheckConstraint(
            f"contract_type IN {BREAKDOWN_CONTRACT_TYPE_VALUES}",
            name="ck_sbds_contract_type",
        ),
        CheckConstraint(
            f"window_role IN {WINDOW_ROLE_VALUES}",
            name="ck_sbds_window_role",
        ),
        CheckConstraint(
            f"state IN {BREAKDOWN_EVIDENCE_STATE_VALUES}",
            name="ck_sbds_state",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "source_revision",
            "contract_type", "window_role", "segment_key",
            name="uq_sbds_revision_segment",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sbds"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # One of the three breakdown MetricContractType values.
    contract_type: Mapped[str] = mapped_column(String(40), nullable=False)
    window_role: Mapped[str] = mapped_column(String(20), nullable=False)
    # The observed (or would-have-been-observed) segment value, e.g. a
    # placement name, ISO country code, or age/gender bucket.
    segment_key: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(String(200), nullable=False)
    call_intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciliation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleBreakdownDailyChildRecord(Base):
    """Immutable daily evidence slot owned by an `observed`
    `ScaleBreakdownSourceRecord`. Never present for a `privacy_suppressed`
    parent -- enforced at the transaction-service layer (composite/parent-
    state-aware FK/CHECK deferred to Task 6, per the module-level note).
    Spec §1.7-§1.8."""

    __tablename__ = "scale_breakdown_daily_children"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "breakdown_source_id"],
            ["scale_breakdown_sources.workspace_id", "scale_breakdown_sources.meta_mode", "scale_breakdown_sources.id"],
            name="fk_sbdc_breakdown_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sbdc_meta_mode",
        ),
        CheckConstraint(
            f"state IN {DAILY_EVIDENCE_STATE_VALUES}",
            name="ck_sbdc_state",
        ),
        CheckConstraint(
            "(state = 'present' AND delivered IS NOT NULL "
            "AND spend IS NOT NULL AND impressions IS NOT NULL "
            "AND link_clicks IS NOT NULL AND purchases IS NOT NULL "
            "AND purchase_value IS NOT NULL) "
            "OR "
            "(state = 'missing' AND delivered IS NULL "
            "AND spend IS NULL AND impressions IS NULL "
            "AND link_clicks IS NULL AND purchases IS NULL "
            "AND purchase_value IS NULL)",
            name="ck_sbdc_state_shape",
        ),
        UniqueConstraint(
            "breakdown_source_id", "local_day",
            name="uq_sbdc_source_day",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sbdc"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    breakdown_source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    delivered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spend: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleBreakdownPrivacyRecord(Base):
    """Typed suppression reason owned by a `privacy_suppressed`
    `ScaleBreakdownSourceRecord`. At most one per parent. Spec §1.7-§1.8."""

    __tablename__ = "scale_breakdown_privacy_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "breakdown_source_id"],
            ["scale_breakdown_sources.workspace_id", "scale_breakdown_sources.meta_mode", "scale_breakdown_sources.id"],
            name="fk_sbpr_breakdown_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sbpr_meta_mode",
        ),
        CheckConstraint(
            f"suppression_reason IN {BREAKDOWN_SUPPRESSION_REASON_VALUES}",
            name="ck_sbpr_suppression_reason",
        ),
        UniqueConstraint(
            "breakdown_source_id",
            name="uq_sbpr_breakdown_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sbpr"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    breakdown_source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suppression_reason: Mapped[str] = mapped_column(String(40), nullable=False)
    # Safe, non-raw detail suitable for structured logs and UI display.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleDeliveryConfigSourceRecord(Base):
    """One immutable delivery-configuration evidence snapshot. Carries both
    `evidence_hash` (the complete contract observation and response) and
    `comparability_hash` (only the fields that necessarily break Episode
    comparability), per spec §1.5/§1.7 -- a small budget delta must not, by
    itself, change the full evidence hash and prematurely reopen an Episode.
    Full targeting stays inside protected raw evidence; this record exposes
    only a `targeting_safety_hash` summary."""

    __tablename__ = "scale_delivery_config_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sdcs_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sdcs_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sdcs_scenario_biconditional",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "source_revision",
            name="uq_sdcs_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sdcs"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_creative_id: Mapped[str] = mapped_column(String(100), nullable=False)
    campaign_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_set_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ad_remote_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    campaign_status: Mapped[str] = mapped_column(String(40), nullable=False)
    ad_set_status: Mapped[str] = mapped_column(String(40), nullable=False)
    ad_status: Mapped[str] = mapped_column(String(40), nullable=False)
    budget_source: Mapped[str] = mapped_column(String(40), nullable=False)
    budget_amount: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    optimization_goal: Mapped[str] = mapped_column(String(60), nullable=False)
    bid_strategy: Mapped[str] = mapped_column(String(60), nullable=False)
    # Stores the Winner's SHA-256 attribution-semantics hash (64 hex chars),
    # not a short Meta UI label.
    attribution_setting: Mapped[str] = mapped_column(String(64), nullable=False)
    # Summary hash over full placement/targeting; the raw payload is retained
    # only inside protected raw evidence, never in this projection-visible row.
    targeting_safety_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    landing_page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    comparability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(String(200), nullable=False)
    call_intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciliation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvidenceSourceManifestRecord(Base):
    """The terminal result for one requested source revision: `complete`
    proves terminal reconciliation and a complete cursor/page chain (missing
    or privacy-suppressed values may still be complete when recorded as typed
    quality); `blocked` records exhausted transport, incomplete pagination,
    unresolved terminal reconciliation, contract mismatch, or other
    source-integrity failure and can never be selected by a run. Spec §1.7."""

    __tablename__ = "scale_evidence_source_manifests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sesm_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sesm_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sesm_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            # Kept short: combined with the long "scale_evidence_source_
            # manifests" table name via the naming convention, the full
            # "..._scenario_biconditional" name overflows PostgreSQL's
            # 63-char identifier limit.
            name="ck_sesm_scenario",
        ),
        CheckConstraint(
            f"state IN {SOURCE_MANIFEST_STATE_VALUES}",
            name="ck_sesm_state",
        ),
        CheckConstraint(
            # `last_complete_local_day` is nullable only when blocked before
            # any day was determined; a `complete` manifest always has one.
            "(state != 'complete') OR (last_complete_local_day IS NOT NULL)",
            name="ck_sesm_complete_last_day",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "source_revision",
            name="uq_sesm_episode_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sesm"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    contract_request_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_complete_local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    safe_watermark: Mapped[date] = mapped_column(Date, nullable=False)
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleManifestItemRecord(Base):
    """One immutable child per requested contract instance in a
    `ScaleEvidenceSourceManifestRecord`, pointing at its terminal source
    record (when fulfilled) or recording that the slot is missing/failed.
    Spec §1.7."""

    __tablename__ = "scale_manifest_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "manifest_id"],
            ["scale_evidence_source_manifests.workspace_id", "scale_evidence_source_manifests.meta_mode", "scale_evidence_source_manifests.id"],
            name="fk_smi_manifest",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_smi_meta_mode",
        ),
        CheckConstraint(
            f"contract_type IN {METRIC_CONTRACT_TYPE_VALUES}",
            name="ck_smi_contract_type",
        ),
        CheckConstraint(
            "state IN ('fulfilled', 'missing', 'failed')",
            name="ck_smi_state",
        ),
        CheckConstraint(
            "(state != 'fulfilled') OR (source_record_id IS NOT NULL)",
            name="ck_smi_fulfilled_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("smi"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contract_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Populated only for the contract type it locates: `local_day` for
    # `daily`, `window_role` for `exact_window`, both null otherwise.
    window_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    segment_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    # Points at the terminal ScaleDailyMetricSource/ScaleExactWindowSource/
    # ScaleBreakdownSource/ScaleDeliveryConfigSource row id when fulfilled.
    source_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleSourceQualityIssueRecord(Base):
    """One immutable typed quality issue owned by a
    `ScaleEvidenceSourceManifestRecord`. `code` is a stable, contracted
    string outside a closed enum: the spec's minimum terminal-source codes
    (`transport_exhausted`, `incomplete_pagination`, `reconciliation_unresolved`,
    `contract_instance_failed`, `connection_ineligible`,
    `source_provenance_mismatch`) are a floor, not a ceiling -- adding a code
    is a Policy change, not a migration. Spec §1.7."""

    __tablename__ = "scale_source_quality_issues"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "manifest_id"],
            ["scale_evidence_source_manifests.workspace_id", "scale_evidence_source_manifests.meta_mode", "scale_evidence_source_manifests.id"],
            name="fk_ssqi_manifest",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_ssqi_meta_mode",
        ),
        CheckConstraint(
            f"severity IN {DATA_QUALITY_IMPACT_VALUES}",
            name="ck_ssqi_severity",
        ),
        CheckConstraint(
            "contract_type IS NULL OR contract_type IN "
            f"{METRIC_CONTRACT_TYPE_VALUES}",
            name="ck_ssqi_contract_type",
        ),
        CheckConstraint(
            f"window_role IS NULL OR window_role IN {WINDOW_ROLE_VALUES}",
            name="ck_ssqi_window_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ssqi"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    is_blocking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Locator: which requested contract instance the issue concerns, when
    # narrower than the whole manifest.
    contract_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    window_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    segment_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Safe, non-raw detail suitable for structured logs and UI display.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvidenceReadinessProjectionRecord(Base):
    """The current Episode read model, ordered by source revision. Points to
    the latest terminal manifest and exposes maturity, quality state,
    freshness, safe issue counts, and recovery action. A blocked manifest
    projects `maturity=insufficient`, `quality_state=blocked`, no current
    health, and no new candidate, while a prior accepted Evaluation remains
    separately readable as historical/stale -- this projection never
    fabricates one. One row per Episode; a CAS-maintained mutable projection,
    not append-only. Spec §1.7."""

    __tablename__ = "scale_evidence_readiness_projections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_serp_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "latest_manifest_id"],
            ["scale_evidence_source_manifests.workspace_id", "scale_evidence_source_manifests.meta_mode", "scale_evidence_source_manifests.id"],
            name="fk_serp_manifest",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_serp_meta_mode",
        ),
        CheckConstraint(
            f"maturity IN {EVIDENCE_MATURITY_VALUES}",
            name="ck_serp_maturity",
        ),
        CheckConstraint(
            f"quality_state IN {DATA_QUALITY_STATE_VALUES}",
            name="ck_serp_quality",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id",
            name="uq_serp_episode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("serp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    latest_manifest_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    maturity: Mapped[str] = mapped_column(String(30), nullable=False)
    quality_state: Mapped[str] = mapped_column(String(20), nullable=False)
    last_complete_local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    freshness_reference_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    blocking_issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.8 Sealed EvidenceSnapshot
#
# `ScaleEvidenceSnapshotRecord` is created only from a `complete` source
# manifest. It freezes every evaluator-relevant value, including the complete
# predecessor `PriorStreakState` (or a typed absence reason), before hashing.
# `sealed_at` marks the single unsealed-to-sealed transition; children permit
# INSERT only while the parent is unsealed and reject UPDATE/DELETE always --
# that trigger-level enforcement is deferred to the Task 6 migration, per the
# module-level note above §1.7. `canonical_hash` stays nullable until sealing,
# so the "sealed content" uniqueness constraint below naturally excludes
# in-flight unsealed rows without a partial index (NULL is never equal to
# NULL in a UniqueConstraint on any supported dialect).
# ---------------------------------------------------------------------------

class ScaleEvidenceSnapshotRecord(Base):
    """Sealed, immutable evaluator input. Spec §1.8."""

    __tablename__ = "scale_evidence_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sesn_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sesn_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sesn_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sesn_scenario_biconditional",
        ),
        CheckConstraint(
            TARGET_BICONDITIONAL_SQL,
            name="ck_sesn_target_biconditional",
        ),
        CheckConstraint(
            "(sealed_at IS NULL) = (canonical_hash IS NULL)",
            name="ck_sesn_sealed_shape",
        ),
        CheckConstraint(
            "(prior_evaluation_id IS NOT NULL "
            "AND prior_evaluated_local_day IS NOT NULL "
            "AND prior_streak_compat_policy_version_id IS NOT NULL "
            "AND prior_streak_compat_metric_contract_set_hash IS NOT NULL "
            "AND prior_streak_compat_configuration_baseline_hash IS NOT NULL "
            "AND prior_streak_compat_data_continuity_generation IS NOT NULL "
            "AND prior_streak_compat_canonical_hash IS NOT NULL "
            "AND prior_healthy_evaluations IS NOT NULL "
            "AND prior_streak_absence_reason IS NULL) "
            "OR "
            "(prior_evaluation_id IS NULL "
            "AND prior_evaluated_local_day IS NULL "
            "AND prior_streak_compat_policy_version_id IS NULL "
            "AND prior_streak_compat_metric_contract_set_hash IS NULL "
            "AND prior_streak_compat_configuration_baseline_hash IS NULL "
            "AND prior_streak_compat_data_continuity_generation IS NULL "
            "AND prior_streak_compat_canonical_hash IS NULL "
            "AND prior_healthy_evaluations IS NULL "
            f"AND prior_streak_absence_reason IN {PRIOR_STREAK_ABSENCE_REASON_VALUES})",
            name="ck_sesn_prior_streak_shape",
        ),
        # Snapshot claim hash: covers Workspace, mode, Episode, local day,
        # source manifest/revision, freshness reference, Policy,
        # Target/no-target, semantic-event sequence, replay generation,
        # predecessor, canonical schema, and algorithm version (spec §1.8).
        # The full composite is folded into `claim_hash` by the sealing
        # transaction; this constraint enforces its uniqueness per tenant/mode.
        UniqueConstraint(
            "workspace_id", "meta_mode", "claim_hash",
            name="uq_sesn_claim_hash",
        ),
        # Scoped canonical content identity for sealed content only --
        # `canonical_hash` is NULL while unsealed, so this never collides
        # across in-flight provisional rows.
        UniqueConstraint(
            "workspace_id", "meta_mode", "canonical_schema_version", "canonical_hash",
            name="uq_sesn_sealed_content",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sesn"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    remote_binding_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Snapshot claim identity (see UniqueConstraint above); folded from the
    # complete composite the spec defines, computed by the sealing
    # transaction rather than this ORM layer.
    claim_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Selected source manifest.
    source_manifest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    safe_watermark: Mapped[date] = mapped_column(Date, nullable=False)

    # Frozen Policy, Target-or-no-target, algorithm, and canonical schema.
    policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # Frozen Scale metric-contract set.
    metric_semantic_contract_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_semantic_contract_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Frozen delivery configuration (comparability, not just full evidence).
    delivery_config_source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_comparability_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    attribution_semantics_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Exact current/baseline/rolling window boundaries (half-open,
    # account-local, per §2.2 `MetricWindow`/`ExactWindowMetrics`).
    current_window_start_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    current_window_end_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    baseline_window_start_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    baseline_window_end_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    rolling_window_start_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    rolling_window_end_local_day: Mapped[date] = mapped_column(Date, nullable=False)

    complete_local_days_in_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    active_delivery_days_in_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    data_continuity_generation: Mapped[str] = mapped_column(String(64), nullable=False)

    # Predecessor Evaluation and complete frozen `PriorStreakState`, or a
    # typed absence reason -- exactly one of the two shapes, per the CHECK
    # above. `prior_streak_counters` mirrors `PriorStreakState.counters`
    # (a small frozen tuple of `{signal_key, consecutive_hits}`); it is
    # cache-shaped JSON rather than a child table because it is
    # predecessor-owned input, never this snapshot's own evaluator output
    # (that lives on `ScaleEvaluationStreakCounterRecord`, owned by the
    # Evaluation this snapshot produces).
    prior_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prior_evaluated_local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    prior_streak_compat_policy_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    prior_streak_compat_target_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    prior_streak_compat_metric_contract_set_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    prior_streak_compat_configuration_baseline_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    prior_streak_compat_data_continuity_generation: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    prior_streak_compat_canonical_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    prior_health: Mapped[str | None] = mapped_column(String(30), nullable=True)
    prior_healthy_evaluations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prior_streak_counters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    prior_streak_absence_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    last_complete_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    last_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    freshness_reference_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    as_of_local_day: Mapped[date] = mapped_column(Date, nullable=False)

    canonical_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvidenceSourceLinkRecord(Base):
    """Immutable child linking a sealed snapshot to one required source role
    (one requested daily/exact-window/breakdown/delivery-configuration
    contract instance). Permits INSERT only while the parent is unsealed;
    UPDATE/DELETE are rejected at all times (trigger enforcement deferred to
    Task 6). Spec §1.8."""

    __tablename__ = "scale_evidence_source_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "snapshot_id"],
            ["scale_evidence_snapshots.workspace_id", "scale_evidence_snapshots.meta_mode", "scale_evidence_snapshots.id"],
            name="fk_sesl_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sesl_meta_mode",
        ),
        CheckConstraint(
            f"contract_type IN {METRIC_CONTRACT_TYPE_VALUES}",
            name="ck_sesl_contract_type",
        ),
        CheckConstraint(
            f"window_role IS NULL OR window_role IN {WINDOW_ROLE_VALUES}",
            name="ck_sesl_window_role",
        ),
        UniqueConstraint(
            "snapshot_id", "contract_type", "window_role", "local_day", "segment_key",
            name="uq_sesl_snapshot_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sesl"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contract_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Populated only for the contract type it locates, mirroring
    # `ScaleManifestItemRecord`: `local_day` for `daily`, `window_role` for
    # `exact_window`, `segment_key` for breakdown families.
    window_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    segment_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Points at the terminal ScaleDailyMetricSource/ScaleExactWindowSource/
    # ScaleBreakdownSource/ScaleDeliveryConfigSource row id this role froze.
    source_record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleSnapshotQualityIssueRecord(Base):
    """One immutable evaluator-visible typed quality issue frozen into a
    sealed snapshot. Shares the source-quality-issue value contract (spec
    §1.7) but remains a separate immutable record so a pre-sealing manifest
    failure is never disguised as evaluator input. Minimum codes:
    `missing_daily_slot`, `exact_window_unavailable`, `breakdown_incomplete`,
    `privacy_suppressed`, `stale_evidence`, `delivery_config_incompatible`,
    `contract_generation_warning`. Spec §1.8."""

    __tablename__ = "scale_snapshot_quality_issues"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "snapshot_id"],
            ["scale_evidence_snapshots.workspace_id", "scale_evidence_snapshots.meta_mode", "scale_evidence_snapshots.id"],
            name="fk_snqi_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_snqi_meta_mode",
        ),
        CheckConstraint(
            f"severity IN {DATA_QUALITY_IMPACT_VALUES}",
            name="ck_snqi_severity",
        ),
        CheckConstraint(
            "contract_type IS NULL OR contract_type IN "
            f"{METRIC_CONTRACT_TYPE_VALUES}",
            name="ck_snqi_contract_type",
        ),
        CheckConstraint(
            f"window_role IS NULL OR window_role IN {WINDOW_ROLE_VALUES}",
            name="ck_snqi_window_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("snqi"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    is_blocking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    contract_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    window_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    local_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    segment_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Safe, non-raw detail suitable for structured logs and UI display.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleSnapshotBreakdownSegmentRecord(Base):
    """One frozen breakdown segment owned by a sealed snapshot, discriminated
    between `observed` (owns complete daily children, see
    `ScaleSnapshotBreakdownDailyRecord`, and forbids a suppression reason) and
    `privacy_suppressed` (owns a typed suppression reason and forbids metric
    children). Spec §1.8; mirrors §2.2 `BreakdownSegment`."""

    __tablename__ = "scale_snapshot_breakdown_segments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_ssbs_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "snapshot_id"],
            ["scale_evidence_snapshots.workspace_id", "scale_evidence_snapshots.meta_mode", "scale_evidence_snapshots.id"],
            name="fk_ssbs_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_ssbs_meta_mode",
        ),
        CheckConstraint(
            f"breakdown_type IN {BREAKDOWN_TYPE_VALUES}",
            name="ck_ssbs_breakdown_type",
        ),
        CheckConstraint(
            f"state IN {BREAKDOWN_EVIDENCE_STATE_VALUES}",
            name="ck_ssbs_state",
        ),
        CheckConstraint(
            "(state = 'observed' AND suppression_reason IS NULL) "
            "OR "
            f"(state = 'privacy_suppressed' AND suppression_reason IN "
            f"{BREAKDOWN_SUPPRESSION_REASON_VALUES})",
            name="ck_ssbs_state_shape",
        ),
        UniqueConstraint(
            "snapshot_id", "breakdown_type", "segment_key",
            name="uq_ssbs_snapshot_segment",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ssbs"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    breakdown_type: Mapped[str] = mapped_column(String(20), nullable=False)
    segment_key: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    suppression_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The immutable §1.7 `ScaleBreakdownSourceRecord` row this segment froze.
    source_record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleSnapshotBreakdownDailyRecord(Base):
    """Immutable daily evidence slot owned by an `observed`
    `ScaleSnapshotBreakdownSegmentRecord`. Never present for a
    `privacy_suppressed` parent -- enforced at the transaction-service layer
    (composite/parent-state-aware FK/CHECK deferred to Task 6). Spec §1.8."""

    __tablename__ = "scale_snapshot_breakdown_daily"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "breakdown_segment_id"],
            ["scale_snapshot_breakdown_segments.workspace_id", "scale_snapshot_breakdown_segments.meta_mode", "scale_snapshot_breakdown_segments.id"],
            name="fk_ssbd_segment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_ssbd_meta_mode",
        ),
        CheckConstraint(
            f"state IN {DAILY_EVIDENCE_STATE_VALUES}",
            name="ck_ssbd_state",
        ),
        CheckConstraint(
            "(state = 'present' AND delivered IS NOT NULL "
            "AND spend IS NOT NULL AND impressions IS NOT NULL "
            "AND link_clicks IS NOT NULL AND purchases IS NOT NULL "
            "AND purchase_value IS NOT NULL) "
            "OR "
            "(state = 'missing' AND delivered IS NULL "
            "AND spend IS NULL AND impressions IS NULL "
            "AND link_clicks IS NULL AND purchases IS NULL "
            "AND purchase_value IS NULL)",
            name="ck_ssbd_state_shape",
        ),
        UniqueConstraint(
            "breakdown_segment_id", "local_day",
            name="uq_ssbd_segment_day",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ssbd"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    breakdown_segment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    delivered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spend: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.9 Durable runs and immutable Evaluations
# ---------------------------------------------------------------------------

class ScaleEvaluationRunRecord(Base):
    """Recoverable orchestration checkpoint. Freezes the semantic request
    before work begins; `run_state` moves by CAS through
    `requested -> snapshot_sealed -> evaluated` or to a terminal state
    (`episode_rolled_over`, `blocked`, `cancelled`, `failed_integrity`). Only
    `evaluation_run_id` enters the Worker job payload -- everything else is
    resolved from this durable checkpoint, never reinterpreted against
    whatever evidence happens to be newest at retry time. Spec §1.9."""

    __tablename__ = "scale_evaluation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sevr_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sevr_enrollment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id"],
            name="fk_sevr_evaluation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sevr_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sevr_scenario_biconditional",
        ),
        CheckConstraint(
            TARGET_BICONDITIONAL_SQL,
            name="ck_sevr_target_biconditional",
        ),
        CheckConstraint(
            f"trigger_kind IN {TRIGGER_KIND_VALUES}",
            name="ck_sevr_trigger_kind",
        ),
        CheckConstraint(
            f"run_state IN {RUN_STATE_VALUES}",
            name="ck_sevr_run_state",
        ),
        CheckConstraint(
            f"outcome_kind IS NULL OR outcome_kind IN {RUN_OUTCOME_KIND_VALUES}",
            name="ck_sevr_outcome_kind",
        ),
        # Non-terminal states never carry a terminal outcome; every terminal
        # state always does.
        CheckConstraint(
            "(run_state IN ('requested', 'snapshot_sealed')) = (outcome_kind IS NULL)",
            name="ck_sevr_outcome_shape",
        ),
        CheckConstraint(
            "(run_state != 'evaluated') OR (evaluation_id IS NOT NULL)",
            name="ck_sevr_evaluated_shape",
        ),
        CheckConstraint(
            "(run_state = 'requested') OR (snapshot_id IS NOT NULL)",
            name="ck_sevr_snapshot_shape",
        ),
        # Run uniqueness key: Episode, target local day, trigger kind,
        # complete manifest hash/revision, freshness reference, Policy,
        # normalized Target/no-target, algorithm, semantic-event sequence,
        # and replay generation (spec §1.9). Nullable dimensions are
        # normalized (see `normalized_target_key`, `replay_generation`
        # defaulting to 0) so `NULL` never weakens this key.
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "target_local_day",
            "trigger_kind", "source_manifest_hash", "source_revision",
            "freshness_reference_at", "policy_version_id", "normalized_target_key",
            "algorithm_version", "semantic_event_sequence", "replay_generation",
            name="uq_sevr_semantic_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sevr"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    target_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    source_manifest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    freshness_reference_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # Ordered semantic-event sequence for Policy/Target-triggered requests
    # (0 for a `live` trigger); replay generation/base for `forward_replay`.
    semantic_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replay_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replay_base_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Trigger lineage.
    triggering_sync_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    triggering_semantic_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Fenced against the Enrollment's `evaluation_cancellation_generation` at
    # request time; a stale run whose fence no longer matches is cancelled.
    cancellation_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    run_state: Mapped[str] = mapped_column(String(30), nullable=False, default="requested")
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Safe, non-raw detail (e.g. the integrity error code) for a terminal
    # outcome that is not a plain evaluated/reused result.
    outcome_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Job-lease/CAS fence: the persistent Job lease owns dispatch, this run
    # owns domain progress; Phase C completion is fenced by lease owner and
    # this revision.
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvaluationRecord(Base):
    """Immutable evaluator result. Contains the complete canonical result
    payload (the exact bytes/structure `result_hash` was computed over --
    see §2.5 Phase 10), plus queryable typed children for signals, Advice
    candidates, streak counters, and streak resets. A duplicate semantic
    fingerprint must have byte-identical canonical result content or is an
    integrity failure (never silently overwritten -- this row is never
    updated). Spec §1.9; mirrors §2.6 `ScaleEvaluation`."""

    __tablename__ = "scale_evaluations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sev_ws_mode_id"),
        UniqueConstraint("workspace_id", "meta_mode", "id", "snapshot_id", name="uq_sev_ws_mode_id_snapshot"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sev_episode",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sev_meta_mode",
        ),
        CheckConstraint(
            TARGET_BICONDITIONAL_SQL,
            name="ck_sev_target_biconditional",
        ),
        CheckConstraint(
            f"maturity IN {EVIDENCE_MATURITY_VALUES}",
            name="ck_sev_maturity",
        ),
        CheckConstraint(
            f"data_quality_state IN {DATA_QUALITY_STATE_VALUES}",
            name="ck_sev_data_quality_state",
        ),
        CheckConstraint(
            f"health IS NULL OR health IN {SCALE_HEALTH_VALUES}",
            name="ck_sev_health",
        ),
        # Semantic fingerprint (spec §1.9): Workspace, mode, Episode,
        # snapshot canonical hash, metric-contract-set hash, Policy version,
        # normalized Target/no-target, and algorithm version.
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id", "snapshot_canonical_hash",
            "metric_semantic_contract_set_hash", "policy_version_id",
            "normalized_target_key", "algorithm_version",
            name="uq_sev_semantic_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sev"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_semantic_contract_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)

    maturity: Mapped[str] = mapped_column(String(30), nullable=False)
    data_quality_state: Mapped[str] = mapped_column(String(20), nullable=False)
    health: Mapped[str | None] = mapped_column(String(30), nullable=True)
    health_reason: Mapped[str] = mapped_column(String(60), nullable=False)

    # Output `StreakState.compatibility` (§2.6 `StreakCompatibility`),
    # inlined because it is a single frozen struct per Evaluation, not a
    # list -- unlike counters/resets below, which own their own children.
    streak_compat_policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    streak_compat_target_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    streak_compat_metric_contract_set_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    streak_compat_configuration_baseline_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    streak_compat_data_continuity_generation: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    streak_compat_canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    streak_healthy_evaluations: Mapped[int] = mapped_column(Integer, nullable=False)

    # The complete canonical result payload `result_hash` was computed over
    # (input snapshot canonical hash, algorithm version, maturity, quality
    # state, health/reason, ordered signals, ordered candidates, output
    # StreakState -- spec §2.5 Phase 10). Excludes `result_hash` itself and
    # persistence timestamps, matching the evaluator's own exclusion rule.
    canonical_result_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvaluationSignalRecord(Base):
    """One immutable per-signal result owned by a `ScaleEvaluationRecord`.
    Spec §1.9; mirrors §2.6 `SignalResult`."""

    __tablename__ = "scale_evaluation_signals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id"],
            name="fk_sevs_evaluation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sevs_meta_mode",
        ),
        CheckConstraint(
            f"state IN {SIGNAL_STATE_VALUES}",
            name="ck_sevs_state",
        ),
        UniqueConstraint(
            "evaluation_id", "signal_id",
            name="uq_sevs_evaluation_signal",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sevs"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signal_id: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    expected: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    observed: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    interval_lower: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    interval_upper: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    p_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    adjusted_q_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvaluationAdviceCandidateRecord(Base):
    """One immutable Advice candidate owned by a `ScaleEvaluationRecord`.
    `signal_ids` and `evidence` are small frozen tuples inlined as JSON
    rather than further child tables, mirroring the Policy JSON-blob
    convention for internally-versioned nested config. Spec §1.9; mirrors
    §2.6 `AdviceCandidate`."""

    __tablename__ = "scale_evaluation_advice_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id"],
            name="fk_seac_evaluation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_seac_meta_mode",
        ),
        CheckConstraint(
            f"advice_type IN {ADVICE_TYPE_VALUES}",
            name="ck_seac_advice_type",
        ),
        CheckConstraint(
            f"severity IN {ADVICE_SEVERITY_VALUES}",
            name="ck_seac_severity",
        ),
        UniqueConstraint(
            "evaluation_id", "reason_signature",
            name="uq_seac_evaluation_reason",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("seac"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    advice_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Over advice type, sorted signal IDs, sorted segment keys, Policy
    # version, Target/no-target, algorithm version, and metric-contract-set
    # and configuration-baseline hashes (spec §2.5).
    reason_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvaluationStreakCounterRecord(Base):
    """One immutable per-signal consecutive-hit counter owned by a
    `ScaleEvaluationRecord`. `StreakState.healthy_evaluations` is inlined on
    the parent Evaluation instead (a single scalar, not a per-key list).
    Spec §1.9; mirrors §2.6 `StreakCounter`."""

    __tablename__ = "scale_evaluation_streak_counters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id"],
            name="fk_sesc_evaluation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sesc_meta_mode",
        ),
        UniqueConstraint(
            "evaluation_id", "signal_key",
            name="uq_sesc_evaluation_signal",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sesc"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signal_key: Mapped[str] = mapped_column(String(100), nullable=False)
    consecutive_hits: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEvaluationStreakResetRecord(Base):
    """One immutable streak reset owned by a `ScaleEvaluationRecord`.
    `streak_id` is `"healthy_evaluations"` or the exact signal/segment key.
    Empty (no rows for an Evaluation) is the normal no-reset case. Spec
    §1.9; mirrors §2.6 `StreakReset`."""

    __tablename__ = "scale_evaluation_streak_resets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id"],
            name="fk_sesr_evaluation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sesr_meta_mode",
        ),
        CheckConstraint(
            "previous_value >= 0",
            name="ck_sesr_prev_value",
        ),
        UniqueConstraint(
            "evaluation_id", "streak_id", "reason_code",
            name="uq_sesr_evaluation_streak_reason",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sesr"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    streak_id: Mapped[str] = mapped_column(String(100), nullable=False)
    previous_value: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleEpisodeProjectionRecord(Base):
    """Replaceable cached projection referencing the accepted immutable
    Evaluation. Never source of truth; historical, stale, and intermediate
    replay Evaluations cannot update it -- only a projection-eligible result
    that wins the structured cursor CAS may. One row per Episode. Spec
    §1.9."""

    __tablename__ = "scale_episode_projections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sepp_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id"],
            name="fk_sepp_evaluation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sepp_meta_mode",
        ),
        CheckConstraint(
            f"maturity IN {EVIDENCE_MATURITY_VALUES}",
            name="ck_sepp_maturity",
        ),
        CheckConstraint(
            f"health IS NULL OR health IN {SCALE_HEALTH_VALUES}",
            name="ck_sepp_health",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "episode_id",
            name="uq_sepp_episode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sepp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    maturity: Mapped[str] = mapped_column(String(30), nullable=False)
    health: Mapped[str | None] = mapped_column(String(30), nullable=True)
    health_reason: Mapped[str] = mapped_column(String(60), nullable=False)
    healthy_evaluations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Cached mirror of the accepted Evaluation's
    # `ScaleEvaluationStreakCounterRecord` children, for fast projection
    # reads without a join.
    streak_counters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Structured projection cursor: `(as_of_local_day, source_revision,
    # semantic_event_sequence, lineage_generation, evaluation_id)`. Ordering
    # by this cursor -- not database insertion time or lexical Evaluation
    # ID -- decides projection eligibility (spec §3.4 step 3).
    cursor_as_of_local_day: Mapped[date] = mapped_column(Date, nullable=False)
    cursor_source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor_semantic_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor_lineage_generation: Mapped[int] = mapped_column(Integer, nullable=False)

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.10 Advice records
#
# `ScaleAdviceThreadRecord` is the CAS-maintained projection of one active
# semantic Advice identity, `(workspace_id, meta_mode, episode_id,
# advice_type, reason_signature)`. Terminal threads are never reactivated;
# recurrence creates a new generation via `predecessor_thread_id`. The
# partial unique index that permits only one active thread per identity
# (`WHERE lifecycle = 'active'`) is PostgreSQL DDL deferred to the Task 6
# migration, per the module-level note above §1.7. Occurrence, thread-event,
# disposition, snooze, and notification-decision records are immutable and
# append-only; user roles never insert `ScaleAdviceThreadEventRecord` rows
# (those are system lifecycle events only). Every occurrence's composite
# foreign key proving its snapshot equals its Evaluation's snapshot is also
# deferred to Task 6.
# ---------------------------------------------------------------------------

class ScaleAdviceThreadRecord(Base):
    """CAS projection for one active semantic Advice identity. Spec §1.10."""

    __tablename__ = "scale_advice_threads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sat_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sat_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sat_enrollment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sat_meta_mode",
        ),
        CheckConstraint(
            f"advice_type IN {ADVICE_TYPE_VALUES}",
            name="ck_sat_advice_type",
        ),
        CheckConstraint(
            f"severity IN {ADVICE_SEVERITY_VALUES}",
            name="ck_sat_severity",
        ),
        CheckConstraint(
            f"lifecycle IN {ADVICE_LIFECYCLE_VALUES}",
            name="ck_sat_lifecycle",
        ),
        CheckConstraint(
            f"disposition_kind IS NULL OR disposition_kind IN {ADVICE_DISPOSITION_KIND_VALUES}",
            name="ck_sat_disposition_kind",
        ),
        CheckConstraint(
            "(lifecycle = 'resolved') = (resolved_at IS NOT NULL)",
            name="ck_sat_resolved_shape",
        ),
        # Deferred to Task 6 (migration): partial unique index permitting one
        # active thread per identity,
        #   ix_sat_active_identity on
        #   (workspace_id, meta_mode, episode_id, advice_type, reason_signature)
        #   WHERE lifecycle = 'active'
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sat"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    advice_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Semantic identity dimension shared with ScaleEvaluationAdviceCandidateRecord
    # (spec §2.5): over advice type, sorted signal IDs, sorted segment keys,
    # Policy version, Target/no-target, algorithm version, and metric-contract-
    # set/configuration-baseline hashes.
    reason_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # A terminal thread is never reactivated; recurrence starts a new
    # generation pointing back at the terminal thread it recurs from.
    predecessor_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_occurrence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    latest_occurrence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Absence of a disposition means unreviewed (spec §1.2); mirrors the
    # latest ScaleAdviceDispositionRecord for this thread.
    disposition_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    disposition_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Current active ScaleAdviceSnoozeRecord's `snooze_until`, or null.
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifecycle_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleAdviceOccurrenceRecord(Base):
    """One immutable Advice occurrence materializing one evaluation's Advice
    candidate into the Advice-lifecycle namespace. Unique per Evaluation/
    type/reason-signature in tenant/mode scope. Its composite foreign key
    proving `snapshot_id` equals the referenced Evaluation's `snapshot_id` is
    deferred to Task 6, per the module-level note above §1.7. Spec §1.10."""

    __tablename__ = "scale_advice_occurrences"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sao_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sao_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sao_enrollment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "evaluation_id", "snapshot_id"],
            ["scale_evaluations.workspace_id", "scale_evaluations.meta_mode", "scale_evaluations.id", "scale_evaluations.snapshot_id"],
            name="fk_sao_evaluation_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_sao_thread",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sao_meta_mode",
        ),
        CheckConstraint(
            f"advice_type IN {ADVICE_TYPE_VALUES}",
            name="ck_sao_advice_type",
        ),
        CheckConstraint(
            f"severity IN {ADVICE_SEVERITY_VALUES}",
            name="ck_sao_severity",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "evaluation_id", "advice_type", "reason_signature",
            name="uq_sao_evaluation_reason",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sao"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The ScaleEvaluationAdviceCandidateRecord this occurrence materializes.
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Freezes the Evaluation's snapshot id; a composite FK (deferred to Task
    # 6) proves this equals the referenced Evaluation's own `snapshot_id`.
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    advice_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleAdviceThreadEventRecord(Base):
    """Append-only, immutable Advice thread lifecycle transition (created,
    resolved, superseded, expired). System-only: user roles cannot insert
    these rows. Spec §1.10."""

    __tablename__ = "scale_advice_thread_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sate_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_sate_thread",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "deduplication_key",
            name="uq_sate_workspace_mode_dedup",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sate_meta_mode",
        ),
        CheckConstraint(
            f"event_type IN {ADVICE_THREAD_EVENT_TYPE_VALUES}",
            name="ck_sate_event_type",
        ),
        CheckConstraint(
            f"previous_lifecycle IS NULL OR previous_lifecycle IN {ADVICE_LIFECYCLE_VALUES}",
            name="ck_sate_previous_lifecycle",
        ),
        CheckConstraint(
            f"current_lifecycle IN {ADVICE_LIFECYCLE_VALUES}",
            name="ck_sate_current_lifecycle",
        ),
        CheckConstraint(
            "(event_type != 'superseded') OR (successor_thread_id IS NOT NULL)",
            name="ck_sate_superseded_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sate"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The triggering occurrence, when this transition was occurrence-driven
    # (e.g. `created`); null for a purely time-based transition (`expired`).
    occurrence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_lifecycle: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_lifecycle: Mapped[str] = mapped_column(String(20), nullable=False)
    # The new-generation thread this thread was superseded by.
    successor_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleAdviceDispositionRecord(Base):
    """Append-only user disposition (acknowledge/dismiss/convert) against one
    Advice thread's current occurrence. `linked_conversion_id` is required
    only for the `converted_to_draft` kind. Spec §1.10."""

    __tablename__ = "scale_advice_dispositions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_sad_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "occurrence_id"],
            ["scale_advice_occurrences.workspace_id", "scale_advice_occurrences.meta_mode", "scale_advice_occurrences.id"],
            name="fk_sad_occurrence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sad_meta_mode",
        ),
        CheckConstraint(
            f"kind IN {ADVICE_DISPOSITION_KIND_VALUES}",
            name="ck_sad_kind",
        ),
        CheckConstraint(
            "(kind = 'converted_to_draft') = (linked_conversion_id IS NOT NULL)",
            name="ck_sad_conversion_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sad"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurrence_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The ScaleAdviceConversionRecord created by a `converted_to_draft`
    # disposition; required only for that kind (see CHECK above).
    linked_conversion_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # Caller-supplied replay key (Issue #25 Task 15 fix round 1): a retry of
    # the same HTTP command against the same thread with the same key
    # returns the existing row instead of inserting a duplicate. Nullable so
    # a caller that supplies none (or a row written before this column
    # existed) is unaffected; the partial unique index enforcing this is
    # PostgreSQL-only DDL (`ix_sad_thread_idempotency`, migration Step 11),
    # matching every other Scale uniqueness gate's SQLite-vs-PostgreSQL split.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleAdviceSnoozeRecord(Base):
    """Append-only snooze against one Advice thread, with actor, start,
    `snooze_until`, reason, and cancellation linkage. Spec §1.10."""

    __tablename__ = "scale_advice_snoozes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_sas_thread",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sas_meta_mode",
        ),
        CheckConstraint(
            "snooze_until > started_at",
            name="ck_sas_until_after_start",
        ),
        CheckConstraint(
            "(cancelled_at IS NULL) = (cancelled_by IS NULL)",
            name="ck_sas_cancel_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sas"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    snooze_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # See ScaleAdviceDispositionRecord.idempotency_key above -- same shape,
    # same PostgreSQL-only partial unique index (`ix_sas_thread_idempotency`).
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleAdviceNotificationDecisionRecord(Base):
    """Immutable send-or-suppress notification decision, one per triggering
    occurrence. Shared notification outbox records are only created for a
    `send` decision. Spec §1.10."""

    __tablename__ = "scale_advice_notification_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_sand_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "occurrence_id"],
            ["scale_advice_occurrences.workspace_id", "scale_advice_occurrences.meta_mode", "scale_advice_occurrences.id"],
            name="fk_sand_occurrence",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "occurrence_id",
            name="uq_sand_occurrence",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sand_meta_mode",
        ),
        CheckConstraint(
            f"decision IN {ADVICE_NOTIFICATION_DECISION_VALUES}",
            name="ck_sand_decision",
        ),
        CheckConstraint(
            "(decision = 'suppress') = (suppression_reason IS NOT NULL)",
            name="ck_sand_suppress_shape",
        ),
        CheckConstraint(
            "(decision = 'send') = (notification_outbox_id IS NOT NULL)",
            name="ck_sand_send_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sand"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurrence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    suppression_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cooldown_hours_applied: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The shared notification outbox record id, present only for `send`.
    notification_outbox_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §1.11 Conversion, outcomes, inspection, and export
# ---------------------------------------------------------------------------

class ScaleAdviceConversionRecord(Base):
    """Unique by Workspace, mode, and occurrence. Freezes input lineage and
    has exactly one output: a Generation-owned unpaid `GenerationDraft`, or a
    Scale-owned non-executable `ScaleTestPlanDraftRecord`. The CHECK below
    enforces the exclusive output; composite foreign keys proving each
    non-null id actually resolves in its owning table are deferred to Task 6.
    Spec §1.11."""

    __tablename__ = "scale_advice_conversions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_sacr_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_sacr_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "occurrence_id"],
            ["scale_advice_occurrences.workspace_id", "scale_advice_occurrences.meta_mode", "scale_advice_occurrences.id"],
            name="fk_sacr_occurrence_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "generation_draft_id"],
            ["generation_drafts.workspace_id", "generation_drafts.id"],
            name="fk_sacr_generation_draft",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "scale_test_plan_draft_id"],
            ["scale_test_plan_drafts.workspace_id", "scale_test_plan_drafts.meta_mode", "scale_test_plan_drafts.id"],
            name="fk_sacr_test_plan_draft",
            ondelete="RESTRICT",
            # Breaks the scale_advice_conversions <-> scale_test_plan_drafts
            # cycle (a conversion optionally points at the draft it created;
            # the draft always points back at its originating conversion) so
            # SQLAlchemy's table-creation sort does not warn/skip either FK.
            # The Task 6 migration installs both constraints explicitly and
            # is unaffected by this flag either way.
            use_alter=True,
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "occurrence_id",
            name="uq_sacr_occurrence",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sacr_meta_mode",
        ),
        CheckConstraint(
            "(generation_draft_id IS NOT NULL AND scale_test_plan_draft_id IS NULL) "
            "OR (generation_draft_id IS NULL AND scale_test_plan_draft_id IS NOT NULL)",
            name="ck_sacr_exclusive_output",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sacr"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurrence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Exactly one of the two is set; see CHECK above.
    generation_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scale_test_plan_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_lineage_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleTestPlanDraftRecord(Base):
    """Scale-owned, non-executable test-plan draft: an immutable generated
    section (criteria, lineage) plus a revisioned editable projection
    (planned changes, observation notes). It never stores an executable Meta
    command. Spec §1.11."""

    __tablename__ = "scale_test_plan_drafts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_stpd_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "conversion_id"],
            ["scale_advice_conversions.workspace_id", "scale_advice_conversions.meta_mode", "scale_advice_conversions.id"],
            name="fk_stpd_conversion",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_stpd_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_stpd_enrollment",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "conversion_id",
            name="uq_stpd_conversion",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_stpd_meta_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("stpd"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    conversion_id: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Immutable generated section.
    generated_criteria: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_lineage_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    # Revisioned editable projection.
    planned_changes: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleAdviceOutcomeRecord(Base):
    """Append-only outcome evidence for one Advice occurrence. Must reference
    trusted subsequent owned-module evidence -- disposition, notes, or
    screenshots cannot be its sole evidence, so `evidence_source_module` and
    `evidence_source_record_id` are required, non-null columns. Workspace and
    mode match the referenced Advice occurrence. Spec §1.11."""

    __tablename__ = "scale_advice_outcomes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "thread_id"],
            ["scale_advice_threads.workspace_id", "scale_advice_threads.meta_mode", "scale_advice_threads.id"],
            name="fk_saor_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "occurrence_id"],
            ["scale_advice_occurrences.workspace_id", "scale_advice_occurrences.meta_mode", "scale_advice_occurrences.id"],
            name="fk_saor_occurrence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_saor_meta_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("saor"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurrence_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The trusted owned-module record this outcome evidence comes from (e.g.
    # a Generation, Experiment, or later Scale Evaluation record) -- never a
    # user disposition, note, or screenshot standing alone.
    evidence_source_module: Mapped[str] = mapped_column(String(60), nullable=False)
    evidence_source_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    evidence_source_record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleUnobservedFactorRecord(Base):
    """Append-only, Episode-scoped note of an unobserved external factor,
    revisioned only by correction links (`corrects_factor_id`) rather than
    update-in-place. May reference a safe uploaded object through the
    existing object boundary. Never linked into an evaluator snapshot. Spec
    §1.11."""

    __tablename__ = "scale_unobserved_factors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "meta_mode", "id", name="uq_suf_ws_mode_id"),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_suf_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "corrects_factor_id"],
            ["scale_unobserved_factors.workspace_id", "scale_unobserved_factors.meta_mode", "scale_unobserved_factors.id"],
            name="fk_suf_corrects_factor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_suf_meta_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("suf"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    factor_type: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # A safe uploaded object reference via the existing object boundary; the
    # raw object payload/key never enters this row directly.
    object_reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The earlier ScaleUnobservedFactorRecord this row corrects, if any --
    # append-only correction chain rather than an in-place edit.
    corrects_factor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(100), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class ScaleExportRecord(Base):
    """References a durable Operation and immutable Enrollment, Episode, and
    optional snapshot/evaluation validators. Stores only a safe result
    reference after atomic object commit -- raw payloads and object keys
    never enter Scale responses; download intents use the existing
    authorized object seam. Spec §1.11."""

    __tablename__ = "scale_exports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "episode_id"],
            ["scale_episodes.workspace_id", "scale_episodes.meta_mode", "scale_episodes.id"],
            name="fk_sexp_episode",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode", "scale_enrollments.id"],
            name="fk_sexp_enrollment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_sexp_operation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sexp_meta_mode",
        ),
        CheckConstraint(
            "(completed_at IS NULL) = (result_object_reference IS NULL)",
            name="ck_sexp_result_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sexp"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # The durable Operation this export request is tracked under.
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    export_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    # Safe result reference (never a raw object key/payload), populated only
    # after atomic object commit; downloads use the existing authorized
    # object seam.
    result_object_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# Mirror the PostgreSQL partial unique indexes installed by migration Step 11.
# ``ddl_if`` keeps create_all-based SQLite tests aligned with the migration,
# which intentionally installs these database-level guards only on PostgreSQL.
Index(
    "ix_senr_active_binding",
    ScaleEnrollmentRecord.workspace_id,
    ScaleEnrollmentRecord.meta_mode,
    ScaleEnrollmentRecord.meta_connection_id,
    ScaleEnrollmentRecord.ad_account_id,
    ScaleEnrollmentRecord.remote_binding_key,
    unique=True,
    postgresql_where=text("lifecycle IN ('active', 'paused')"),
).ddl_if(dialect="postgresql")
Index(
    "ix_sat_active_thread",
    ScaleAdviceThreadRecord.workspace_id,
    ScaleAdviceThreadRecord.meta_mode,
    ScaleAdviceThreadRecord.episode_id,
    ScaleAdviceThreadRecord.advice_type,
    ScaleAdviceThreadRecord.reason_signature,
    unique=True,
    postgresql_where=text("lifecycle = 'active'"),
).ddl_if(dialect="postgresql")
Index(
    "ix_stgt_active_target",
    ScaleTargetVersionRecord.workspace_id,
    ScaleTargetVersionRecord.product_version_id,
    ScaleTargetVersionRecord.offer_version_id,
    ScaleTargetVersionRecord.market_code,
    ScaleTargetVersionRecord.currency,
    unique=True,
    postgresql_where=text("lifecycle_state = 'active'"),
).ddl_if(dialect="postgresql")
Index(
    "ix_sad_thread_idempotency",
    ScaleAdviceDispositionRecord.workspace_id,
    ScaleAdviceDispositionRecord.meta_mode,
    ScaleAdviceDispositionRecord.thread_id,
    ScaleAdviceDispositionRecord.idempotency_key,
    unique=True,
    postgresql_where=text("idempotency_key IS NOT NULL"),
).ddl_if(dialect="postgresql")
Index(
    "ix_sas_thread_idempotency",
    ScaleAdviceSnoozeRecord.workspace_id,
    ScaleAdviceSnoozeRecord.meta_mode,
    ScaleAdviceSnoozeRecord.thread_id,
    ScaleAdviceSnoozeRecord.idempotency_key,
    unique=True,
    postgresql_where=text("idempotency_key IS NOT NULL"),
).ddl_if(dialect="postgresql")


# ---------------------------------------------------------------------------
# §4.3 ScaleDeliveryTargetRecord (issue #86 lineage hardening, Task 7)
# ---------------------------------------------------------------------------
# Physical table created by migrations/versions/ab1_scale_lineage_tables.py,
# extended by Task 8 with CommandJournal, LineageGuard, Candidate,
# EvidenceSnapshot and ActivationPreview tables.

SOURCE_TYPE_VALUES = "('curated_scenario', 'evidence_observation')"


class ScaleDeliveryTargetRecord(Base):
    """Proves the complete delivery context for one Scale Enrollment slot.

    v3's delivery-target row only stored the Meta remote hierarchy. This one
    independently proves the *complete* delivery context: the remote
    hierarchy, its source provenance per level, the creative content bound to
    it, market, attribution, and the exact library versions -- everything
    ``lock_and_validate_enrollment_lineage`` (Task 9) needs to prove a Winner
    and a delivery slot are the same offer, market, placement and
    attribution, not just that both happen to exist. Spec §4.3.

    Naming: ``adset_remote_id`` (no underscore) matches the Meta API
    convention and the ``FakeMetaObjectRecord.object_type`` vocabulary
    (``"campaign"``, ``"adset"``, ``"ad"``). ``ScaleEnrollmentRecord``'s
    column is ``ad_set_remote_id`` (with underscore); this migration does
    NOT rename it. The divergence between the two tables is intentional and
    documented here, not a mistake -- see the Global Constraints in this
    issue's plan.

    Composite-FK scope note: every composite FK below targets an upstream
    unique constraint that already exists, except two that this table's own
    migration adds (``meta_connection_records`` gains
    ``(workspace_id, meta_mode, id, ad_account_id)``;
    ``observation_contract_generation_records`` gains
    ``(workspace_id, meta_mode, id, meta_connection_id, ad_account_id)`` and
    ``(workspace_id, meta_mode, id, meta_connection_id)``), and one the spec
    calls for that this task deliberately leaves unenforced at the database
    level: ``source_attestation_id`` -> ``evidence_attestation_records``.
    That table has no ``sync_run_id`` column yet (spec §4.3's target is
    ``(workspace_id, meta_mode, id, contract_generation_id, sync_run_id,
    publication_id)``), and adding one is a schema change to a table this
    task does not own (``app/meta/models.py``). ``source_attestation_id`` is
    still stored, still required by the source-type CHECK below when
    ``source_type = 'evidence_observation'``, and still hashed into
    ``source_manifest_hash`` -- only the extra database-level FK proof is
    deferred.
    """

    __tablename__ = "scale_delivery_targets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "meta_connection_id", "ad_account_id"],
            ["meta_connection_records.workspace_id",
             "meta_connection_records.meta_mode",
             "meta_connection_records.id",
             "meta_connection_records.ad_account_id"],
            name="fk_sdt_connection",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "contract_generation_id",
             "meta_connection_id", "ad_account_id"],
            ["observation_contract_generation_records.workspace_id",
             "observation_contract_generation_records.meta_mode",
             "observation_contract_generation_records.id",
             "observation_contract_generation_records.meta_connection_id",
             "observation_contract_generation_records.ad_account_id"],
            name="fk_sdt_contract_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "creative_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_sdt_creative_asset_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_sdt_offer_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "landing_page_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_sdt_landing_page_version",
            ondelete="RESTRICT",
        ),
        # Nullable composite FKs: PostgreSQL's default MATCH SIMPLE means a
        # NULL in any referencing column exempts the row from the check, so
        # each of these is only actually enforced for the one source_type
        # that populates it -- exactly the source-type biconditional CHECK
        # below, proven twice (application-level CHECK, and structurally by
        # the FK only ever having a target to check when the column is set).
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "scenario_version_id"],
            ["scenario_version_records.workspace_id",
             "scenario_version_records.meta_mode",
             "scenario_version_records.id"],
            name="fk_sdt_scenario_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "source_sync_run_id", "contract_generation_id"],
            ["meta_sync_run_records.workspace_id",
             "meta_sync_run_records.meta_mode",
             "meta_sync_run_records.id",
             "meta_sync_run_records.contract_generation_id"],
            name="fk_sdt_source_sync_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "source_publication_id", "source_sync_run_id"],
            ["evidence_publication_records.workspace_id",
             "evidence_publication_records.meta_mode",
             "evidence_publication_records.id",
             "evidence_publication_records.sync_run_id"],
            name="fk_sdt_source_publication",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", name="uq_sdt_ws_mode_id",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", "scenario_scope_key",
            name="uq_sdt_ws_mode_id_scenario",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", "meta_connection_id",
            "ad_account_id", "scenario_scope_key", "campaign_remote_id",
            "adset_remote_id", "ad_remote_id", "ad_creative_id",
            "creative_asset_version_id", "market_code", "placement",
            "attribution_setting", "offer_version_id",
            "landing_page_version_id", "target_fingerprint",
            name="uq_sdt_exact_context",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", "meta_connection_id",
            "ad_account_id", "scenario_scope_key", "campaign_remote_id",
            "adset_remote_id", "ad_remote_id", "creative_asset_version_id",
            "market_code", "offer_version_id", "landing_page_version_id",
            "target_fingerprint",
            name="uq_sdt_enrollment_binding",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sdt_meta_mode",
        ),
        CheckConstraint(
            f"source_type IN {SOURCE_TYPE_VALUES}",
            name="ck_sdt_source_type",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_sdt_scenario_biconditional",
        ),
        # Scale Enrollment never targets a partial hierarchy: every level is
        # NOT NULL already, but a stray empty string would satisfy NOT NULL
        # while still being a partial/absent identifier.
        CheckConstraint(
            "length(campaign_remote_id) > 0 AND length(adset_remote_id) > 0 "
            "AND length(ad_remote_id) > 0",
            name="ck_sdt_hierarchy_non_empty",
        ),
        # Source-type biconditional: exactly one provenance shape per row.
        CheckConstraint(
            "(source_type = 'curated_scenario' "
            "AND scenario_version_id IS NOT NULL "
            "AND source_sync_run_id IS NULL "
            "AND source_publication_id IS NULL "
            "AND source_attestation_id IS NULL) "
            "OR "
            "(source_type = 'evidence_observation' "
            "AND scenario_version_id IS NULL "
            "AND source_sync_run_id IS NOT NULL "
            "AND source_publication_id IS NOT NULL "
            "AND source_attestation_id IS NOT NULL)",
            name="ck_sdt_source_type_biconditional",
        ),
        # curated_scenario only ever comes from sandbox/mock provisioning;
        # evidence_observation only ever comes from internal_validation
        # sync runs. Spec §4.3.
        CheckConstraint(
            "(source_type = 'curated_scenario' "
            "AND meta_mode IN ('customer_sandbox', 'controlled_mock')) "
            "OR "
            "(source_type = 'evidence_observation' "
            "AND meta_mode = 'internal_validation')",
            name="ck_sdt_source_type_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sdt"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)

    # -- Meta hierarchy (per-level source provenance, not polymorphic) ------
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_remote_id: Mapped[str] = mapped_column(String(100), nullable=False)
    adset_remote_id: Mapped[str] = mapped_column(String(100), nullable=False)
    ad_remote_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # -- Delivery content -----------------------------------------------------
    ad_creative_id: Mapped[str] = mapped_column(String(100), nullable=False)
    creative_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creative_binding_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    market_code: Mapped[str] = mapped_column(String(10), nullable=False)
    placement: Mapped[str] = mapped_column(String(60), nullable=False)

    # -- Attribution ----------------------------------------------------------
    # Stores the Winner's SHA-256 attribution-semantics hash (64 hex chars),
    # not a short Meta UI label.
    attribution_setting: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # -- Library versions bound to this delivery slot --------------------------
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    landing_page_version_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # -- Source provenance (typed, individual per hierarchy level) -----------
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scenario_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_sync_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_publication_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # No composite FK yet -- see class docstring's "Composite-FK scope note".
    source_attestation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # -- Identity ---------------------------------------------------------------
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_binding_key: Mapped[str] = mapped_column(String(200), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §7.1 ScaleCommandJournalRecord (issue #86 hardening, Task 8)
# ---------------------------------------------------------------------------

SCALE_COMMAND_TYPE_VALUES = (
    "('create_candidate', 'create_enrollment', 'create_preview', "
    "'activate_enrollment', 'resume_enrollment', "
    "'create_target_publication_preview', 'publish_scale_target', "
    "'create_target_withdrawal_preview', 'withdraw_scale_target', "
    "'create_scale_export')"
)


class ScaleCommandJournalRecord(Base):
    """Unified, atomically-claimed idempotency journal for every candidate/
    enrollment-activation mutation created or materially rewritten by #86.

    ``claim_command()`` (``app/scale/command_journal.py``) is the only
    writer: an ``INSERT ... ON CONFLICT (workspace_id, meta_mode,
    command_type, dedup_key) DO NOTHING`` that either wins the claim or hands
    the caller the already-committed row to compare fingerprints against --
    no SELECT-then-INSERT window. This is the authoritative idempotency
    check for the five journaled command types and runs before any lifecycle
    check; ``ScaleEnrollmentEventRecord.deduplication_key`` remains a
    secondary safety net, both written in the same transaction. Spec §7.1.
    """

    __tablename__ = "scale_command_journal"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "meta_mode", "command_type", "dedup_key",
            name="uq_scj_ws_mode_type_dedup",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_scj_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_scj_scenario_biconditional",
        ),
        CheckConstraint(
            f"command_type IN {SCALE_COMMAND_TYPE_VALUES}",
            name="ck_scj_command_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("scj"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    command_type: Mapped[str] = mapped_column(String(60), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    # Server-derived identity hash for the five Task 1 Target/Export command
    # types (issue #87), letting `claim_command()` detect a duplicate request
    # even when its caller-supplied `dedup_key` differs. Nullable: the five
    # pre-#87 command types never populate it. Enforced unique only where
    # non-null via the partial index below (PostgreSQL only, mirroring every
    # other partial-unique guard in this module).
    identity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


# Mirrors the PostgreSQL partial unique index installed by the Task 1
# migration -- see the module docstring's ``ddl_if`` note above.
Index(
    "ix_scj_ws_mode_identity_hash",
    ScaleCommandJournalRecord.workspace_id,
    ScaleCommandJournalRecord.meta_mode,
    ScaleCommandJournalRecord.identity_hash,
    unique=True,
    postgresql_where=text("identity_hash IS NOT NULL"),
).ddl_if(dialect="postgresql")


# ---------------------------------------------------------------------------
# §7.3 ScaleLineageGuardRecord (issue #86 hardening, Task 8)
# ---------------------------------------------------------------------------

class ScaleLineageGuardRecord(Base):
    """One persistent, revision-bumped guard per exact Winner/DeliveryTarget
    pair, closing the validation-to-COMMIT TOCTOU window a fingerprint
    comparison alone cannot close (spec §7.3).

    ``get_or_create_lineage_guard()`` (``app/scale/lineage_guard.py``)
    creates the row with ``INSERT ... ON CONFLICT (workspace_id, meta_mode,
    winner_decision_id, delivery_target_id) DO NOTHING`` and always returns
    it ``SELECT ... FOR UPDATE``-locked, whether this call created it or an
    earlier one did. PostgreSQL triggers on every eligibility-affecting
    upstream table call the SECURITY DEFINER
    ``marketai_bump_scale_lineage_guards()`` (installed by this table's
    migration) to increment ``revision`` in the same transaction as the
    upstream mutation, so a Scale command that locked the guard first orders
    its COMMIT before any such mutation's trigger can run (spec §7.4).

    Composite-FK scope note (mirrors ``ScaleDeliveryTargetRecord``'s own,
    see that class's docstring): ``winner_decision_id`` ->
    ``experiment_decisions(workspace_id, id)``, ``winner_confirmation_id`` ->
    ``decision_confirmations(workspace_id, id)``, and
    ``market_asset_binding_id`` -> ``market_asset_bindings(workspace_id,
    id)`` are NOT installed as database-level FKs here. Each upstream
    record's own composite-FK chain (``experiment_decisions`` into
    ``experiments``/``judgements``/``experiment_entries``;
    ``market_asset_bindings`` into the full Localization item/batch/
    publication chain) belongs to domains this task does not own, and
    reproducing those pipelines end-to-end in every guard-creating test is
    out of Task 8's scope. Lineage validation itself (Task 9's
    ``lock_and_validate_enrollment_lineage``) re-reads and authoritatively
    checks all three by id under this lock; only the extra database-level FK
    proof is deferred, exactly as ``ScaleDeliveryTargetRecord`` already
    deferred one FK (``source_attestation_id``) for the same reason.
    """

    __tablename__ = "scale_lineage_guards"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "meta_mode", "winner_decision_id", "delivery_target_id",
            name="uq_slg_ws_mode_winner_target",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", name="uq_slg_ws_mode_id",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", "scenario_scope_key",
            name="uq_slg_ws_mode_id_scenario",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_slg_asset_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_slg_product_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_slg_offer_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "delivery_target_id", "scenario_scope_key"],
            ["scale_delivery_targets.workspace_id", "scale_delivery_targets.meta_mode",
             "scale_delivery_targets.id", "scale_delivery_targets.scenario_scope_key"],
            name="fk_slg_delivery_target",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "contract_generation_id", "meta_connection_id"],
            ["observation_contract_generation_records.workspace_id",
             "observation_contract_generation_records.meta_mode",
             "observation_contract_generation_records.id",
             "observation_contract_generation_records.meta_connection_id"],
            name="fk_slg_contract_generation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_slg_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_slg_scenario_biconditional",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("slg"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    winner_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    winner_confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_asset_binding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §4.4 ScaleEnrollmentCandidateRecord (issue #86 hardening, Task 9)
# ---------------------------------------------------------------------------

class ScaleEnrollmentCandidateRecord(Base):
    """A short-lived, server-derived proof that one Winner Decision and one
    DeliveryTarget were, at some moment, an exactly-compatible lineage pair
    (spec §4.4). ``app.scale.candidates.create_enrollment_candidate()`` is
    the only writer: it calls
    ``app.scale.enrollment_lineage.lock_and_validate_enrollment_lineage()``
    (which itself locks/creates the ``ScaleLineageGuardRecord`` for the
    pair) and freezes the resulting ``candidate_fingerprint`` plus the
    guard's revision *at creation time* onto this row.

    A candidate is single-use: ``app.scale.candidates.consume_candidate()``
    atomically claims it via ``UPDATE ... WHERE consumed_at IS NULL``. Being
    consumed does not, by itself, prove the lineage is still valid at the
    moment of consumption -- ``app.scale.enrollment.create_draft_enrollment()``
    re-runs ``lock_and_validate_enrollment_lineage()`` under the (now
    re-locked) guard before trusting this row's frozen fields, exactly as
    the guard itself is always re-validated rather than blindly trusted
    (see ``ScaleLineageGuardRecord``'s own docstring). This row exists so
    the browser can be handed one opaque, narrow-scoped id instead of the
    raw Winner/DeliveryTarget identifiers themselves (spec §4.4's whole
    reason for existing) and so an Enrollment can be created from *exactly*
    the pairing a caller was shown, not a substituted one.

    ``winner_decision_id`` carries no database-level FK, mirroring
    ``ScaleLineageGuardRecord``'s identical, documented omission (see that
    class's docstring's "Composite-FK scope note").
    """

    __tablename__ = "scale_enrollment_candidates"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", name="uq_secand_ws_mode_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "lineage_guard_id"],
            ["scale_lineage_guards.workspace_id", "scale_lineage_guards.meta_mode",
             "scale_lineage_guards.id"],
            name="fk_secand_guard",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "delivery_target_id", "scenario_scope_key"],
            ["scale_delivery_targets.workspace_id", "scale_delivery_targets.meta_mode",
             "scale_delivery_targets.id", "scale_delivery_targets.scenario_scope_key"],
            name="fk_secand_target",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_secand_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_secand_scenario_biconditional",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("secand"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    winner_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lineage_guard_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    guard_revision_at_creation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §6.11 ScaleDeliveryEvidenceSnapshotRecord (issue #86 hardening, Task 10)
# ---------------------------------------------------------------------------

class ScaleDeliveryEvidenceSnapshotRecord(Base):
    """A point-in-time capture of delivery evidence state bound to an exact
    delivery target + fingerprint. Activation preview (and later activation
    itself) references this immutable snapshot rather than re-querying
    volatile evidence tables at each step."""

    __tablename__ = "scale_delivery_evidence_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", name="uq_sdes_ws_mode_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "delivery_target_id"],
            ["scale_delivery_targets.workspace_id", "scale_delivery_targets.meta_mode",
             "scale_delivery_targets.id"],
            name="fk_sdes_target",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_sdes_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            # Freeze the identifier PostgreSQL previously derived by
            # truncating the longer logical name.  This keeps already-applied
            # schemas compatible while bringing the metadata name under the
            # 63-character PostgreSQL limit.
            name="ck_sdes_scenario_b_5b78",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sdes"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_generation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attestation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_watermark: Mapped[str] = mapped_column(String(200), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §4.5 ScaleActivationPreviewRecord (issue #86 hardening, Task 10)
# ---------------------------------------------------------------------------

class ScaleActivationPreviewRecord(Base):
    """A short-lived, server-derived proof that an enrollment's lineage and
    evidence were valid at preview-time. Activation (Task 11) consumes the
    preview, re-validates lineage, and compares lineage hashes to detect
    drift between preview and commit."""

    __tablename__ = "scale_activation_previews"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "meta_mode", "id", name="uq_saprev_ws_mode_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "enrollment_id"],
            ["scale_enrollments.workspace_id", "scale_enrollments.meta_mode",
             "scale_enrollments.id"],
            name="fk_saprev_enrollment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "delivery_evidence_snapshot_id"],
            ["scale_delivery_evidence_snapshots.workspace_id",
             "scale_delivery_evidence_snapshots.meta_mode",
             "scale_delivery_evidence_snapshots.id"],
            name="fk_saprev_evidence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"meta_mode IN {META_MODE_VALUES}",
            name="ck_saprev_meta_mode",
        ),
        CheckConstraint(
            SCENARIO_BICONDITIONAL_SQL,
            name="ck_saprev_scenario_biconditional",
        ),
        CheckConstraint(
            "transition_kind IN ('activate', 'resume')",
            name="ck_saprev_transition_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("saprev"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    enrollment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    transition_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    delivery_evidence_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lineage_guard_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    guard_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    lineage_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    enrollment_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
