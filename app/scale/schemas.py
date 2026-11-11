"""Pydantic request/response contracts for the Scale v1 HTTP surface
(Issue #25, Task 15).

Spec: docs/product/scale-advice-semantics-contract.md §4.1-§4.3, ADR 0008
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-15-brief.md

Every model forbids unknown fields (``extra="forbid"``) so a client typo
fails loudly (422) instead of being silently ignored. Money/ratio values
(``budget_amount``) cross the wire as canonical decimal strings, never
floats -- the router parses them into ``Decimal`` before calling the domain
layer and a malformed string is a 422, never a silent truncation.
Timestamps are offset-aware ``datetime`` values; FastAPI/Pydantic serialize
those as ISO 8601 with an explicit UTC offset, matching every other Scale
task's convention (``app.adapters.persistence.models.utcnow``).

These models are the outer HTTP shape only. The domain dataclasses they
adapt to/from (``DraftEnrollmentRequest`` in ``app.scale.enrollment``)
remain the source of truth for what a command actually requires; a field
added there without a matching field here is caught by the router's own
construction call, not silently dropped. ``EnrollmentActivateRequest``/
``EnrollmentResumeRequest`` are the one exception (issue #86 Task 11): both
now carry only ``activation_preview_id`` -- ``app.scale.enrollment.
DeliveryConfigBaseline``/``EpisodeContext`` are still real domain
dataclasses (used internally by ``app.scale.service``'s episode-rollover
path), but a browser can no longer supply either directly.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Action eligibility (spec §2): every response carries server-computed,
# never client-inferred, action eligibility.
# ---------------------------------------------------------------------------


class ActionEligibilityModel(_Base):
    action: str
    allowed: bool
    reason_code: str | None = None
    requirements: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Enrollment / Episode
# ---------------------------------------------------------------------------


class EnrollmentCreateRequest(_Base):
    """Issue #86 Task 9: the only field a caller may supply is the id of an
    already-proven ``ScaleEnrollmentCandidateRecord`` -- every raw Winner/
    DeliveryTarget identifier a pre-#86 client sent directly (and
    ``creator_id``, which the server now derives exclusively from the
    verified ``TenantContext``) is rejected as an unknown field by
    ``_Base``'s ``extra="forbid"``, surfacing as a 422."""

    enrollment_candidate_id: str = Field(min_length=1)


class CandidateCreateRequest(_Base):
    """Wire shape for creating one short-lived Enrollment Candidate (spec
    §4.4). ``winner_decision_id``/``delivery_target_id`` are still raw
    identifiers here -- that is expected: this is the one endpoint whose
    entire job is proving a caller-named pair is currently a valid lineage,
    before handing back the opaque, narrow-scoped candidate id every other
    Scale write actually consumes."""

    winner_decision_id: str = Field(min_length=1)
    delivery_target_id: str = Field(min_length=1)


class CandidateResponse(_Base):
    id: str
    meta_mode: str
    scenario_scope_key: str
    winner_decision_id: str
    delivery_target_id: str
    candidate_fingerprint: str
    expires_at: datetime
    consumed_at: datetime | None
    created_at: datetime


class ObservedAdResponse(_Base):
    """One ad a connection has observed (#129): the sandbox scenario's ads,
    or the ads of the connection's published delivery-configuration read."""

    meta_connection_id: str
    ad_account_id: str
    campaign_remote_id: str
    adset_remote_id: str
    ad_remote_id: str
    ad_creative_id: str
    source_type: str
    asset_version_ids: list[str]


class ObservedAdUnavailableResponse(_Base):
    meta_connection_id: str
    reason: str


class ObservedAdListResponse(_Base):
    items: list[ObservedAdResponse]
    unavailable: list[ObservedAdUnavailableResponse]


class MetricContractSetCreateRequest(_Base):
    """Set up a connection's Scale read contracts (#112, platform only).

    A real account's currency and time zone are frozen into its contracts;
    the account itself is read back and compared on every configuration
    read (``currency_mismatch`` / ``timezone_mismatch``). A sandbox
    connection may omit both: its scenario simulates them."""

    meta_connection_id: str = Field(min_length=1, max_length=64)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    timezone_name: str | None = Field(default=None, min_length=1, max_length=64)


class MetricContractSetGenerationResponse(_Base):
    contract_type: str
    generation_id: str
    schedule_state: str | None
    next_fire_at: datetime | None


class MetricContractSetResponse(_Base):
    id: str
    meta_mode: str
    meta_connection_id: str
    created: bool
    generations: list[MetricContractSetGenerationResponse]


class DeliveryTargetCreateRequest(_Base):
    """Pick one observed ad as a Winner's DeliveryTarget (#129). Only the
    ad is named: the server resolves the hierarchy, creative, Offer, Landing
    Page, market, placement and attribution from recorded evidence."""

    winner_decision_id: str = Field(min_length=1)
    meta_connection_id: str = Field(min_length=1)
    ad_remote_id: str = Field(min_length=1)


