"""CustomerNotificationWriter: transactional outbox for customer notifications.

Enqueues customer-facing notifications into the outbox table with
deduplication and delivery lifecycle management.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.entitlements.usage_models import CustomerNotificationOutboxRecord
from app.platform.tenancy import TenantContext


class CustomerNotificationWriter:
    """Enqueue customer-facing notifications into the transactional outbox."""

    def enqueue(
        self,
        session: Session,
        context: TenantContext,
        *,
        notification_type: str,
        dimension: str,
        threshold: Decimal,
        guardrail_revision: int,
        platform_policy_version: int,
        window_kind: str,
        window_key: str,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        crossing_sequence: int,
        deduplication_key: str,
        customer_payload: dict,
    ) -> str:
        """Create a pending notification outbox record.

        Returns the notification id. The ``deduplication_key`` is subject
        to a unique constraint per workspace to prevent duplicate
        notifications for the same triggering event.
        """
        now = datetime.now(timezone.utc)
        notification_id = f"cn_{uuid.uuid4().hex}"

        record = CustomerNotificationOutboxRecord(
            id=notification_id,
            workspace_id=context.workspace_id,
            notification_type=notification_type,
            dimension=dimension,
            threshold=threshold,
            guardrail_revision=guardrail_revision,
            platform_policy_version=platform_policy_version,
            window_kind=window_kind,
            window_key=window_key,
            period_start=period_start,
            period_end=period_end,
            crossing_sequence=crossing_sequence,
            deduplication_key=deduplication_key,
            customer_payload_json=customer_payload,
            delivery_state="pending",
            attempts=0,
            created_at=now,
        )
        session.add(record)
        session.flush()
        return notification_id
