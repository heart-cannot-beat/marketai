# app/meta/sync_pipeline.py
"""Sync run lifecycle: creation, page checkpoints, completion, schedule dispatch (spec §6.2-§6.5, §6.13).

Four operations:

* :func:`create_sync_run` -- idempotently creates one
  ``MetaObservationSyncRunRecord`` for an ``(occurrence_id, run_generation)``
  pair. Per §6.13, a naive ``INSERT ... ON CONFLICT (occurrence_id,
  run_generation) DO UPDATE SET id = EXCLUDED.id`` would silently rewrite an
  existing row's primary key if two callers raced to create the same
  occurrence. Instead this checks for an existing row first, and if a
  concurrent insert wins the race anyway, the ``IntegrityError`` from
  ``uq_msync_run_occurrence_generation`` is caught inside a SAVEPOINT
  (``session.begin_nested()``) -- only the losing insert is rolled back, the
  rest of the caller's transaction is untouched -- and the winner's row is
  read back and returned instead.
* :func:`record_page` -- writes one ``MetaEvidencePageRecord`` and advances
  the owning sync run's cursor checkpoint (``last_completed_page_order``,
  ``output_cursor``) in the same flush (§6.4), so a crash between the two
  writes cannot happen -- either both are visible after commit or neither
  is.
* :func:`complete_sync_run` -- transitions a sync run to ``succeeded``.
  Per §6.5, callers achieve terminal-page atomicity by calling
  ``record_page(..., is_terminal=True)`` and ``complete_sync_run`` in the
  same session without an intervening commit; SQLAlchemy flushes both
  statements as part of one database transaction.
* :func:`dispatch_schedules` -- claims a batch of due, active schedules with
  ``FOR UPDATE SKIP LOCKED`` on PostgreSQL (§6.2), letting multiple
  dispatcher instances run concurrently against disjoint claims. SQLite (no
  ``SKIP LOCKED`` support) falls back to a plain ordered select -- tests and
  local dev never run concurrent dispatchers against it.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.persistence.models import new_id, utcnow
from .models import (
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
)

__all__ = [
    "SyncPipelineError",
    "create_sync_run",
    "record_page",
    "complete_sync_run",
    "dispatch_schedules",
]


class SyncPipelineError(ValueError):
    """Raised for sync pipeline invariant violations."""


def _existing_run(
    session: Session, *, occurrence_id: str, run_generation: int
) -> MetaObservationSyncRunRecord | None:
    return session.scalar(
        select(MetaObservationSyncRunRecord).where(
            MetaObservationSyncRunRecord.occurrence_id == occurrence_id,
            MetaObservationSyncRunRecord.run_generation == run_generation,
        )
    )


def create_sync_run(
    session: Session,
    *,
    workspace_id: str,
    meta_mode: str,
    contract_generation_id: str,
    occurrence_id: str,
    operation_id: str,
    executor: str,
    run_generation: int = 1,
) -> MetaObservationSyncRunRecord:
    """Create a new ``queued`` sync run, or return the one this occurrence
    already produced (spec §6.13).

    Idempotent on ``(occurrence_id, run_generation)`` -- a scheduler that
    dispatches the same occurrence twice (e.g. after a crash and restart)
    gets the original run back rather than a duplicate.
    """
    existing = _existing_run(
        session, occurrence_id=occurrence_id, run_generation=run_generation
    )
    if existing is not None:
        return existing

    run = MetaObservationSyncRunRecord(
        id=new_id("msync"),
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        contract_generation_id=contract_generation_id,
        occurrence_id=occurrence_id,
        run_generation=run_generation,
        state="queued",
        operation_id=operation_id,
        executor=executor,
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        existing = _existing_run(
            session, occurrence_id=occurrence_id, run_generation=run_generation
        )
        if existing is None:
            raise
        return existing
    return run


def record_page(
    session: Session,
    *,
    sync_run_id: str,
    page_order: int,
    payload_checksum: str,
    payload_compressed: bytes,
    row_count: int,
    intent_id: str,
    outcome_id: str,
    input_cursor: str | None = None,
    output_cursor: str | None = None,
    is_terminal: bool = False,
) -> MetaEvidencePageRecord:
    """Write one page of paginated evidence and checkpoint the sync run's cursor.

    Per §6.4, the page write and the checkpoint update
    (``last_completed_page_order``, ``output_cursor``) happen in the same
    flush -- an incomplete page (crash mid-write) never advances the
    checkpoint, so a restarted worker resumes from the last fully committed
    page, not a partial one.
    """
    run = session.get(MetaObservationSyncRunRecord, sync_run_id)
    if run is None:
        raise SyncPipelineError(f"no sync run {sync_run_id!r}")

    page = MetaEvidencePageRecord(
        id=new_id("mpage"),
        workspace_id=run.workspace_id,
        meta_mode=run.meta_mode,
        sync_run_id=run.id,
        contract_generation_id=run.contract_generation_id,
        page_order=page_order,
        input_cursor=input_cursor,
        output_cursor=output_cursor,
        payload_checksum=payload_checksum,
        payload_compressed=payload_compressed,
        row_count=row_count,
        is_terminal=is_terminal,
        intent_id=intent_id,
        outcome_id=outcome_id,
    )
    session.add(page)

    run.last_completed_page_order = page_order
    run.output_cursor = output_cursor
    if run.state == "queued":
        run.state = "running"

    session.flush()
    return page


def complete_sync_run(
    session: Session,
    *,
    sync_run_id: str,
    total_pages: int,
    total_rows: int,
) -> MetaObservationSyncRunRecord:
    """Transition a sync run to ``succeeded`` (spec §6.5).

    Idempotent: completing an already-succeeded run is a no-op rather than
    an error, since a crash-recovered worker may replay the final step.
    Callers achieve terminal-page atomicity by calling this in the same
    session as the terminal ``record_page`` call, without a commit between
    them -- both writes land in one database transaction.
    """
    run = session.get(MetaObservationSyncRunRecord, sync_run_id)
    if run is None:
        raise SyncPipelineError(f"no sync run {sync_run_id!r}")

    if run.state == "succeeded":
        return run

    run.state = "succeeded"
    run.completed_at = utcnow()
    run.total_pages = total_pages
    run.total_rows = total_rows
    session.flush()
    return run


def dispatch_schedules(
    session: Session, batch_size: int
) -> list[MetaObservationSyncScheduleRecord]:
    """Claim up to ``batch_size`` due, active schedules (spec §6.2).

    On PostgreSQL, ``FOR UPDATE SKIP LOCKED`` lets multiple dispatcher
    instances run concurrently: each claims a disjoint set of ready
    schedules instead of blocking on rows another dispatcher already holds.
    SQLite has no ``SKIP LOCKED``, and SQLAlchemy leaves the lock out of its
    SQL there; the plain select is safe because SQLite's single-writer model
    means only one dispatcher runs at a time in tests and local development.
    The statement keeps the lock, so the SQLite grant guard (#59) checks it.
    """
    now = utcnow()
    stmt = (
        select(MetaObservationSyncScheduleRecord)
        .where(
            MetaObservationSyncScheduleRecord.state == "active",
            MetaObservationSyncScheduleRecord.next_fire_at.is_not(None),
            MetaObservationSyncScheduleRecord.next_fire_at <= now,
        )
        .order_by(MetaObservationSyncScheduleRecord.next_fire_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )

    return list(session.scalars(stmt).all())