class DeliveryTargetResponse(_Base):
    id: str
    meta_mode: str
    meta_connection_id: str
    ad_account_id: str
    campaign_remote_id: str
    adset_remote_id: str
    ad_remote_id: str
    ad_creative_id: str
    creative_asset_version_id: str
    market_code: str
    placement: str
    offer_version_id: str
    landing_page_evidence_hash: str
    attribution_setting: str
    contract_generation_id: str
    source_type: str
    observed_at: datetime
    expires_at: datetime
    created_at: datetime


class EnrollmentActivateRequest(_Base):
    """Issue #86 Task 11: the only field a caller may supply is the id of an
    already-consumed-server-side ``ScaleActivationPreviewRecord`` --
    ``delivery_config``/``episode_context`` (the pre-hardening shape a
    browser could use to fabricate any lineage/evidence it liked) are
    rejected as unknown fields by ``_Base``'s ``extra="forbid"``, surfacing
    as a 422."""

    activation_preview_id: str = Field(min_length=1, max_length=64)


class EnrollmentPauseRequest(_Base):
    reason: str = "user_requested"


class EnrollmentResumeRequest(_Base):
    """Issue #86 Task 11: same preview-only shape as
    ``EnrollmentActivateRequest`` -- resume cannot bypass the preview
    protocol (spec §5.3), even when it will end up continuing an
    already-open Episode untouched."""

    activation_preview_id: str = Field(min_length=1, max_length=64)


class EnrollmentCloseRequest(_Base):
    reason: str = "user_requested"


class EnrollmentResponse(_Base):
    id: str
    meta_mode: str
    meta_connection_id: str
    scenario_scope_key: str
    ad_account_id: str
    winner_decision_id: str
    winner_confirmation_id: str
    asset_version_id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    landing_page_version_id: str | None
    market_asset_binding_id: str | None
    campaign_remote_id: str | None
    ad_set_remote_id: str | None
    ad_remote_id: str | None
    remote_binding_key: str
    lifecycle: str
    transition_reason: str | None
    current_episode_id: str | None
    creator_id: str
    original_initiator_id: str
    revision: int
    evaluation_cancellation_generation: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    allowed_actions: list[ActionEligibilityModel]


class EnrollmentListResponse(_Base):
    items: list[EnrollmentResponse]
    next_cursor: str | None = None


class EpisodeResponse(_Base):
    id: str
    enrollment_id: str
    meta_mode: str
    phase: str
    started_at: datetime
    started_local_day: date
    ended_at: datetime | None
    closure_reason: str | None
    delivery_evidence_hash: str
    delivery_comparability_hash: str
    metric_semantic_contract_set_id: str
    timezone_name: str
    currency: str
    attribution_semantics_hash: str
    revision: int
    projection_revision: int
    accepted_evaluation_id: str | None
    allowed_actions: list[ActionEligibilityModel]


class EvidenceWindowResponse(_Base):
    role: str
    start_local_day: date
    end_local_day: date


class EvidenceQualityIssueResponse(_Base):
    code: str
    severity: str
    is_blocking: bool
    contract_type: str | None
    window_role: str | None
    local_day: date | None
    segment_key: str | None
    detail: str | None
    recovery_action: str | None


class EvidenceSignalResponse(_Base):
    signal_id: str
    state: str
    expected: str | None
    observed: str | None
    interval_lower: str | None
    interval_upper: str | None
    threshold: str | None
    p_value: str | None
    adjusted_q_value: str | None
    reason_code: str


class EnrollmentEvidenceResponse(_Base):
    """Current evidence/readiness for an Enrollment's active Episode --
    the accepted-Evaluation projection (spec §3.4 step 3/4), not a raw
    evidence snapshot. Reflects only a projection-eligible result that has
    already won the structured cursor CAS."""

    enrollment_id: str
    episode_id: str
    evaluation_id: str
    snapshot_id: str
    policy_version_id: str
    target_version_id: str | None
    algorithm_version: str
    metric_semantic_contract_set_hash: str
    maturity: str
    data_quality_state: str
    health: str | None
    health_reason: str
    healthy_evaluations: int
    streak_counters: list[dict]
    safe_watermark: date
    last_complete_local_day: date
    last_sync_at: datetime
    freshness_reference_at: datetime
    complete_local_days_in_episode: int
    active_delivery_days_in_episode: int
    current_window: EvidenceWindowResponse
    baseline_window: EvidenceWindowResponse
    rolling_window: EvidenceWindowResponse
    quality_issues: list[EvidenceQualityIssueResponse]
    signals: list[EvidenceSignalResponse]
    cursor_as_of_local_day: date
    cursor_source_revision: int
    cursor_semantic_event_sequence: int
    cursor_lineage_generation: int
    revision: int
    updated_at: datetime


