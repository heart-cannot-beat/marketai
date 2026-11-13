"""Market codes for market policies in the shared test database (#81).

``market_policies.market_id`` is unique across the whole database
(``uq_mp_market_id``), and every test file one pytest process runs shares
one SQLite database (``tests/conftest.py``). CI spreads files over workers
with ``-n logical --dist loadfile``, so which files share a database changes
from run to run. Two helpers that pick codes on their own therefore collide
on some runs only.

Two-letter codes (the shape ``app.experiments.service`` accepts for an
Experiment market) come from exactly two places:

* :func:`next_market_code`: one generator per process, shared by every
  module, that never repeats a code and never hands out a fixed one;
* :data:`FIXED_MARKET_CODES`: codes a test needs verbatim, each owned by one
  test file. Take one with :func:`fixed_market_code`.

:func:`guard_shared_database` (installed by ``tests/conftest.py``) checks
every market policy written to the shared database, so breaking either rule
fails with an AssertionError that names the rule, on every run, instead of
an occasional IntegrityError.
"""
from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from string import ascii_uppercase

REPO_ROOT = Path(__file__).resolve().parents[2]

# Code -> the one test file that may write it to the shared database.
# next_market_code() never hands these out.
FIXED_MARKET_CODES: dict[str, str] = {
    # Scale handoff: the confirmed plan, the offer snapshot and the
    # DeliveryTarget all name US, and the test asserts on them.
    "US": "tests/test_marketing_evidence_handoff.py",
    # Localization draft target for the handed-off source.
    "GB": "tests/test_marketing_evidence_handoff.py",
}

TWO_LETTER_CODE = re.compile(r"[A-Z]{2}")


def generated_code_space() -> Iterator[str]:
    """Every code :func:`next_market_code` can hand out, in order: AA..ZZ
    without the fixed codes."""
    for first, second in itertools.product(ascii_uppercase, repeat=2):
        code = first + second
        if code not in FIXED_MARKET_CODES:
            yield code


_codes = generated_code_space()
_issued: set[str] = set()


def next_market_code() -> str:
    """A two-letter market code no other caller in this process has had."""
    code = next(_codes, None)
    if code is None:
        raise AssertionError(
            f"next_market_code() has handed out all {len(_issued)} two-letter "
            "market codes in this process; give new fixtures a longer, "
            "prefixed market id instead"
        )
    _issued.add(code)
    return code


def issued_market_codes() -> frozenset[str]:
    """Codes :func:`next_market_code` has handed out in this process."""
    return frozenset(_issued)


def fixed_market_code(code: str) -> str:
    """``code``, once it is registered in :data:`FIXED_MARKET_CODES`."""
    if code not in FIXED_MARKET_CODES:
        raise AssertionError(
            f"market code {code!r} is not registered in "
            "tests.support.market_codes.FIXED_MARKET_CODES; register it with "
            "its owning test file, or use next_market_code()"
        )
    return code


def _running_test(item: object | None) -> tuple[str | None, str]:
    """(repo-relative file, node id) of the pytest item that is running."""
    if item is None:
        return None, "code outside any test"
    path = Path(str(getattr(item, "path"))).resolve()
    try:
        test_file = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        test_file = path.as_posix()
    return test_file, getattr(item, "nodeid")


def guard_shared_database(
    database_url: str, current_item: Callable[[], object | None]
) -> Callable[[], None]:
    """Check every market policy written to the database at ``database_url``.

    ``current_item`` returns the pytest item that is running (its setUpClass
    and fixtures included), or None. Returns a function that removes the
    check.
    """
    from sqlalchemy import event, select
    from sqlalchemy.engine import make_url

    from app.adapters.persistence.models import MarketPolicyRecord

    shared = make_url(database_url)
    table = MarketPolicyRecord.__table__
    written_by: dict[str, str] = {}

    def in_shared_database(connection) -> bool:
        url = connection.engine.url
        return (
            url.get_backend_name() == shared.get_backend_name()
            and url.database == shared.database
        )

    def check(mapper, connection, target) -> None:
        if not in_shared_database(connection):
            return
        code = target.market_id
        test_file, test_id = _running_test(current_item())
        if isinstance(code, str) and TWO_LETTER_CODE.fullmatch(code):
            owner = FIXED_MARKET_CODES.get(code)
            if owner is not None and test_file is not None and test_file != owner:
                raise AssertionError(
                    f"market code {code!r} is reserved for {owner} in "
                    "tests.support.market_codes.FIXED_MARKET_CODES, but "
                    f"{test_id} wrote it; use next_market_code() instead"
                )
            if owner is None and code not in _issued:
                raise AssertionError(
                    f"two-letter market code {code!r} written by {test_id} "
                    "was neither handed out by "
                    "tests.support.market_codes.next_market_code() nor "
                    "registered in FIXED_MARKET_CODES; one of them must "
                    "own every two-letter code in the shared test database"
                )
        existing = connection.execute(
            select(table.c.id).where(table.c.market_id == code).limit(1)
        ).first()
        if existing is not None:
            raise AssertionError(
                f"market code {code!r} already has a market policy in this "
                "process's shared test database (written by "
                f"{written_by.get(code, 'an earlier write')}); {test_id} must "
                "use next_market_code(), its own fixed code, or a helper "
                "that reuses the existing policy"
            )

    def remember(mapper, connection, target) -> None:
        if in_shared_database(connection):
            written_by[target.market_id] = _running_test(current_item())[1]

    event.listen(MarketPolicyRecord, "before_insert", check)
    event.listen(MarketPolicyRecord, "after_insert", remember)

    def remove() -> None:
        event.remove(MarketPolicyRecord, "before_insert", check)
        event.remove(MarketPolicyRecord, "after_insert", remember)

    return remove
