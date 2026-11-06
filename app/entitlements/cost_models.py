"""Domain SQLAlchemy models for spend guardrails, reservations, provider calls, and cost ledger.

Spec section 2.7-2.11 and 3.1: the cost control pipeline from guardrail
configuration through spend/capacity reservation, provider call attempts,
and the immutable cost ledger.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401


class SpendGuardrailRecord(Base):
    """Per-workspace spend guardrail configuration (spec section 2.7).

    One row per workspace; ``workspace_id`` is the primary key.
    ``revision`` is bumped on every admin update.
    """

    __tablename__ = "spend_guardrails"
    __table_args__ = (
        CheckConstraint(
            "per_operation_limit >= 0 AND rolling_24h_limit >= 0 "
            "AND period_limit >= 0 "
            "AND outstanding_exposure_limit >= 0",
            name="spend_guardrail_amounts_nonneg",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), primary_key=True,
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    budget_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD",
    )

    per_operation_limit: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    rolling_24h_limit: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    period_limit: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    outstanding_exposure_limit: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )

    platform_policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)


class SpendGuardrailEventRecord(Base):
    """Append-only guardrail change events (spec section 2.8).

    Each row records a complete before/after snapshot of guardrail values.
    Rows are never updated or deleted; append-only enforcement lives in
    the migration trigger, not the ORM.
    """

    __tablename__ = "spend_guardrail_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    revision_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision_after: Mapped[int] = mapped_column(Integer, nullable=False)
    old_values_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_values_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    old_budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    new_budget_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    audit_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfa_evidence_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class SpendGuardrailPreviewRecord(Base):
    """One Owner's previewed guardrail change, awaiting their confirm (#386).

    ``id`` is the random ``preview_token``. A preview is bound to its
    Workspace, its creator and the guardrail revision it was made against,
    expires 15 minutes after creation and is consumed at most once: confirm
    sets ``consumed_at`` and ``result_revision`` (the revision it produced,
    the matching ``spend_guardrail_events.revision_after``) and nothing else
    is ever rewritten.
    """

    __tablename__ = "spend_guardrail_previews"
    __table_args__ = (
        CheckConstraint(
            "per_operation_limit >= 0 AND rolling_24h_limit >= 0 "
            "AND period_limit >= 0 "
            "AND outstanding_exposure_limit >= 0",
            name="amounts_nonneg",
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (result_revision IS NULL)",
            name="consumed_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    creator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    per_operation_limit: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    rolling_24h_limit: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    period_limit: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    outstanding_exposure_limit: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SpendReservationRecord(Base):
    """Per-call spend reservation within an Operation (spec section 2.9).

    Lifecycle: reserved -> partially_settled -> settled -> released.
    The ``semantic_key_hash`` is unique per workspace so that duplicate
    reservations are rejected at the database level.
    """

    __tablename__ = "spend_reservations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_spend_reservation_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id", "semantic_key_hash",
            name="uq_spend_reservation_workspace_semantic_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_spend_reservations_workspace_operation_operation_records",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_spend_reservations_workspace_job_jobs",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('reserved', 'partially_settled', 'settled', "
            "'released', 'uncertain')",
            name="spend_reservation_state_valid",
        ),
        CheckConstraint(
            "maximum_provider_amount >= 0",
            name="spend_reservation_max_provider_nonneg",
        ),
        CheckConstraint(
            "maximum_budget_amount >= 0",
            name="spend_reservation_max_budget_nonneg",
        ),
        CheckConstraint(
            "outstanding_budget_amount >= 0",
            name="spend_reservation_outstanding_nonneg",
        ),
        CheckConstraint(
            "outstanding_budget_amount <= maximum_budget_amount",
            name="spend_reservation_outstanding_le_maximum",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved",
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    maximum_provider_amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )

    budget_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    maximum_budget_amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    outstanding_budget_amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )

    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    entitlement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    guardrail_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    run_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipe_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CapacityReservationRecord(Base):
    """Capacity reservation for concurrent-call gating (spec section 2.10).

    Lifecycle: reserved -> committed -> released | expired.
    """

    __tablename__ = "capacity_reservations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_capacity_reservation_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id", "semantic_key_hash",
            name="uq_capacity_reservation_workspace_semantic_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_capacity_reservations_workspace_operation_operation_records",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_capacity_reservations_workspace_job_jobs",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "acquisition_phase IN ('admission', 'execution')",
            name="capacity_reservation_phase_valid",
        ),
        CheckConstraint(
            "state IN ('reserved', 'committed', 'released', 'expired')",
            name="capacity_reservation_state_valid",
        ),
        CheckConstraint(
            "quantity >= 1",
            name="capacity_reservation_quantity_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entitlement_key: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    acquisition_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved",
    )
    entitlement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class ProviderCallAttemptRecord(Base):
    """Individual provider API call attempt (spec section 3.1).

    Tracks dispatch certainty so cost-accounting can reason about
    whether an uncertain call might have been billed upstream.
    """

    __tablename__ = "provider_call_attempts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_provider_call_attempt_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id", "call_semantic_key_hash", "attempt_no",
            name="uq_provider_call_attempt_workspace_key_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_provider_call_attempts_workspace_operation_operation_records",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_provider_call_attempts_workspace_job_jobs",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "spend_reservation_id"],
            ["spend_reservations.workspace_id", "spend_reservations.id"],
            name="fk_provider_call_attempts_ws_spend_reservation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_provider_call_attempts_workspace_cred_credential_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "attempt_no >= 0",
            name="provider_call_attempt_no_nonneg",
        ),
        CheckConstraint(
            "dispatch_state IN ('prepared', 'dispatching', 'succeeded', "
            "'failed', 'uncertain')",
            name="dispatch_state_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_item_id"],
            ["run_items.workspace_id", "run_items.id"],
            name="fk_provider_call_attempts_workspace_run_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_provider_call_attempts_workspace_truth_pkg_truth_packages",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "step_kind IN ('generation', 'visual_qc', 'source_analysis', "
            "'translation', 'localization_edit', 'localization_ocr', "
            "'localization_visual_qc', 'llm_chat', 'market_data')",
            name="ck_provider_call_attempt_step_kind",
        ),
        CheckConstraint(
            "chargeability IN ('charged', 'free_mock', 'free_sandbox', "
            "'legacy_reserved')",
            name="chargeability",
        ),
        CheckConstraint(
            "(chargeability = 'charged' AND execution_mode = 'byok' "
            "AND spend_reservation_id IS NOT NULL) OR "
            "(chargeability = 'free_mock' AND execution_mode = 'mock' "
            "AND spend_reservation_id IS NULL) OR "
            "(chargeability = 'free_sandbox' AND execution_mode IN "
            "('sandbox', 'controlled_mock') "
            "AND spend_reservation_id IS NULL) OR "
            "(chargeability = 'legacy_reserved' AND execution_mode IN "
            "('mock', 'sandbox') AND spend_reservation_id IS NOT NULL "
            "AND step_kind IN ('generation', 'visual_qc'))",
            name="chargeability_consistency",
        ),
        CheckConstraint(
            "step_kind NOT IN ('source_analysis', 'translation', "
            "'localization_edit', 'localization_ocr', "
            "'localization_visual_qc') OR truth_package_id IS NOT NULL",
            name="truth_required",
        ),
        UniqueConstraint(
            "workspace_id", "id", "operation_id", "truth_package_id",
            "step_kind", "execution_mode",
            name="uq_provider_call_attempt_alt_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    spend_reservation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    call_semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)

    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_fingerprint_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    provider_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    idempotency_key_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )

    credential_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    dispatch_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared",
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    provider_evidence_ref: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    dispatching_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    run_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="generation")
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_asset_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    redacted_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chargeability: Mapped[str] = mapped_column(
        String(24), nullable=False, default="charged",
    )
    truth_package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProviderCostLedgerEntryRecord(Base):
    """Append-only provider cost ledger entry (spec section 2.11).

    Records the actual cost, credit, or adjustment for a provider call.
    Rows are never updated or deleted; append-only enforcement lives in
    the migration trigger, not the ORM.
    """

    __tablename__ = "provider_cost_ledger"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_provider_cost_ledger_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id", "semantic_key_hash",
            name="uq_provider_cost_ledger_workspace_semantic_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_provider_cost_ledger_workspace_operation_operation_records",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_provider_cost_ledger_workspace_job_jobs",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "spend_reservation_id"],
            ["spend_reservations.workspace_id", "spend_reservations.id"],
            name="fk_provider_cost_ledger_ws_spend_reservation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_provider_cost_ledger_workspace_cred_credential_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "entry_type IN ('quote', 'reservation', 'release', "
            "'reported_cost', 'estimated_settlement', 'adjustment')",
            name="provider_cost_ledger_entry_type_valid",
        ),
        CheckConstraint(
            "direction IN ('increase', 'decrease')",
            name="provider_cost_ledger_direction_valid",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="execution_mode_valid",
        ),
        CheckConstraint(
            "provider_amount >= 0",
            name="provider_amount_nonnegative",
        ),
        CheckConstraint(
            "budget_amount >= 0",
            name="budget_amount_nonnegative",
        ),
        CheckConstraint(
            "exchange_rate > 0",
            name="exchange_rate_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    spend_reservation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )

    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    provider_amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    provider_currency: Mapped[str] = mapped_column(String(3), nullable=False)

    budget_amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    budget_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False, default=1,
    )
    exchange_rate_source: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    exchange_rate_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    price_rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    related_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adjusts_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_evidence_ref: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
    )
    reconciliation_evidence_ref: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
    )
    audit_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audit_actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    simulated_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 12), nullable=True,
    )
    simulated_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    run_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_call_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipe_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# `ProviderCallAttemptRecord.__table_args__` above carries a
# ForeignKeyConstraint into `run_items`, declared in `app.generation.models`
# (Issue #21). SQLAlchemy resolves that string table reference lazily on the
# first flush of a row through this module, so any process that persists a
# `ProviderCallAttemptRecord` must have `app.generation.models` imported
# first, or resolution raises `NoReferencedTableError`. See the matching note
# in `app/adapters/persistence/models.py`.
from app.generation import models as _generation_models  # noqa: E402,F401
