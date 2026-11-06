"""Typed response contracts for the Experiments v1 workspace surface.

The browser renders server-owned lifecycle, evidence and confirmation state.
These models intentionally keep judgement proposals separate from confirmed
decisions so a statistical result can never be presented as a material
Winner before a human confirmation exists.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExperimentSummary(BaseModel):
    id: str
    name: str
    state: str
    revision: int
    record_schema_version: int | None = None
    created_at: datetime
    frozen_at: datetime | None = None
    closed_at: datetime | None = None


class ExperimentPage(BaseModel):
    items: list[ExperimentSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ExperimentEntry(BaseModel):
    id: str
    position: int
    asset_version_id: str
    marketing_vars: dict[str, Any] = Field(default_factory=dict)


class ExperimentCandidateBinding(BaseModel):
    ad_account_id: str
    ad_id: str
    source: str


class ExperimentCandidate(BaseModel):
    id: str
    position: int
    asset_version_id: str
    candidate_hash: str
    execution_mode: Literal["byok", "sandbox", "mock"]
    marketing_vars: dict[str, Any] = Field(default_factory=dict)
    binding: ExperimentCandidateBinding | None = None


class ExperimentFrozenContext(BaseModel):
    product_version_id: str
    offer_version_id: str
    market: str
    placement: str
    landing_page_evidence_hash: str
    audience: dict[str, Any] = Field(default_factory=dict)
    daily_budget_amount: str
    total_budget_cap: str
    budget_currency: str
    schedule_start_at: datetime
    schedule_end_at: datetime
    schedule_timezone: str
    attribution_click_window_days: int
    attribution_view_window_days: int
    statistical_plan: dict[str, Any] = Field(default_factory=dict)


class ExperimentJudgement(BaseModel):
    id: str
    experiment_id: str
    algorithm_version: str
    as_of: date
    snapshot_hash: str
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ExperimentProposal(BaseModel):
    id: str
    judgement_id: str
    proposal_hash: str
    state: str
    sequence: int
    proposed_entries: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ExperimentDecision(BaseModel):
    id: str
    experiment_id: str
    verdict: str
    metric: str
    estimate: str | None = None
    interval_low: str | None = None
    interval_high: str | None = None
    candidate_id: str | None = None
    proposal_id: str | None = None
    confirmation_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    confirmed_at: datetime | None = None


class ResolutionEvidenceRow(BaseModel):
    duplicate_group_hash: str
    dimension_key: str
    concept_key_id: str
    concept_key_name: str
    action: str
    reason: str
    affected_candidate_fingerprint: str | None = None
    remap_destination_concept_key_id: str | None = None
    remap_destination_concept_key_name: str | None = None
    actor_role: str
    resolved_at: datetime


class ConceptResolutionEvidence(BaseModel):
    concept_resolutions_hash: str
    had_duplicates: bool
    resolutions: list[ResolutionEvidenceRow] = Field(default_factory=list)


class ExperimentDetail(BaseModel):
    id: str
    product_id: str
    name: str
    state: str
    revision: int
    record_schema_version: int
    draft_context: dict[str, Any] = Field(default_factory=dict)
    frozen_snapshot_hash: str | None = None
    created_at: datetime
    frozen_at: datetime | None = None
    started_at: datetime | None = None
    closed_at: datetime | None = None
    version_id: str | None = None
    frozen_context: ExperimentFrozenContext | None = None
    entries: list[ExperimentEntry] = Field(default_factory=list)
    candidates: list[ExperimentCandidate] = Field(default_factory=list)
    judgements: list[ExperimentJudgement] = Field(default_factory=list)
    proposals: list[ExperimentProposal] = Field(default_factory=list)
    decisions: list[ExperimentDecision] = Field(default_factory=list)
    concept_resolution_evidence: ConceptResolutionEvidence | None = None


class ExperimentEntryMutation(BaseModel):
    id: str
    experiment_id: str
    asset_version_id: str
    position: int | None = None
    marketing_vars: dict[str, Any] = Field(default_factory=dict)
    revision: int


class ExperimentContextMutation(BaseModel):
    revision: int
    draft_context: dict[str, Any]


class ExperimentBindingMutation(BaseModel):
    candidate_id: str
    ad_account_id: str
    ad_id: str


class ExperimentJudgementResult(BaseModel):
    judgement: ExperimentJudgement
    proposal_id: str | None = None
    proposal_state: str | None = None


class ExperimentConfirmationResult(BaseModel):
    confirmation_id: str
    decisions: list[ExperimentDecision] = Field(default_factory=list)


class ExperimentTimelineEvent(BaseModel):
    id: str
    timeline_sequence: int
    event_type: str
    source_resource_type: str
    source_resource_id: str
    actor_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime


class ExperimentTimeline(BaseModel):
    head_sequence: int
    items: list[ExperimentTimelineEvent] = Field(default_factory=list)
    next_cursor: int | None = None


class RemapTarget(BaseModel):
    concept_key_id: str
    concept_key_name: str


class DuplicateGroupMember(BaseModel):
    source_entry_id: str
    source_candidate_fingerprint: str = Field(min_length=64, max_length=64)
    asset_version_id: str
    display_label: str


class DuplicateGroup(BaseModel):
    duplicate_group_hash: str = Field(min_length=64, max_length=64)
    dimension_key: str
    concept_key_id: str
    concept_key_name: str
    members: list[DuplicateGroupMember] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    remap_targets: list[RemapTarget] = Field(default_factory=list)


class DuplicateAnalysisResponse(BaseModel):
    experiment_id: str
    analyzed_revision: int
    candidate_set_hash: str = Field(min_length=64, max_length=64)
    has_duplicates: bool
    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)
