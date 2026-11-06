"""ORM records for the Generation → Assets domain (Issue #21).

New tables: generation_drafts, resolution_preview_confirmations,
generation_recipe_snapshots, run_items, provider_attempt_events,
provider_attempt_projections, run_item_asset_version_bindings,
asset_qc_attempts, asset_qc_check_results, generation_run_commands,
export_packages.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    text,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import new_id, utcnow


class GenerationDraftRecord(Base):
    __tablename__ = "generation_drafts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_generation_drafts_workspace_id"),
        CheckConstraint(
            "state IN ('editing', 'previewing', 'archived')",
            name="ck_generation_draft_state",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_generation_draft_execution_mode",
        ),
        CheckConstraint("revision >= 1", name="ck_generation_draft_revision"),
        CheckConstraint(
            "regeneration_intent IN ('new_generation', 'same_recipe', 'new_seed', "
            "'edit_prompt', 'quality_feedback')",
            name="ck_generation_draft_regen_intent",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_generation_drafts_workspace_product",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    selectors_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    combination_strategy_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    format_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    exclusions_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    regeneration_intent: Mapped[str] = mapped_column(String(40), nullable=False, default="new_generation")
    source_asset_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_recipe_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="editing")
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResolutionPreviewConfirmationRecord(Base):
    __tablename__ = "resolution_preview_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_resolution_preview_confirmations_workspace_id",
        ),
        UniqueConstraint(
            "workspace_id", "preview_id",
            name="uq_resolution_preview_confirmations_workspace_preview",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "preview_id"],
            ["resolution_previews.workspace_id", "resolution_previews.id"],
            name="fk_rpc_workspace_preview",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    preview_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    quoted_max_provider_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    quoted_max_budget_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    quoted_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pricing_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    authorization_context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class GenerationRecipeSnapshotRecord(Base):
    __tablename__ = "generation_recipe_snapshots"
    __table_args__ = (
        Index("ix_generation_recipe_marketing_project", "workspace_id",
            text("(((canonical_json -> 'selection'::text) -> 'marketing_intent'::text) ->> 'project_id'::text)")).ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_generation_recipe_snapshots_workspace_id",
        ),
        UniqueConstraint(
            "workspace_id", "canonicalization_version", "recipe_hash",
            name="uq_recipe_snapshot_workspace_canon_hash",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock', 'controlled_mock')",
            name="ck_recipe_snapshot_exec_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonicalization_version: Mapped[str] = mapped_column(String(40), nullable=False)
    recipe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OnboardingFactOutboxRecord(Base):
    """Typed, append-only facts consumed by the control-plane checklist."""

    __tablename__ = "onboarding_fact_outbox"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "fact_type", "source_resource_id",
            name="uq_onboarding_fact_source",
        ),
        CheckConstraint(
            "fact_type IN ('resolution_preview.validated', "
            "'generation_recipe_snapshot.mock_created', "
            "'asset_version.qualified', 'quality_review.human_recorded')",
            name="ck_onboarding_fact_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_lease_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class RunItemRecord(Base):
    __tablename__ = "run_items"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_run_items_workspace_id",
        ),
        UniqueConstraint(
            "workspace_id", "generation_run_id", "ordinal",
            name="uq_run_items_workspace_run_ordinal",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "generation_run_id"],
            ["generation_runs.workspace_id", "generation_runs.id"],
            name="fk_run_items_workspace_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('pending', 'queued', 'running', 'qc_pending', "
            "'succeeded', 'reused', 'failed', 'cancelled', 'paused', 'uncertain')",
            name="ck_run_item_state",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock', 'controlled_mock')",
            name="ck_run_item_execution_mode",
        ),
        CheckConstraint("ordinal >= 0", name="ck_run_item_ordinal"),
        CheckConstraint("revision >= 1", name="ck_run_item_revision"),
        CheckConstraint(
            "intent_kind IN ('new_generation', 'same_recipe', 'new_seed', "
            "'edit_prompt', 'quality_feedback')",
            name="ck_run_item_intent_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation_run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    combination_identity_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Template-sourced fingerprints are "<entry_id>:<dimension_id>=<value_id>"
    # joined per dimension (app.generation.planner.enumerate_full);
    # three dimensions already exceed 64 characters, so these are unbounded
    # text, not fixed-width hashes (migration ze1f2a3b4c5d).
    combination_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    stable_dimension_signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_seed: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effective_seed: Mapped[str | None] = mapped_column(String(128), nullable=True)
    seed_unavailable_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="new_generation")
    source_asset_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_run_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    legacy_asset_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    same_snapshot_resumable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


Index(
    "ix_run_items_run_state",
    RunItemRecord.workspace_id,
    RunItemRecord.generation_run_id,
    RunItemRecord.state,
)


class ProviderAttemptEventRecord(Base):
    __tablename__ = "provider_attempt_events"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider_call_attempt_id", "sequence",
            name="uq_provider_attempt_events_workspace_attempt_seq",
        ),
        UniqueConstraint(
            "workspace_id", "provider_call_attempt_id", "idempotency_key",
            name="uq_provider_attempt_events_workspace_attempt_idem",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id"],
            ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_provider_attempt_events_workspace_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('dispatch_started', 'response_received', "
            "'failure_classified', 'usage_reported', 'certainty_resolved', "
            "'reconciled', 'legacy_state_imported')",
            name="ck_provider_attempt_event_type",
        ),
        CheckConstraint("sequence >= 1", name="ck_provider_attempt_event_seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_call_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_refs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    executor: Mapped[str] = mapped_column(String(100), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProviderAttemptProjectionRecord(Base):
    __tablename__ = "provider_attempt_projections"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider_call_attempt_id",
            name="uq_provider_attempt_projections_workspace_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id"],
            ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_provider_attempt_projections_workspace_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "dispatch_certainty IN ('not_dispatched', 'dispatched', 'uncertain')",
            name="ck_projection_dispatch_cert",
        ),
        CheckConstraint(
            "outcome IN ('pending', 'succeeded', 'failed', 'uncertain', 'reconciled')",
            name="ck_projection_outcome",
        ),
        CheckConstraint("last_event_sequence >= 0", name="ck_projection_last_seq"),
        CheckConstraint("revision >= 1", name="ck_projection_revision"),
        CheckConstraint(
            "settlement_state IN ('pending', 'settled', 'voided')",
            name="ck_projection_settlement",
        ),
        CheckConstraint(
            "reconciliation_state IN ('none', 'needed', 'reconciled')",
            name="ck_projection_reconcile",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_call_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_certainty: Mapped[str] = mapped_column(String(20), nullable=False, default="not_dispatched")
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    usage_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    settlement_state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reconciliation_state: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    last_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RunItemAssetVersionBindingRecord(Base):
    __tablename__ = "run_item_asset_version_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_run_item_av_bindings_workspace_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_item_id"],
            ["run_items.workspace_id", "run_items.id"],
            name="fk_run_item_av_bindings_workspace_run_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_run_item_av_bindings_workspace_av",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('produced', 'reused')",
            name="ck_run_item_av_binding_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_item_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    producing_provider_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AssetQCAttemptRecord(Base):
    __tablename__ = "asset_qc_attempts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_asset_qc_attempts_workspace_id",
        ),
        UniqueConstraint(
            "workspace_id", "id", "asset_version_id",
            name="uq_aqc_workspace_id_av",
        ),
        UniqueConstraint(
            "workspace_id", "asset_version_id", "attempt_sequence",
            name="uq_aqc_workspace_av_seq",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_asset_qc_attempts_workspace_av",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_item_id"],
            ["run_items.workspace_id", "run_items.id"],
            name="fk_asset_qc_attempts_workspace_run_item",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'passed', 'failed_blocking', "
            "'failed_nonblocking', 'uncertain')",
            name="ck_asset_qc_attempt_state",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_asset_qc_attempt_mode",
        ),
        CheckConstraint("feedback_round >= 0", name="ck_asset_qc_feedback_round"),
        CheckConstraint("revision >= 1", name="ck_asset_qc_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_item_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    feedback_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qc_policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_set_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    feedback_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remediates_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AssetQCCheckResultRecord(Base):
    __tablename__ = "asset_qc_check_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "qc_attempt_id"],
            ["asset_qc_attempts.workspace_id", "asset_qc_attempts.id"],
            name="fk_asset_qc_check_results_workspace_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "severity IN ('must_match', 'should_match', 'informational')",
            name="ck_qc_check_severity",
        ),
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'skip', 'error')",
            name="ck_qc_check_outcome",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qc_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    check_type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class GenerationRunCommandRecord(Base):
    __tablename__ = "generation_run_commands"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_generation_run_commands_workspace_id",
        ),
        UniqueConstraint(
            "workspace_id", "operation_id",
            name="uq_generation_run_commands_workspace_operation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "generation_run_id"],
            ["generation_runs.workspace_id", "generation_runs.id"],
            name="fk_generation_run_commands_workspace_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_generation_run_commands_workspace_operation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "command_kind IN ('submit', 'retry_failed_items', "
            "'resume_paused_items', 'cancel', 'reconcile')",
            name="ck_generation_run_command_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation_run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_selection_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    target_selection_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expected_run_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExportPackageRecord(Base):
    __tablename__ = "export_packages"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_export_packages_workspace_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_export_packages_workspace_operation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_export_package_mode",
        ),
        CheckConstraint(
            "state IN ('pending', 'building', 'ready', 'failed', 'expired')",
            name="ck_export_package_state",
        ),
        CheckConstraint("revision >= 1", name="ck_export_package_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    selector_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    selected_versions_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    committed_object_key: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    committed_storage_version_id: Mapped[str] = mapped_column(
        String(256), nullable=False, default=""
    )
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    zip_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    counts_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    failure_evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
