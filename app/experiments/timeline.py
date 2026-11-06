"""Experiment timeline: monotonic sequence head and append-only events."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    ExperimentTimelineEventRecord,
    ExperimentTimelineHeadRecord,
    new_id,
    utcnow,
)


class TimelineSequenceConflict(RuntimeError):
    pass


def ensure_head(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
) -> ExperimentTimelineHeadRecord:
    head = session.scalar(
        select(ExperimentTimelineHeadRecord).where(
            ExperimentTimelineHeadRecord.workspace_id == workspace_id,
            ExperimentTimelineHeadRecord.experiment_id == experiment_id,
        )
    )
    if head is not None:
        return head
    head = ExperimentTimelineHeadRecord(
        id=new_id("tlhead"),
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        head_sequence=0,
    )
    session.add(head)
    session.flush()
    return head


def append_event(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    event_type: str,
    source_resource_type: str,
    source_resource_id: str,
    actor_id: str | None = None,
    summary: dict | None = None,
) -> ExperimentTimelineEventRecord:
    head = ensure_head(
        session,
        workspace_id=workspace_id,
        experiment_id=experiment_id,
    )
    now = utcnow()
    next_seq = head.head_sequence + 1

    existing = session.scalar(
        select(ExperimentTimelineEventRecord).where(
            ExperimentTimelineEventRecord.workspace_id == workspace_id,
            ExperimentTimelineEventRecord.source_resource_type == source_resource_type,
            ExperimentTimelineEventRecord.source_resource_id == source_resource_id,
            ExperimentTimelineEventRecord.event_type == event_type,
        )
    )
    if existing is not None:
        return existing

    result = session.execute(
        update(ExperimentTimelineHeadRecord)
        .where(
            ExperimentTimelineHeadRecord.id == head.id,
            ExperimentTimelineHeadRecord.head_sequence == head.head_sequence,
        )
        .values(head_sequence=next_seq)
    )
    if result.rowcount != 1:
        raise TimelineSequenceConflict("Timeline head CAS failed")

    event = ExperimentTimelineEventRecord(
        id=new_id("tlev"),
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        timeline_sequence=next_seq,
        event_type=event_type,
        source_resource_type=source_resource_type,
        source_resource_id=source_resource_id,
        actor_id=actor_id,
        summary_json=summary or {},
        observed_at=now,
    )
    session.add(event)
    session.flush()
    return event
