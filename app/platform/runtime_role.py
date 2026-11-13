"""Which PostgreSQL role the code on the stack runs as (#59).

FastAPI connects as ``marketai_domain_api`` and both Worker runtimes as
``marketai_domain_worker``; ``deploy/postgres/domain-tenant-roles.sql`` grants
each one an explicit table matrix. SQLite has no roles, so a write the
PostgreSQL role may not make passes every SQLite test and fails in production
(#46). The test suite records every write together with the role entry it ran
under and checks it against that matrix (``tests/support/grant_guard.py``).

:func:`runs_as` marks a Worker entry: each runtime's ``run_once``, the Job
handler methods it dispatches to, and the domain handler functions those
call (``app.localization.worker.handle_*``, ``run_scale_evaluation``, ...),
which tests also drive directly. Only mark a function no API code calls. The
API is marked per HTTP request by the test suite itself. Marking is off unless
``MARKETAI_RECORD_RUNTIME_ROLE=1`` is set before this module is imported
(``tests/conftest.py`` does): :func:`runs_as` then returns the function
itself, so production pays nothing for it.

The innermost entry wins: a Worker handler is only ever run by a Worker
process, so a test that runs one inside a request (to simulate the Worker
finishing mid-read) still gets the Worker role for it. ``app/`` code never
calls a Worker handler from the API.
"""
from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

API_ROLE = "marketai_domain_api"
WORKER_ROLE = "marketai_domain_worker"

RECORDING = os.environ.get("MARKETAI_RECORD_RUNTIME_ROLE") == "1"

_current: ContextVar[str | None] = ContextVar("marketai_runtime_role", default=None)

F = TypeVar("F", bound=Callable)


def current_role() -> str | None:
    """The role of the innermost entry on the stack, or None outside one."""
    return _current.get()


@contextmanager
def entered_as(role: str) -> Iterator[None]:
    """Run the block as ``role``."""
    token = _current.set(role)
    try:
        yield
    finally:
        _current.reset(token)


def runs_as(role: str) -> Callable[[F], F]:
    """Mark a function as an entry that runs as ``role``.

    Returns the function unchanged unless the test suite turned recording
    on, so it costs nothing outside tests.
    """

    def decorate(function: F) -> F:
        if not RECORDING:
            return function
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def entry(*args, **kwargs):
                with entered_as(role):
                    return await function(*args, **kwargs)

        else:

            @functools.wraps(function)
            def entry(*args, **kwargs):
                with entered_as(role):
                    return function(*args, **kwargs)

        return entry  # type: ignore[return-value]

    return decorate
