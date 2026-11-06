"""Domain SQLAlchemy models for the entitlement inbox and projection.

Spec section 2.1: EntitlementSnapshotInboxRecord -- append-only inbox of
control-plane entitlement snapshots received by the domain.

Spec section 2.2: EntitlementProjectionRecord -- single-row-per-workspace
materialized projection of the current entitlement state.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base


class EntitlementSnapshotInboxRecord(Base):
    """Append-only inbox of control-plane entitlement snapshots (spec section 2.1).

    Each row captures the exact bytes received from the control plane, the
    cryptographic verification metadata, and the outcome of applying the
    snapshot to the local projection.  Rows are never updated or deleted;
    append-only enforcement lives in the migration trigger, not the ORM.
    """

    __tablename__ = "entitlement_snapshot_inbox"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "entitlement_version", "lease_sequence",
            name="uq_entitlement_inbox_workspace_version_lease",
        ),
        UniqueConstraint(
            "workspace_id", "snapshot_id",
            name="uq_entitlement_inbox_workspace_snapshot",
        ),
        CheckConstraint(
            "outcome IN ('applied', 'duplicate', 'stale', "
            "'version_jump_paused', 'conflict')",
            name="entitlement_inbox_outcome_valid",
        ),
        CheckConstraint(
            "delivery_kind IN ('push', 'full_repair')",
            name="delivery_kind_valid",
        ),
    )

    # Control-plane immutable message ID.
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )

    entitlement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)

    delivery_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # Exact received bytes -- immutable evidence.
    canonical_envelope: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    envelope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Signature verification metadata.
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="hmac-sha256",
    )

    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    projection_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class EntitlementProjectionRecord(Base):
    """Single-row-per-workspace materialized entitlement projection (spec section 2.2).

    Holds the resolved entitlement state computed from the most recently
    applied snapshot.  The composite FK back to the inbox row ensures that
    the projection always points at a real, workspace-scoped snapshot.
    """

    __tablename__ = "entitlement_projections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "current_snapshot_id"],
            [
                "entitlement_snapshot_inbox.workspace_id",
                "entitlement_snapshot_inbox.snapshot_id",
            ],
            name="fk_entitlement_projections_workspace_snapshot_inbox",
        ),
        CheckConstraint(
            "entitlement_version >= 0 AND lease_sequence >= 0 "
            "AND projection_version >= 0 AND schema_version >= 0 "
            "AND entitlement_registry_version >= 0 "
            "AND resolution_policy_version >= 0 "
            "AND platform_limits_version >= 0",
            name="versions_nonnegative",
        ),
        CheckConstraint(
            "valid_until IS NULL OR issued_at IS NULL OR valid_until > issued_at",
            name="entitlement_projection_valid_window",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), primary_key=True,
    )

    current_snapshot_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )

    entitlement_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    lease_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    projection_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2,
    )

    plan_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    source_refs_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_anchor: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    resolved_entitlements_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
    )
    restriction_summary_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
    )

    semantic_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
    )

    entitlement_registry_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    resolution_policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    platform_limits_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )

    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    control_plane_audit_event_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )

    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signature_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature_algorithm: Mapped[str | None] = mapped_column(String(32), nullable=True)

    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