class EvidenceSnapshotResponse(_Base):
    """Sealed, immutable evaluator input (spec §1.8)."""

    id: str
    episode_id: str
    meta_mode: str
    claim_hash: str
    source_manifest_id: str
    source_manifest_hash: str
    source_revision: int
    safe_watermark: date
    canonical_hash: str | None
    sealed_at: datetime | None


class EvaluationResponse(_Base):
    """Immutable evaluator result (spec §1.9)."""

    id: str
    episode_id: str
    snapshot_id: str
    policy_version_id: str
    target_version_id: str | None
    algorithm_version: str
    maturity: str
    data_quality_state: str
    health: str | None
    health_reason: str
    streak_healthy_evaluations: int
    result_hash: str


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------


class AdviceThreadResponse(_Base):
    id: str
    episode_id: str
    enrollment_id: str
    advice_type: str
    reason_signature: str
    severity: str
    lifecycle: str
    predecessor_thread_id: str | None
    first_occurrence_id: str
    latest_occurrence_id: str
    disposition_kind: str | None
    disposition_at: datetime | None
    snooze_until: datetime | None
    opened_at: datetime
    resolved_at: datetime | None
    lifecycle_reason: str | None
    revision: int
    allowed_actions: list[ActionEligibilityModel]


class AdviceThreadListResponse(_Base):
    items: list[AdviceThreadResponse]
    next_cursor: str | None = None


class AdviceDispositionRequest(_Base):
    """``kind`` is validated by the domain layer
    (``app.scale.advice.create_disposition`` rejects everything but
    ``acknowledged``/``dismissed`` with ``unsupported_disposition_kind``,
    mapped to 422 by ``app.scale.v1_router``'s problem handler) rather than
    re-validated here, so that single rejection reason stays authoritative."""

    kind: str = Field(min_length=1)
    note: str | None = None
    occurrence_id: str | None = None


class AdviceDispositionResponse(_Base):
    id: str
    thread_id: str
    occurrence_id: str
    kind: str
    note: str | None
    actor_id: str
    created_at: datetime


class AdviceSnoozeRequest(_Base):
    until: datetime
    reason: str | None = None


class AdviceSnoozeResponse(_Base):
    id: str
    thread_id: str
    actor_id: str
    started_at: datetime
    snooze_until: datetime
    reason: str | None


class ConversionRequest(_Base):
    provider: str = "scale_advice_default"
    model: str = "scale_advice_default"
    refresh_axis: str | None = None


