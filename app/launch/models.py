# app/launch/models.py
"""Launch-domain SQLAlchemy record classes (spec §3.15-§3.21).

Multi-candidate Launch saga records for issue #23. All tables carry
``(workspace_id, meta_mode)`` as composite isolation keys, matching the
pattern established in ``app.meta.models``. RLS policies use
``marketai_meta_context_is_current()`` from the foundation migration.

These records are named/tabled to avoid colliding with the pre-existing
single-candidate Launch canary records in
``app.adapters.persistence.models`` (``LaunchPreviewRecord`` /
``launch_previews``, ``LaunchOperationRecord`` / ``launch_operations``).
Multi-candidate confirmations use their own mode-scoped record.  The legacy
``LaunchConfirmationRecord`` remains reserved for the single-candidate canary;
sharing it would lose the Meta mode boundary and its foreign key points at the
legacy ``launch_previews`` table.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import utcnow


# ---------------------------------------------------------------------------
# §3.15 MetaLaunchPreviewRecord (spec: LaunchPreviewRecord)
# ---------------------------------------------------------------------------

class MetaLaunchPreviewRecord(Base):
    """Frozen preview of a multi-candidate Launch (spec §3.15).

    Named ``MetaLaunchPreviewRecord`` (table ``launch_preview_records``) to
    avoid colliding with the pre-existing single-candidate
    ``app.adapters.persistence.models.LaunchPreviewRecord`` (table
    ``launch_previews``). Confirmation is handled by the existing
    ``LaunchConfirmationRecord`` protocol (spec §5.2) -- see
    ``LaunchProvisioningRecord.confirmation_id`` below.
    """

    __tablename__ = "launch_preview_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "meta_connection_id"],
            ["meta_connection_records.workspace_id",
             "meta_connection_records.meta_mode",
             "meta_connection_records.id"],
            name="fk_lprev_workspace_mode_conn",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "meta_binding_id"],
            ["meta_data_source_binding_records.workspace_id",
             "meta_data_source_binding_records.meta_mode",
             "meta_data_source_binding_records.id"],
            name="fk_lprev_workspace_mode_binding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "meta_mode", "id",
                         name="uq_lprev_workspace_mode_id"),
        CheckConstraint(
            "(state = 'confirmed') = (confirmed_at IS NOT NULL)",
            name="confirmed_state",
        ),
        CheckConstraint("daily_budget_minor > 0", name="budget_positive"),
        CheckConstraint("candidate_count >= 1", name="candidate_count_min"),
        CheckConstraint(
            "state IN ('draft', 'confirmed', 'expired')",
            name="state_valid",
        ),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    meta_binding_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # experiment_id / experiment_version_id are cross-domain references to
    # app.adapters.persistence.models.ExperimentRecord /
    # ExperimentVersionRecord. Task 6's "Consumes" interface (see
    # task-6-brief.md) lists only the Meta-domain records from Tasks 3-4 as
    # hard dependencies, and the brief's own test constructs previews with
    # bare placeholder ids (e.g. "exp_1") that do not correspond to real
    # Experiment rows -- so no FK is declared here to keep those tests
    # (and any similarly-scoped caller) valid.
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Frozen manifest
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    campaign_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    daily_budget_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Snapshot integrity
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    # State
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        # Real preview creation must set an explicit TTL-based expires_at
        # (spec §5.2); the callable default here only satisfies NOT NULL
        # for callers that omit it, matching task-6-brief.md's tests.
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §3.16 LaunchCandidateRecord
# ---------------------------------------------------------------------------

class LaunchCandidateRecord(Base):
    """One per candidate in a frozen preview. Immutable after creation (spec §3.16)."""

    __tablename__ = "launch_candidate_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "preview_id"],
            ["launch_preview_records.workspace_id",
             "launch_preview_records.meta_mode",
             "launch_preview_records.id"],
            name="fk_lcand_workspace_mode_preview",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "meta_mode", "id",
                         name="uq_lcand_workspace_mode_id"),
        UniqueConstraint("preview_id", "candidate_index",
                         name="uq_lcand_preview_index"),
        CheckConstraint("candidate_index >= 0", name="index_nonneg"),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    preview_id: Mapped[str] = mapped_column(String(64), nullable=False)

    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # experiment_version_candidate_id / asset_version_id are cross-domain
    # references (ExperimentVersionCandidateRecord / AssetVersionRecord);
    # not FK-enforced here for the same reason as experiment_id above on
    # MetaLaunchPreviewRecord.
    experiment_version_candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)

    creative_config: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Deterministic idempotency keys (per-candidate scope) -- spec §3.16:
    # HMAC-SHA256(connection_secret, canonical_identity).
    adset_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    ad_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §3.16a MetaLaunchConfirmationRecord
# ---------------------------------------------------------------------------

class MetaLaunchConfirmationRecord(Base):
    """Fresh human confirmation for one immutable multi-candidate preview.

    A preview may be executed at most once.  Keeping the Workspace and Meta
    mode on this row makes the confirmation independently tenant-safe instead
    of trying to reuse the legacy canary confirmation's transitive scope.
    """

    __tablename__ = "launch_confirmation_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "preview_id"],
            ["launch_preview_records.workspace_id",
             "launch_preview_records.meta_mode",
             "launch_preview_records.id"],
            name="fk_lconf_workspace_mode_preview",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "id",
            name="uq_lconf_workspace_mode_id",
        ),
        UniqueConstraint(
            "workspace_id", "meta_mode", "preview_id",
            name="uq_lconf_workspace_mode_preview",
        ),
        UniqueConstraint("token_hash", name="uq_lconf_token_hash"),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    preview_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §3.17 LaunchProvisioningRecord
# ---------------------------------------------------------------------------

class LaunchProvisioningRecord(Base):
    """Saga state for provisioning Meta objects (spec §3.17).

    Renamed from ``LaunchOperationRecord`` (spec terminology) to avoid
    collision with both the generic ``OperationRecord`` and the pre-existing
    single-candidate ``app.adapters.persistence.models.LaunchOperationRecord``
    (table ``launch_operations``).
    """

    __tablename__ = "launch_provisioning_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "meta_connection_id"],
            ["meta_connection_records.workspace_id",
             "meta_connection_records.meta_mode",
             "meta_connection_records.id"],
            name="fk_lprov_workspace_mode_conn",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "preview_id"],
            ["launch_preview_records.workspace_id",
             "launch_preview_records.meta_mode",
             "launch_preview_records.id"],
            name="fk_lprov_workspace_mode_preview",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "confirmation_id"],
            ["launch_confirmation_records.workspace_id",
             "launch_confirmation_records.meta_mode",
             "launch_confirmation_records.id"],
            name="fk_lprov_workspace_mode_confirmation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lprov_operation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "meta_mode", "id",
                         name="uq_lprov_workspace_mode_id"),
        CheckConstraint(
            "activation_state = 'paused' OR provisioning_state = 'provisioned'",
            name="activation_needs_provisioned",
        ),
        CheckConstraint(
            "provisioning_state IN ('pending', 'provisioning', 'provisioned', "
            "'partially_provisioned', 'failed')",
            name="provisioning_state_valid",
        ),
        CheckConstraint(
            "activation_state IN ('paused', 'activating', 'active', 'failed')",
            name="activation_state_valid",
        ),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_id: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Campaign-level idempotency key (shared campaign) -- spec §3.17:
    # HMAC-SHA256(connection_secret, "campaign:{preview_id}").
    campaign_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Provisioning state (separate from activation)
    provisioning_state: Mapped[str] = mapped_column(String(24), nullable=False)
    # Activation state (only meaningful after provisioning completes). In
    # #23 scope this remains 'paused' for all provisionings -- activation
    # is deferred (spec §9); 'activating'/'active' are reserved for
    # forward compatibility.
    activation_state: Mapped[str] = mapped_column(String(16), nullable=False, default="paused")

    # Remote IDs (populated as saga steps complete)
    remote_campaign_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Fencing: monotonically increasing; stale-token writes rejected by a
    # CAS check at the service layer (spec §3.17, §4.2).
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §3.18 LaunchProvisioningStatusHistoryRecord
# ---------------------------------------------------------------------------

class LaunchProvisioningStatusHistoryRecord(Base):
    """Append-only status changes for provisioning records (spec §3.18)."""

    __tablename__ = "launch_provisioning_status_history_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "provisioning_id"],
            ["launch_provisioning_records.workspace_id",
             "launch_provisioning_records.meta_mode",
             "launch_provisioning_records.id"],
            name="fk_lpsh_workspace_mode_prov",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "field IN ('provisioning_state', 'activation_state')",
            name="field_valid",
        ),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provisioning_id: Mapped[str] = mapped_column(String(64), nullable=False)

    field: Mapped[str] = mapped_column(String(32), nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    executor: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §3.19 LaunchSagaStepAttemptRecord
# ---------------------------------------------------------------------------

class LaunchSagaStepAttemptRecord(Base):
    """Per-Meta-call persistence within the Launch saga (spec §3.19).

    ``candidate_id`` is NULL for campaign-level steps (create_campaign,
    readback_campaign) and NOT NULL for candidate-level steps (create_adset,
    upload_image, create_creative, create_ad, readback_adset, readback_ad).
    The attempt carries ``intent_id`` -- the attempt references the intent,
    not the reverse; intent + attempt are inserted in one atomic
    authorization transaction (spec §4.2).
    """

    __tablename__ = "launch_saga_step_attempt_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "provisioning_id"],
            ["launch_provisioning_records.workspace_id",
             "launch_provisioning_records.meta_mode",
             "launch_provisioning_records.id"],
            name="fk_lssa_workspace_mode_prov",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "intent_id"],
            ["meta_call_intent_records.workspace_id",
             "meta_call_intent_records.id"],
            name="fk_lssa_workspace_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "candidate_id"],
            ["launch_candidate_records.workspace_id",
             "launch_candidate_records.meta_mode",
             "launch_candidate_records.id"],
            name="fk_lssa_workspace_mode_candidate",
            ondelete="RESTRICT",
            # candidate_id is nullable (NULL for campaign-level steps); a
            # composite FK with any NULL member is not enforced by SQL,
            # matching the nullable-credential-FK pattern already used in
            # app.meta.models.MetaDataSourceBindingRecord.
        ),
        UniqueConstraint("workspace_id", "meta_mode", "id",
                         name="uq_lssa_workspace_mode_id"),
        CheckConstraint(
            "state IN ('started', 'intent_committed', 'outcome_received', 'applied')",
            name="state_valid",
        ),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provisioning_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    step_type: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Lease/fencing: must match provisioning record's current fencing_token.
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)

    state: Mapped[str] = mapped_column(String(24), nullable=False)

    # Remote evidence (populated as available) -- Meta's ID for the
    # created/read object.
    remote_object_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    executor: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


# Campaign-level steps: one attempt sequence per step_type.
Index(
    "uq_lssa_campaign_level_attempt",
    LaunchSagaStepAttemptRecord.provisioning_id,
    LaunchSagaStepAttemptRecord.step_type,
    LaunchSagaStepAttemptRecord.attempt_number,
    unique=True,
    sqlite_where=text("candidate_id IS NULL"),
    postgresql_where=text("candidate_id IS NULL"),
)

# Candidate-level steps: one attempt sequence per (step_type, candidate).
Index(
    "uq_lssa_candidate_level_attempt",
    LaunchSagaStepAttemptRecord.provisioning_id,
    LaunchSagaStepAttemptRecord.step_type,
    LaunchSagaStepAttemptRecord.candidate_id,
    LaunchSagaStepAttemptRecord.attempt_number,
    unique=True,
    sqlite_where=text("candidate_id IS NOT NULL"),
    postgresql_where=text("candidate_id IS NOT NULL"),
)


# ---------------------------------------------------------------------------
# §3.20 LaunchParityCheckRecord
# ---------------------------------------------------------------------------

class LaunchParityCheckRecord(Base):
    """Coherent set-level evaluation of the candidate set (spec §3.20)."""

    __tablename__ = "launch_parity_check_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "meta_mode", "provisioning_id"],
            ["launch_provisioning_records.workspace_id",
             "launch_provisioning_records.meta_mode",
             "launch_provisioning_records.id"],
            name="fk_lpc_workspace_mode_prov",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "check_type IN ('post_provisioning', 'post_activation', 'periodic')",
            name="check_type_valid",
        ),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provisioning_id: Mapped[str] = mapped_column(String(64), nullable=False)

    check_type: Mapped[str] = mapped_column(String(24), nullable=False)

    # Parity evaluation
    all_candidates_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    budget_parity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config_parity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_parity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)

    detail: Mapped[dict] = mapped_column(JSON, nullable=False)

    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    executor: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# §3.21 LaunchReconciliationRecord
# ---------------------------------------------------------------------------

class LaunchReconciliationRecord(Base):
    """Serializes reconciliation of uncertain Launch operations (spec §3.21).

    At most one open reconciliation per original operation.
    """

    __tablename__ = "launch_reconciliation_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "original_operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lrec_workspace_original_op",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "recovery_operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lrec_workspace_recovery_op",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("recovery_operation_id",
                         name="uq_lrec_recovery_operation"),
        CheckConstraint(
            "(state = 'resolved') = (resolution IS NOT NULL)",
            name="resolution_state",
        ),
        CheckConstraint(
            "(state = 'resolved') = (resolved_at IS NOT NULL)",
            name="resolved_at_state",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('succeeded', 'partially_succeeded', 'failed', 'cancelled')",
            name="resolution_valid",
        ),
        CheckConstraint(
            "state IN ('open', 'resolved')",
            name="state_valid",
        ),
        CheckConstraint(
            "meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')",
            name="mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    meta_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    original_operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recovery_operation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    resolution: Mapped[str | None] = mapped_column(String(24), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


# At most one open reconciliation per original operation.
Index(
    "uq_lrec_open_original_operation",
    LaunchReconciliationRecord.original_operation_id,
    unique=True,
    sqlite_where=text("state = 'open'"),
    postgresql_where=text("state = 'open'"),
)
