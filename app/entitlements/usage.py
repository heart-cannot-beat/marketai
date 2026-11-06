"""UsageRecorder and UsageQuery: usage event recording and aggregation.

UsageRecorder creates append-only usage events for billing and analytics.
UsageQuery aggregates metered usage events within a time window.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.entitlements.usage_models import UsageEventRecord
from app.platform.tenancy import TenantContext


class UsageRecorder:
    """Record metered usage events."""

    def record(
        self,
        session: Session,
        context: TenantContext,
        *,
        metric_key: str,
        quantity: Decimal,
        unit: str,
        execution_mode: str,
        source_resource_type: str | None = None,
        source_resource_id: str | None = None,
        semantic_key_hash: str,
        occurred_at: datetime | None = None,
    ) -> str:
        """Create an append-only usage event record.

        Returns the event id.
        """
        now = datetime.now(timezone.utc)
        event_id = f"ue_{uuid.uuid4().hex}"

        record = UsageEventRecord(
            id=event_id,
            workspace_id=context.workspace_id,
            metric_key=metric_key,
            measurement_kind="counter",
            direction="increase",
            quantity=quantity,
            unit=unit,
            execution_mode=execution_mode,
            source_resource_type=source_resource_type,
            source_resource_id=source_resource_id,
            semantic_key_hash=semantic_key_hash,
            occurred_at=occurred_at if occurred_at is not None else now,
            created_at=now,
        )
        session.add(record)
        session.flush()
        return event_id


class UsageQuery:
    """Aggregation of metered usage events within a time window."""

    def aggregate(
        self,
        session: Session,
        context: TenantContext,
        *,
        metric_key: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Decimal:
        """Sum the quantity of usage events matching the metric key in the window."""
        result = session.query(
            func.coalesce(func.sum(UsageEventRecord.quantity), Decimal("0")),
        ).filter(
            and_(
                UsageEventRecord.workspace_id == context.workspace_id,
                UsageEventRecord.metric_key == metric_key,
                UsageEventRecord.occurred_at >= window_start,
                UsageEventRecord.occurred_at < window_end,
            )
        ).scalar()

        return Decimal(str(result)) if result is not None else Decimal("0")
