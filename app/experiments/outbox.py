"""Notification outbox: append-only intent records with source dedup."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    ExperimentNotificationOutboxRecord,
    new_id,
)
from ..platform.canonical import _canonical_json, _sha256


def append_notification(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    timeline_event_id: str,
    notification_type: str,
    payload: dict,
) -> ExperimentNotificationOutboxRecord:
    existing = session.scalar(
        select(ExperimentNotificationOutboxRecord).where(
            ExperimentNotificationOutboxRecord.workspace_id == workspace_id,
            ExperimentNotificationOutboxRecord.timeline_event_id == timeline_event_id,
            ExperimentNotificationOutboxRecord.notification_type == notification_type,
        )
    )
    if existing is not None:
        return existing

    canonical = _canonical_json(payload)
    payload_hash = _sha256(canonical.encode("utf-8"))

    record = ExperimentNotificationOutboxRecord(
        id=new_id("notif"),
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        timeline_event_id=timeline_event_id,
        notification_type=notification_type,
        payload_canonical_json=canonical,
        payload_hash=payload_hash,
        payload_schema_version=1,
    )
    session.add(record)
    session.flush()
    return record
