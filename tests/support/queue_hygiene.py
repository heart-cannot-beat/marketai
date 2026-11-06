"""Keep Worker-driving tests from stepping on the shared durable queue.

Every test in the process shares one ``jobs`` table, and ``jobs.claim_next``
hands out the oldest queued Job regardless of Workspace. A test that drives
``WorkerRuntime.run_once`` therefore has to (a) keep earlier tests' leftover
Jobs out of its own Worker's way, and (b) not leave its own Jobs behind for a
later test's Worker to claim -- ``tests/test_foundation.py`` asserts on *its*
Job being the one processed and fails on either kind of pollution.

``quarantine_foreign_jobs`` is the pattern ``test_foundation``'s killed-Worker
test already uses (push every other queued Job's ``next_attempt_at`` a day
out); ``cancel_workspace_jobs`` is the one ``test_export_worker`` uses.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from app.adapters.persistence.database import platform_session
from app.adapters.persistence.models import JobRecord, utcnow

_OPEN_STATES = ("queued", "retry_wait")


def quarantine_foreign_jobs(workspace_id: str) -> None:
    """Defer every open Job outside ``workspace_id`` so a drain loop skips it."""
    with platform_session() as session:
        session.execute(
            update(JobRecord)
            .where(
                JobRecord.workspace_id != workspace_id,
                JobRecord.state.in_(_OPEN_STATES),
            )
            .values(next_attempt_at=utcnow() + timedelta(days=1))
        )


def cancel_workspace_jobs(workspace_id: str) -> None:
    """Cancel every open Job in ``workspace_id`` (cleanup for a submitting test)."""
    with platform_session() as session:
        session.execute(
            update(JobRecord)
            .where(
                JobRecord.workspace_id == workspace_id,
                JobRecord.state.in_(_OPEN_STATES),
            )
            .values(state="cancelled", finished_at=utcnow())
        )
