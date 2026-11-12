"""Domain SQLAlchemy models for usage events and customer notifications.

Spec section 2.12: UsageEventRecord -- append-only, workspace-scoped usage
events with metric_key, measurement_kind, direction, and unit.

Spec section 2.13: CustomerNotificationOutboxRecord and
GuardrailNotificationStateRecord -- the transactional outbox for delivering
customer-facing notifications and the state machine that prevents duplicate
guardrail alerts.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base


class UsageEventRecord(Base):
    """Append-only usage event (spec section 2.12).

    Records each metered usage event for billing and analytics.  Rows are
    never updated or deleted; append-only enforcement lives in the
    migration trigger, not the ORM.

    The CHECKs (migration ``zzk145a1b2c3``) hold the shape every metric
    shares; units and which metric uses which kind stay in the registry
    (``app.entitlements.usage.METRICS``).
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "semantic_key_hash",
            name="uq_usage_events_workspace_semantic_key",
        ),
        CheckConstraint(
            "measurement_kind IN ('delta', 'gauge')",
            name="measurement_kind_valid",
        ),
        CheckConstraint(
            "(measurement_kind <> 'delta' OR direction IN ('increase', 'decrease')) "
            "AND (measurement_kind <> 'gauge' OR direction = 'set')",
            name="direction_matches_kind",
        ),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock', 'not_applicable')",
            name="execution_mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    metric_key: Mapped[str] = mapped_column(String(128), nullable=False)
    measurement_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False,
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    source_resource_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    source_resource_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class CustomerNotificationOutboxRecord(Base):
    """Transactional outbox for customer-facing notifications (spec section 2.13).

    Delivery lifecycle: pending -> delivering -> delivered | failed | dead_letter.
    The ``deduplication_key`` prevents duplicate notifications for the
    same triggering event.
    """

    __tablename__ = "customer_notification_outbox"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_notification_outbox_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id", "deduplication_key",
            name="uq_notification_outbox_workspace_dedup_key",
        ),
        CheckConstraint(
            "delivery_state IN ('pending', 'delivering', 'delivered', "
            "'failed', 'dead_letter')",
            name="delivery_state_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    guardrail_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    window_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    window_key: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    crossing_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False)
    customer_payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    delivery_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    delivery_lease_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class GuardrailNotificationStateRecord(Base):
    """Guardrail notification deduplication state (spec section 2.13).

    Tracks which guardrail thresholds have fired so that the same alert
    is not sent twice within the same window.  Lifecycle: below -> notified.
    """

    __tablename__ = "guardrail_notification_states"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_guardrail_notification_state_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id", "guardrail_revision", "platform_policy_version",
            "dimension", "window_key", "threshold",
            name="uq_guardrail_notification_state_composite",
        ),
        CheckConstraint(
            "state IN ('below', 'notified')",
            name="state_valid",
        ),
        CheckConstraint(
            "crossing_sequence >= 0",
            name="crossing_seq_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    guardrail_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    window_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    window_key: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="below",
    )
    crossing_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
