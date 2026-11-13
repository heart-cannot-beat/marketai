"""Suite-wide isolated environment bootstrap.

Pytest imports this file before collecting test modules.  Database-backed
modules therefore cannot bind ``app.config`` to the repository's development
database merely because their filename sorts before ``test_foundation.py``.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest


TEST_ROOT = Path(tempfile.mkdtemp(prefix="marketai-pytest-"))
atexit.register(shutil.rmtree, TEST_ROOT, ignore_errors=True)
SHARED_DATABASE_URL = f"sqlite:///{TEST_ROOT / 'marketai.db'}"

os.environ["MARKETAI_TEST_ROOT"] = str(TEST_ROOT)
os.environ["MARKETAI_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["MARKETAI_DATABASE_URL"] = SHARED_DATABASE_URL
os.environ["MARKETAI_WORKSPACE_ID"] = "ws_test"
os.environ["MAX_COST_PER_RUN"] = "5"
os.environ["MAX_COST_PER_DAY"] = "20"
os.environ["JOB_LEASE_SECONDS"] = "1"
# Before any app module is imported: Worker entries mark their role only
# when this is set (app/platform/runtime_role.py), for the grant guard below.
os.environ["MARKETAI_RECORD_RUNTIME_ROLE"] = "1"


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "postgres: PostgreSQL-specific test (RLS enforcement)")
    config.addinivalue_line(
        "markers", "e2e: end-to-end browser test (Playwright + a running server; CI-only)"
    )


@pytest.fixture(autouse=True, scope="module")
def _start_each_file_with_no_claimable_leftover_jobs():
    """Every file in a process shares one ``jobs`` table, and many files leave
    Jobs open. Defer them before the file's first test (and its setUpClass),
    so no file's Worker claims another file's Job, whichever files ran before
    it -- CI spreads files over ``pytest -n`` workers (#10)."""
    from tests.support.queue_hygiene import quarantine_open_jobs

    quarantine_open_jobs()
    yield


_running = {"item": None}
_grant_guard = {"guard": None}
_observation_guard = {"guard": None}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Remember the running test, its setUpClass and fixtures included, so the
    market-code guard and the grant guard can name it."""
    _running["item"] = item


def _running_test_id() -> str:
    item = _running["item"]
    return item.nodeid if item is not None else "(before the first test)"


@pytest.fixture(autouse=True, scope="session")
def _api_and_worker_writes_have_postgres_grants():
    """SQLite has no database roles, so a write the PostgreSQL API or Worker
    role may not make passes here and fails in production (#46). Every HTTP
    request runs as the API role and every Worker entry as the Worker role;
    each INSERT, UPDATE, DELETE, SELECT and row lock they send is checked
    against deploy/postgres/domain-tenant-roles.sql, and a missing grant
    fails the test that first sent it, at its teardown (#59).
    ``MARKETAI_GRANT_GUARD=off`` turns the check off;
    ``MARKETAI_GRANT_GUARD_REPORT=<path>`` writes what it saw as JSON."""
    from tests.support.grant_guard import guard_from_environment, mark_api_requests

    guard = guard_from_environment(_running_test_id)
    if guard is None:
        yield
        return
    unmark = mark_api_requests()
    uninstall = guard.install()
    _grant_guard["guard"] = guard
    yield
    uninstall()
    unmark()
    report = os.environ.get("MARKETAI_GRANT_GUARD_REPORT")
    if report:
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        guard.write_report(f"{report}.{worker}" if worker else report)


def _observation_schedule_guard():
    """Every test's Worker dispatches due Meta observation schedules from any
    Workspace on its first ``run_once``, after the test quarantined the queue,
    so a schedule one test leaves active, due now or later, runs in another
    test's Worker. No test leaves one today; checked after every test, this
    fails the teardown of the first that does (#124)."""
    if _observation_guard["guard"] is None:
        from tests.support.queue_hygiene import ObservationScheduleGuard

        _observation_guard["guard"] = ObservationScheduleGuard(SHARED_DATABASE_URL)
    return _observation_guard["guard"]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Fail the teardown of the test that first sent a statement PostgreSQL
    will refuse, or that left a Meta observation schedule for a later test's
    Worker, whatever the test itself did."""
    outcome = yield
    if call.when != "teardown":
        return
    failures = []
    guard = _grant_guard["guard"]
    violations = guard.unreported() if guard is not None else []
    if violations:
        from tests.support.grant_guard import failure_message

        failures.append(("grant guard", failure_message(violations)))
    left = _observation_schedule_guard().unreported()
    if left:
        from tests.support.queue_hygiene import observation_schedule_failure

        failures.append(("observation schedule guard", observation_schedule_failure(left, item.nodeid)))
    if not failures:
        return
    report = outcome.get_result()
    for section, message in failures:
        if report.passed:
            report.outcome = "failed"
            report.longrepr = message
        else:
            report.sections.append((section, message))


def pytest_terminal_summary(terminalreporter):
    guard = _grant_guard["guard"]
    if guard is not None:
        terminalreporter.write_line(guard.summary_line())


@pytest.fixture(autouse=True, scope="session")
def _market_codes_never_collide_in_the_shared_database():
    """``market_policies.market_id`` is unique, and which files share this
    process's database changes from CI run to run. Every two-letter code must
    come from ``tests.support.market_codes``, and no code may be written
    twice; breaking either fails with an AssertionError that names the rule,
    not an occasional IntegrityError (#81)."""
    from tests.support.market_codes import guard_shared_database

    remove = guard_shared_database(SHARED_DATABASE_URL, lambda: _running["item"])
    yield
    remove()


def pytest_collection_modifyitems(config, items):
    """Skip postgres- and e2e-marked tests in development (SQLite-only,
    browser-less local/CI-lite environment)."""
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="PostgreSQL RLS tests skipped in SQLite environment"))
        if "e2e" in item.keywords:
            item.add_marker(
                pytest.mark.skip(reason="Playwright browser E2E tests require a running server; CI-only")
            )
