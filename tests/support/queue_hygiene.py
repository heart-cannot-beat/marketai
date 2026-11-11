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

``quarantine_open_jobs`` does (a) for a whole file: ``tests/conftest.py`` runs
it before every test file, so which files ran earlier in the process -- the
alphabetical order of a serial run, or whatever ``pytest -n`` hands one
worker -- does not decide what a file's Worker claims (#10).

Quarantine cannot reach one source of work: a Worker's first ``run_once``
turns every due Meta observation schedule, in any Workspace, into a tick Job
(``dispatch_due_observation_ticks``, #107) after the test has quarantined.
:class:`ObservationScheduleGuard` (installed by ``tests/conftest.py``) fails
the test that leaves such a schedule behind, due now or later, and names it
(#124).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.adapters.persistence import database
from app.adapters.persistence.database import platform_session
from app.adapters.persistence.models import JobRecord, utcnow

_OPEN_STATES = ("queued", "retry_wait")
_LEASED_STATES = ("leased", "running")


def _database_has(table_name: str) -> bool:
    url = database.engine.url
    if url.get_backend_name() == "sqlite" and not (
        url.database and Path(url.database).exists()
    ):
        return False
    return inspect(database.engine).has_table(table_name)


def quarantine_open_jobs() -> None:
    """Defer every open Job, in every Workspace, out of a Worker's reach.

    Queued and retrying Jobs become claimable a day from now. Leased and
    running ones keep their lease a day longer, so ``claim_next`` does not
    recover them into the queue once their one-second test lease lapses.
    No state changes and no row is removed.
    """
    if not _database_has(JobRecord.__tablename__):
        return
    later = utcnow() + timedelta(days=1)
    with platform_session() as session:
        session.execute(
            update(JobRecord)
            .where(JobRecord.state.in_(_OPEN_STATES))
            .values(next_attempt_at=later)
        )
        session.execute(
            update(JobRecord)
            .where(JobRecord.state.in_(_LEASED_STATES))
            .values(lease_expires_at=later)
        )


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


# -- Meta observation schedules (#124) ----------------------------------------

#: Every open schedule's next slot is at or before this, so asking the
#: dispatcher what is due "then" lists everything it will ever dispatch.
_END_OF_TIME = datetime(9999, 1, 1, tzinfo=timezone.utc)
_MAX_SLOTS = 10_000


def dispatchable_observation_slots(now: datetime | None = None) -> list[tuple[object, bool]]:
    """Every (connection, fire time) a Worker's observation dispatch turns into
    a tick Job, with whether it is due at ``now`` or only once its slot passes.

    The dispatcher's own selection (``observation_sync._due_slots``, SQLite's
    view of the PostgreSQL dispatch function) decides: an active schedule of an
    active connection and contract generation, in an active Workspace. Empty
    while the database or its schedule table does not exist yet.
    """
    from app.meta import observation_sync
    from app.meta.models import MetaObservationSyncScheduleRecord

    if not _database_has(MetaObservationSyncScheduleRecord.__tablename__):
        return []
    now = now or utcnow()
    with platform_session() as session:
        slots = observation_sync._due_slots(session, limit=_MAX_SLOTS, now=_END_OF_TIME)
    return [(slot, slot.fire_at <= now) for slot in slots]


class ObservationScheduleGuard:
    """Report each connection whose schedule a test leaves for a later Worker.

    ``tests/conftest.py`` asks after every test's teardown; whatever is new
    since the last ask was left by that test. Each connection is reported
    once, so the failure lands on the test that left it, not on every test
    after it.
    """

    def __init__(self, database_url: str, find: Callable[[], list] = dispatchable_observation_slots) -> None:
        self._database = make_url(database_url).database
        self._find = find
        self._reported: set[tuple[str, str, str]] = set()

    def unreported(self) -> list[tuple[object, bool]]:
        if database.engine.url.database != self._database:
            return []  # a test swapped the engine; the next ask checks the shared database
        try:
            found = self._find()
        except OperationalError:
            return []  # a migration test left an older schema for a moment
        new = [(slot, due) for slot, due in found
               if (slot.workspace_id, slot.meta_mode, slot.connection_id) not in self._reported]
        self._reported.update((slot.workspace_id, slot.meta_mode, slot.connection_id) for slot, _ in new)
        return new


def observation_schedule_failure(left: list[tuple[object, bool]], test_id: str) -> str:
    lines = [
        f"{test_id} left Meta observation schedules active (#124). The first run_once of any later "
        "test's WorkerRuntime in this process dispatches them (dispatch_due_observation_ticks), after "
        "that test quarantined the queue, and its Worker then runs another Workspace's ticks:",
    ]
    for slot, due in left:
        when = "due now" if due else f"due at {slot.fire_at.isoformat()}"
        lines.append(f"  Workspace {slot.workspace_id}, {slot.meta_mode} connection {slot.connection_id}: {when}")
    lines.append("Pause or retire them in the test's cleanup, as _quiesce in tests/test_meta_observation_sync.py does.")
    return "\n".join(lines)