class ConversionResponse(_Base):
    id: str
    thread_id: str
    occurrence_id: str
    generation_draft_id: str | None
    scale_test_plan_draft_id: str | None
    input_lineage_hash: str
    initiator_id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Targets (Task 15 scoping: ``TargetCreateRequest`` still backs the existing
# 501 stub routes in ``app.scale.v1_router``/``app.scale.platform_router`` --
# ``app.scale.targets`` (Issue #87 Task 2) is now the real domain module and
# Task 6 rewires those routes onto the lifecycle schemas below.)
# ---------------------------------------------------------------------------


class TargetCreateRequest(_Base):
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str


# ---------------------------------------------------------------------------
# Targets (Issue #87: full preview/publish/withdraw lifecycle)
# ---------------------------------------------------------------------------


class TargetContextResponse(_Base):
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    has_active_target: bool


# A Target amount is a plain ASCII decimal ("20", "2.50"): no exponent,
# separator or surrounding space. A minus sign is read only to say the amount
# is too small.
_AMOUNT = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?")

# (whole digits, decimal places, zero allowed): the columns the amounts are
# stored in (``app.scale.models``: CPA and minimum spend ``Numeric(24, 12)``,
# ROAS ``Numeric(10, 4)``). A minimum spend of zero is the same as none
# (``app.scale.evidence``).
_AMOUNT_LIMITS = {
    "target_cpa": (12, 12, False),
    "breakeven_roas": (6, 4, False),
    "minimum_economic_spend": (12, 12, True),
}


class TargetPublicationPreviewRequest(_Base):
    product_version_id: str = Field(min_length=1)
    offer_version_id: str = Field(min_length=1)
    market_code: str = Field(min_length=1, max_length=10)
    currency: str = Field(min_length=3, max_length=3)
    target_cpa: str | None = None
    breakeven_roas: str | None = None
    minimum_economic_spend: str | None = None

    @field_validator("target_cpa", "breakeven_roas", "minimum_economic_spend")
    @classmethod
    def _amount(cls, value: str | None, info: ValidationInfo) -> str | None:
        """#381: a malformed amount is a 422 on its field, with a code the
        interface has copy for; leading and trailing zeros do not count
        against the column, as with Pydantic's own ``Decimal``. An empty
        string is still an amount not entered."""
        if not value:
            return value
        match = _AMOUNT.fullmatch(value)
        if match is None:
            raise PydanticCustomError("decimal_parsing", "Input should be a valid decimal")
        sign, whole, places = match.groups()
        places = places or ""
        whole_digits, decimal_places, zero_allowed = _AMOUNT_LIMITS[info.field_name]
        if zero_allowed and sign:
            raise PydanticCustomError(
                "greater_than_equal", "Input should be greater than or equal to {ge}", {"ge": 0},
            )
        if not zero_allowed and (sign or not (whole + places).strip("0")):
            raise PydanticCustomError("greater_than", "Input should be greater than {gt}", {"gt": 0})
        if len(whole.lstrip("0")) > whole_digits:
            raise PydanticCustomError(
                "decimal_whole_digits",
                "Decimal input should have no more than {whole_digits} digits before the decimal point",
                {"whole_digits": whole_digits},
            )
        if len(places.rstrip("0")) > decimal_places:
            raise PydanticCustomError(
                "decimal_max_places",
                "Decimal input should have no more than {decimal_places} decimal places",
                {"decimal_places": decimal_places},
            )
        return value


class TargetPublicationPreviewResponse(_Base):
    id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    target_cpa: str | None
    breakeven_roas: str | None
    minimum_economic_spend: str | None
    affected_enrollment_count: int
    policy_floor_check_passed: bool
    impact_hash: str
    expires_at: datetime
    created_at: datetime


class TargetPublishRequest(_Base):
    publication_preview_id: str = Field(min_length=1, max_length=64)


class TargetWithdrawalPreviewRequest(_Base):
    reason: str = Field(min_length=1, max_length=200)


class TargetWithdrawalPreviewResponse(_Base):
    id: str
    target_version_id: str
    reason: str
    affected_enrollment_count: int
    dependent_advice_thread_count: int
    impact_hash: str
    expires_at: datetime
    created_at: datetime


class TargetWithdrawRequest(_Base):
    withdrawal_preview_id: str = Field(min_length=1, max_length=64)


class TargetResponse(_Base):
    id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    target_cpa: str | None
    breakeven_roas: str | None
    minimum_economic_spend: str | None
    lifecycle_state: str
    creator_id: str
    revision: int
    published_at: datetime | None
    withdrawn_at: datetime | None
    created_at: datetime
    allowed_actions: list[ActionEligibilityModel]


class TargetListResponse(_Base):
    items: list[TargetResponse]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Command recovery
# ---------------------------------------------------------------------------


class ActivationPreviewCreateRequest(_Base):
    expected_revision: int


class ActivationPreviewResponse(_Base):
    id: str
    enrollment_id: str
    meta_mode: str
    scenario_scope_key: str
    transition_kind: str
    delivery_evidence_snapshot_id: str
    lineage_guard_id: str
    candidate_fingerprint: str
    guard_revision: int
    lineage_hash: str
    enrollment_revision: int
    expires_at: datetime
    consumed_at: datetime | None
    created_at: datetime


class CommandOutcomeResponse(_Base):
    identity_hash: str
    command_type: str
    dedup_key: str
    result_resource_id: str
    result_resource_type: str
    request_fingerprint: str
    initiator_id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Breakdowns (Issue #87: snapshot-pinned paginated breakdown projections)
# ---------------------------------------------------------------------------


class BreakdownSegmentResponse(_Base):
    breakdown_type: str
    segment_key: str
    state: str
    suppression_reason: str | None = None
    spend_share: str | None = None
    impressions: int | None = None
    link_clicks: int | None = None
    purchases: int | None = None
    purchase_value: str | None = None
    cpa: str | None = None
    roas: str | None = None
    insufficient_reason: str | None = None


class BreakdownResponse(_Base):
    snapshot_id: str
    episode_id: str
    breakdown_type: str
    window_start: date
    window_end: date
    safe_watermark: date
    meta_mode: str
    policy_version_id: str
    target_version_id: str | None
    segments: list[BreakdownSegmentResponse]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Exports (Issue #87: idempotent 202 Operations + download intents)
# ---------------------------------------------------------------------------


class ExportCreateRequest(_Base):
    enrollment_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    export_kind: str = Field(min_length=1)
    snapshot_id: str | None = None
    evaluation_id: str | None = None


class ExportResponse(_Base):
    id: str
    operation_id: str
    enrollment_id: str
    episode_id: str
    export_kind: str
    requested_by: str
    requested_at: datetime
    completed_at: datetime | None
    result_object_reference: str | None


class ExportDownloadIntentRequest(_Base):
    pass


class ExportDownloadIntentResponse(_Base):
    intent_id: str
    expires_at: datetime
