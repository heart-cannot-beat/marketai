"""Every statement API and Worker code sends in the suite has a PostgreSQL
grant (#59).

SQLite has no database roles, so a statement ``marketai_domain_api`` or
``marketai_domain_worker`` may not run passes every SQLite test and returns
500 in production (heart-cannot-beat/marketai-archive#284,
heart-cannot-beat/marketai-archive#347, #46).

``tests/conftest.py`` marks each HTTP request as the API role, and
:func:`app.platform.runtime_role.runs_as` marks the Worker runtimes'
``run_once`` and Job handlers as the Worker role. :class:`GrantGuard` sees
every statement the suite sends to a database (``before_cursor_execute``)
and records what it needs as ``role x table x privilege``:

* INSERT, UPDATE (with its columns) and DELETE;
* SELECT on every table it reads (``FROM``/``JOIN``), on the columns it
  names where the role holds column grants only, and on the columns a
  write's ``WHERE``/``SET`` expressions, ``ON CONFLICT (...)`` target or
  ``RETURNING`` list read;
* UPDATE on at least one column of each table a ``SELECT ... FOR
  UPDATE/SHARE`` locks (``LOCK``). SQLite has no row locks and SQLAlchemy
  leaves them out of its SQL, so the guard reads the lock from the statement
  object instead.

Each is checked against the matrix :mod:`tests.support.grant_matrix`
evaluates from ``deploy/postgres/domain-tenant-roles.sql``. A statement with
no grant fails the test that first sent it, at its teardown, naming the
role, table, privilege, columns and the ``app/`` line that sent it.

Statements outside any role entry (fixtures and seeds writing directly, or
service functions a test calls without going through a request or a Job)
are not checked; :meth:`GrantGuard.summary` counts them.

Two lists below let a statement through:

* :data:`SQLITE_ONLY_WRITES`: statements SQLite runs where PostgreSQL takes
  another path (a SECURITY DEFINER function, a provisioned projection), keyed
  by the function that sends them, so the same statement from anywhere else
  is still checked.
* :data:`KNOWN_GAPS`: a real missing grant, each with the issue that tracks
  it. Remove the entry when that issue's fix lands.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.support.grant_matrix import API_ROLE, WORKER_ROLE, GrantMatrix, load_grant_matrix

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = str(REPO_ROOT / "app") + os.sep


@dataclass(frozen=True)
class SqliteOnlyWrite:
    """A statement only the SQLite path sends; ``site`` is the
    ``module:qualname`` of the function that sends it. ``when`` is
    ``(argument, value)`` when only calls with that argument take the
    SQLite-only path."""

    table: str
    operation: str
    site: str
    reason: str
    when: tuple[str, object] | None = None


@dataclass(frozen=True)
class KnownGap:
    """A missing grant the guard found, tracked by ``issue``; only what
    ``site`` (``module:qualname``) sends is let through. ``operation`` is a
    privilege, or ``LOCK`` for a row lock without UPDATE on any column."""

    role: str
    table: str
    operation: str
    site: str
    issue: int


_PROJECTION_INGEST = "app.entitlements.projection:EntitlementProjectionModule.ingest"
_PROJECTED = (
    "PostgreSQL applies the signed snapshot through the SECURITY DEFINER "
    "marketai_apply_workspace_projection_v2(); only SQLite writes the rows "
    "from Python (_apply_sqlite)"
)
_PROVISIONED = (
    "SQLite creates a missing Workspace on first use; PostgreSQL raises "
    "TenantScopeError instead, because the control-plane projection owns "
    "Workspace rows"
)
_PLATFORM_WORKSPACE = (
    "PostgreSQL creates the platform Workspace through the SECURITY DEFINER "
    "marketai_create_platform_workspace_v1() (#91); only SQLite writes the row "
    "from Python"
)

_BINDINGS_VIEW = (
    "PostgreSQL answers the API from the workspace_provider_bindings view (binding id and "
    "revision, no secret column); only SQLite, which has no such view, reads the binding "
    "and credential records themselves. include_secret=True (Worker) reads them everywhere"
)

SQLITE_ONLY_WRITES: tuple[SqliteOnlyWrite, ...] = (
    SqliteOnlyWrite("entitlement_snapshot_inbox", "INSERT", _PROJECTION_INGEST, _PROJECTED),
    SqliteOnlyWrite("entitlement_projections", "INSERT", _PROJECTION_INGEST, _PROJECTED),
    SqliteOnlyWrite("entitlement_projections", "UPDATE", _PROJECTION_INGEST, _PROJECTED),
    SqliteOnlyWrite("workspaces", "UPDATE", _PROJECTION_INGEST, _PROJECTED),
    SqliteOnlyWrite("workspaces", "INSERT", "app.store:_ensure_workspace", _PROVISIONED),
    SqliteOnlyWrite("workspaces", "INSERT", "app.meta.service:_ensure_workspace_and_account", _PROVISIONED),
    SqliteOnlyWrite(
        "workspaces", "INSERT", "app.meta.platform_api:_create_platform_workspace", _PLATFORM_WORKSPACE,
    ),
    SqliteOnlyWrite(
        "credential_versions", "SELECT", "app.generation.selection:credential_binding",
        _BINDINGS_VIEW, when=("include_secret", False),
    ),
)

# Empty while every gap the guard has found is fixed; a new one goes here
# with the issue that tracks it.
KNOWN_GAPS: tuple[KnownGap, ...] = ()


# --------------------------------------------------------- classification --

_NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED = rf"{_NAME}(?:\s*\.\s*{_NAME})?"
_INSERT = re.compile(
    rf"INSERT\s+(?:OR\s+(?P<or>\w+)\s+)?INTO\s+(?P<table>{_QUALIFIED})\s*(?P<cols>\()?",
    re.IGNORECASE,
)
_REPLACE = re.compile(rf"REPLACE\s+INTO\s+(?P<table>{_QUALIFIED})\s*(?P<cols>\()?", re.IGNORECASE)
_UPDATE = re.compile(
    rf"UPDATE\s+(?:OR\s+\w+\s+)?(?P<table>{_QUALIFIED})(?:\s+(?:AS\s+)?{_NAME})?\s+SET\s",
    re.IGNORECASE,
)
_DELETE = re.compile(rf"DELETE\s+FROM\s+(?P<table>{_QUALIFIED})", re.IGNORECASE)
_DO_UPDATE = re.compile(r"\bDO\s+UPDATE\s+SET\s", re.IGNORECASE)
_CONFLICT_TARGET = re.compile(r"\bON\s+CONFLICT\s*\(", re.IGNORECASE)
_RETURNING = re.compile(r"\bRETURNING\s", re.IGNORECASE)
_LEADING = re.compile(r"\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*", re.DOTALL)
_SET_STOP_WORDS = {"WHERE", "FROM", "RETURNING", "ORDER", "LIMIT", "ON"}

Write = tuple[str, str, "frozenset[str] | None"]  # (operation, table, columns)


def _unquote(name: str) -> str:
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.lower()


def _table(qualified: str) -> str:
    return _unquote(re.split(r"\s*\.\s*(?=[\"A-Za-z_])", qualified)[-1])


def _balanced(sql: str, start: int) -> int:
    """Index just past the parenthesis closing the one at ``start``."""
    depth = 0
    quote = None
    index = start
    while index < len(sql):
        character = sql[index]
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(sql)


def _top_level_items(sql: str, start: int, stop_words: set[str] | None) -> list[str]:
    """Comma-separated items from ``start`` at depth 0, up to a closing
    parenthesis or (when given) a stop word."""
    items: list[str] = []
    depth = 0
    quote = None
    item_start = start
    index = start
    while index < len(sql):
        character = sql[index]
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and character == ",":
            items.append(sql[item_start:index])
            item_start = index + 1
        elif (
            depth == 0
            and stop_words
            and character.isalpha()
            and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] in "_.\""))
        ):
            word = re.match(r"[A-Za-z_]+", sql[index:]).group(0)
            if word.upper() in stop_words:
                break
            index += len(word)
            continue
        index += 1
    items.append(sql[item_start:index])
    return [item for item in (part.strip() for part in items) if item]


def _assigned_columns(sql: str, start: int) -> frozenset[str]:
    columns: set[str] = set()
    for assignment in _top_level_items(sql, start, _SET_STOP_WORDS):
        target = assignment.split("=", 1)[0].strip()
        if target.startswith("("):
            names = target[1:].rstrip(")").split(",")
        else:
            names = [target]
        for name in names:
            columns.add(_unquote(name.strip().split(".")[-1]))
    return frozenset(columns)


def classify(sql: str) -> tuple[Write, ...]:
    """The privileges one write statement needs: ``(privilege, table,
    columns)``.

    ``columns`` is the SET list for an UPDATE (PostgreSQL checks UPDATE per
    column), the column list for an INSERT, and None for a DELETE or an
    INSERT without one. A write also needs SELECT on what its ``ON CONFLICT
    (...)`` target and its ``RETURNING`` list read. Statements that write
    nothing classify as ``()``; reads on their own are not checked.
    """
    position = _LEADING.match(sql).end()
    head = sql[position:position + 7].upper()
    if head.startswith("WITH"):
        return _classify_with(sql, position + 4)
    if head.startswith("INSERT"):
        match = _INSERT.match(sql, position)
        if match is None:
            return ()
        table = _table(match.group("table"))
        columns = (
            frozenset(_unquote(c) for c in _top_level_items(sql, match.end(), None))
            if match.group("cols")
            else None
        )
        writes: list[Write] = [("INSERT", table, columns)]
        conflict = (match.group("or") or "").upper()
        if conflict == "REPLACE":
            writes.append(("DELETE", table, None))
        elif conflict not in {"", "IGNORE", "ABORT", "FAIL", "ROLLBACK"}:
            raise AssertionError(f"unexpected INSERT OR {conflict}: {sql[:120]}")
        upsert = _DO_UPDATE.search(sql, match.end())
        if upsert is not None:
            writes.append(("UPDATE", table, _assigned_columns(sql, upsert.end())))
        target = _CONFLICT_TARGET.search(sql, match.end())
        if target is not None:
            # PostgreSQL reads the conflict target's columns: SELECT on them.
            writes.append(("SELECT", table, frozenset(
                _unquote(c.split(".")[-1]) for c in _top_level_items(sql, target.end(), None)
            )))
        return tuple(writes) + _returning(sql, table)
    if head.startswith("REPLACE"):
        match = _REPLACE.match(sql, position)
        if match is None:
            return ()
        table = _table(match.group("table"))
        return (("INSERT", table, None), ("DELETE", table, None))
    if head.startswith("UPDATE"):
        match = _UPDATE.match(sql, position)
        if match is None:
            return ()
        table = _table(match.group("table"))
        return (("UPDATE", table, _assigned_columns(sql, match.end())),) + _returning(sql, table)
    if head.startswith("DELETE"):
        match = _DELETE.match(sql, position)
        if match is None:
            return ()
        table = _table(match.group("table"))
        return (("DELETE", table, None),) + _returning(sql, table)
    return ()


_FROM_OR_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+", re.IGNORECASE)
_TABLE_REF = re.compile(
    rf"(?P<table>{_QUALIFIED})(?P<call>\s*\()?(?:\s+(?:AS\s+)?(?P<alias>{_NAME}))?",
    re.IGNORECASE,
)
_FROM_LIST_STOP_WORDS = {
    "WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING", "WINDOW", "UNION", "INTERSECT",
    "EXCEPT", "RETURNING", "FOR",
}
_CTE_NAME = re.compile(
    rf"(?:\bWITH\s+(?:RECURSIVE\s+)?|\)\s*,\s*)(?P<name>{_NAME})\s*(?:\([^)]*\)\s*)?"
    r"AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(",
    re.IGNORECASE,
)
_LITERAL = re.compile(r"'(?:[^']|'')*'")
#: Words that can follow a table reference without being its alias.
_NOT_ALIASES = frozenset({
    "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "OUTER", "ON",
    "USING", "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING", "WINDOW", "UNION", "INTERSECT",
    "EXCEPT", "SET", "RETURNING", "VALUES", "SELECT", "FOR", "DEFAULT", "INDEXED", "NOT",
})


@dataclass(frozen=True)
class Read:
    """A table one statement reads: ``names`` are what its columns are
    qualified with (the table and its aliases). ``scanned`` is true when the
    statement scans it (a ``FROM`` or ``JOIN``), so PostgreSQL wants SELECT
    on at least one column even when none is named (``count(*)``); a write's
    target only needs SELECT on the columns its ``WHERE`` or ``SET``
    expressions read. ``top_level`` (only for a scanned table) is true outside
    any parenthesis: the tables ``SELECT ... FOR UPDATE`` locks."""

    table: str
    names: frozenset[str]
    scanned: bool
    top_level: bool = False


def reads(sql: str) -> tuple[Read, ...]:
    """The tables one statement reads, so the guard can check SELECT on
    them: every ``FROM``/``JOIN`` reference (CTE names, subqueries and
    table-valued functions aside) and the target of an UPDATE, DELETE or
    upsert, whose ``WHERE`` and ``SET`` expressions read its columns."""
    if "'" in sql:
        sql = _LITERAL.sub("''", sql)
    ctes = (
        {_unquote(m.group("name")) for m in _CTE_NAME.finditer(sql)}
        if re.search(r"\bWITH\b", sql, re.IGNORECASE)
        else set()
    )
    found: dict[str, Read] = {}
    depth, counted = 0, 0
    for keyword in _FROM_OR_JOIN.finditer(sql):
        before = sql[max(0, keyword.start() - 10):keyword.start()].rstrip().upper()
        if before.endswith(("DELETE", "DISTINCT")):
            continue  # the DELETE target; IS [NOT] DISTINCT FROM
        position = keyword.end()
        depth += sql.count("(", counted, position) - sql.count(")", counted, position)
        counted = position
        # FROM a JOIN b ON ..., c: each comma-separated item starts with a
        # table (JOINs inside an item are found as their own keyword).
        items = (
            _top_level_items(sql, position, _FROM_LIST_STOP_WORDS)
            if keyword.group(0)[0] in "Ff"
            else [sql[position:position + 200]]
        )
        for item in items:
            match = _TABLE_REF.match(item)
            if match is None or match.group("call"):
                continue  # a subquery, VALUES or a table-valued function
            table = _table(match.group("table"))
            if table in ctes or table.startswith(("sqlite_", "pragma_")):
                continue
            alias = match.group("alias")
            names = {table} | ({_unquote(alias)} if alias and alias.upper() not in _NOT_ALIASES else set())
            previous = found.get(table)
            if previous is not None:
                names |= previous.names
            found[table] = Read(
                table, frozenset(names), True,
                depth == 0 or (previous is not None and previous.top_level),
            )
    for operation, table, _columns in classify(sql):
        if operation in {"UPDATE", "DELETE"} and table not in found:
            found[table] = Read(table, frozenset({table}), False)
    return tuple(found.values())


def qualified_columns(sql: str, names: frozenset[str]) -> frozenset[str] | None:
    """The columns ``sql`` names through any of ``names`` (``name.column``);
    None when it reads a whole row (``name.*``). SQLAlchemy qualifies every
    column it renders, so this sees what ORM and Core statements read."""
    alternatives = "|".join(
        alternative
        for name in sorted(names)
        for alternative in (re.escape(name), re.escape(f'"{name}"'))
    )
    columns: set[str] = set()
    for match in re.finditer(
        rf'(?<![\w."])(?:{alternatives})\s*\.\s*(\*|"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)',
        _LITERAL.sub("''", sql),
        re.IGNORECASE,
    ):
        if match.group(1) == "*":
            return None
        columns.add(_unquote(match.group(1)))
    return frozenset(columns)


def _returning(sql: str, table: str) -> tuple[Write, ...]:
    """``RETURNING`` reads what it returns: SELECT on those columns (None:
    ``RETURNING *`` or an expression, which needs more than named columns)."""
    found = None
    for found in _RETURNING.finditer(sql):
        pass
    if found is None:
        return ()
    columns: set[str] = set()
    for item in _top_level_items(sql, found.end(), None):
        name = re.split(r"\s+AS\s+", item, flags=re.IGNORECASE)[0].strip()
        if not re.fullmatch(rf"(?:{_NAME}\s*\.\s*)?{_NAME}", name):
            return (("SELECT", table, None),)
        columns.add(_unquote(re.split(r"\s*\.\s*", name)[-1]))
    return (("SELECT", table, frozenset(columns)),)


def _classify_with(sql: str, index: int) -> tuple[Write, ...]:
    """``WITH [RECURSIVE] name [(cols)] AS [NOT] [MATERIALIZED] (...), ...``
    then the main statement; data-modifying CTE bodies count too."""
    writes: list[Write] = []
    recursive = re.match(r"\s*RECURSIVE\b", sql[index:], re.IGNORECASE)
    if recursive:
        index += recursive.end()
    while True:
        definition = re.match(
            rf"\s*{_NAME}\s*(?:\([^)]*\)\s*)?AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(",
            sql[index:],
            re.IGNORECASE,
        )
        if definition is None:
            break
        body_start = index + definition.end() - 1
        body_end = _balanced(sql, body_start)
        writes.extend(classify(sql[body_start + 1:body_end - 1]))
        index = body_end
        comma = re.match(r"\s*,", sql[index:])
        if comma is None:
            break
        index += comma.end()
    writes.extend(classify(sql[index:]))
    return tuple(writes)


# ------------------------------------------------------------------ guard --

_WRITES = frozenset({"INSERT", "UPDATE", "DELETE"})

#: Why a statement needs the privilege a violation names.
_WHY = {
    "write": "",
    "returning": ", which the write's ON CONFLICT target or RETURNING list reads",
    "read": ", which the statement reads",
    "where": ", which the write's WHERE or SET expressions read",
    "lock": "",
}


@dataclass
class Violation:
    role: str
    table: str
    operation: str
    columns: frozenset[str] | None
    test: str
    site: str
    why: str = "write"
    reported: bool = False

    def describe(self) -> str:
        columns = f" ({', '.join(sorted(self.columns))})" if self.columns else ""
        if self.operation == "LOCK":
            grant = (
                f"no UPDATE grant on any column of {self.table}, which SELECT ... FOR "
                "UPDATE/SHARE needs to lock its rows,"
            )
        else:
            grant = f"no {self.operation} grant on {self.table}{columns}{_WHY[self.why]}"
        return (
            f"{self.role} has {grant} in deploy/postgres/domain-tenant-roles.sql\n"
            f"    first made by {self.test}\n"
            f"    sent from {self.site}"
        )


@dataclass
class _Tally:
    count: int = 0
    first_test: str = ""


@dataclass(frozen=True)
class _Plan:
    """What one statement string needs, worked out once: its writes (and the
    SELECT they need), the tables it reads, and which of those a ``FOR
    UPDATE`` would lock (only for a statement that writes nothing)."""

    writes: tuple[Write, ...]
    reads: tuple[Read, ...]
    lockable: tuple[str, ...]


def _plan(statement: str) -> _Plan | None:
    writes = classify(statement)
    found = reads(statement)
    if not writes and not found:
        return None
    lockable = () if writes else tuple(r.table for r in found if r.top_level)
    return _Plan(writes, found, lockable)


#: A need: ``(privilege, table, columns, why)``; ``LOCK`` is UPDATE on any column.
Need = tuple[str, str, "frozenset[str] | None", str]


@dataclass
class GrantGuard:
    matrix: GrantMatrix
    current_test: Callable[[], str]
    #: The roles this guard checks; statements marked with any other role
    #: are someone else's (the guard's own tests use made-up roles).
    roles: frozenset[str] = frozenset({API_ROLE, WORKER_ROLE})
    sqlite_only: tuple[SqliteOnlyWrite, ...] = SQLITE_ONLY_WRITES
    known_gaps: tuple[KnownGap, ...] = KNOWN_GAPS

    _plans: dict[str, _Plan | None] = field(default_factory=dict)
    _checked: dict[tuple, tuple[tuple[_Tally, ...], tuple[Need, ...]]] = field(default_factory=dict)
    #: ``(role or None, privilege, table) -> how often and first by which test``.
    seen: dict[tuple[str | None, str, str], _Tally] = field(default_factory=dict)
    violations: dict[tuple, Violation] = field(default_factory=dict)
    exempted: Counter = field(default_factory=Counter)
    unmarked_entries: Counter | None = None

    def __post_init__(self) -> None:
        from app.platform.runtime_role import current_role

        self._current_role = current_role
        self._gap_sites: dict[tuple[str, str, str], dict[str, None]] = {}
        for gap in self.known_gaps:
            self._gap_sites.setdefault((gap.role, gap.table, gap.operation), {})[gap.site] = None
        self._sqlite_sites: dict[tuple[str, str], dict[str, tuple[str, object] | None]] = {}
        for entry in self.sqlite_only:
            self._sqlite_sites.setdefault((entry.table, entry.operation), {})[entry.site] = entry.when
        if os.environ.get("MARKETAI_GRANT_GUARD_PROFILE") == "1":
            self.unmarked_entries = Counter()

    # -- the event ------------------------------------------------------

    def before_cursor_execute(self, conn, cursor, statement, parameters, context, executemany):
        plan = self._plans.get(statement, False)
        if plan is False:
            plan = self._plans[statement] = _plan(statement)
        if plan is None:
            return
        role = self._current_role()
        if role is not None and role not in self.roles:
            return
        locked = False
        if plan.lockable and role is not None:
            # SQLite drops FOR UPDATE from the SQL; the statement keeps it.
            compiled = getattr(context, "compiled", None)
            locked = getattr(getattr(compiled, "statement", None), "_for_update_arg", None) is not None
        key = (role, statement, locked)
        checked = self._checked.get(key)
        if checked is None:
            checked = self._checked[key] = self._check(role, statement, plan, locked)
        tallies, refused = checked
        for tally in tallies:
            tally.count += 1
        for need in refused:
            self._refused(role, *need)
        if role is None and self.unmarked_entries is not None:
            for operation, table, _columns in plan.writes:
                if operation != "SELECT":
                    self.unmarked_entries[(_entry_site(), operation, table)] += 1

    def _check(self, role, statement, plan: _Plan, locked: bool):
        """The tallies one execution counts and the needs the matrix refuses,
        for this role, statement and lock: the same every time it runs."""
        needs: list[Need] = [
            (operation, table, columns, "returning" if operation == "SELECT" else "write")
            for operation, table, columns in plan.writes
        ]
        refused: list[Need] = [] if role is None else [
            need for need in needs if not self.matrix.allows(role, need[1], need[0], need[2])
        ]
        for read in plan.reads:
            needs.append(("SELECT", read.table, None, "read"))
            if role is not None:
                missing = self._missing_select(role, statement, read)
                if missing is not False:
                    refused.append(("SELECT", read.table, missing, "read" if read.scanned else "where"))
        if locked:
            for table in plan.lockable:
                needs.append(("LOCK", table, None, "lock"))
                if role is not None and not self.matrix.privileges(role, table).allows_any("UPDATE"):
                    refused.append(("LOCK", table, None, "lock"))
        tallies = tuple(
            self._tally(role, operation, table)
            for operation, table in dict.fromkeys((n[0], n[1]) for n in needs)
        )
        return tallies, tuple(refused)

    def _missing_select(self, role, statement, read: Read) -> frozenset[str] | None | bool:
        """False when ``role`` may read what ``statement`` reads of
        ``read.table``; otherwise the columns it may not read (None: the
        whole table, or no column at all)."""
        privileges = self.matrix.privileges(role, read.table)
        if "SELECT" in privileges.table:
            return False
        granted = privileges.columns.get("SELECT", set())
        if read.scanned and not granted:
            return None
        columns = qualified_columns(statement, read.names)
        if columns is None:
            return None
        missing = columns - granted
        if missing:
            return frozenset(missing)
        return False

    def _tally(self, role, operation, table) -> _Tally:
        key = (role, operation, table)
        tally = self.seen.get(key)
        if tally is None:
            tally = self.seen[key] = _Tally(first_test=self.current_test())
        return tally

    def _refused(self, role, operation, table, columns, why) -> None:
        sites = self._sqlite_sites.get((table, operation))
        if sites and _stack_contains(sites):
            self.exempted[("sqlite-only", role, operation, table)] += 1
            return
        gap_sites = self._gap_sites.get((role, table, operation))
        if gap_sites and _stack_contains(gap_sites):
            self.exempted[("known-gap", role, operation, table)] += 1
            return
        # One violation per sending line: a second site with the same
        # missing grant is reported too, not hidden behind the first.
        site = _app_site()
        key = (role, operation, table, columns, site)
        if key not in self.violations:
            self.violations[key] = Violation(
                role, table, operation, columns, self.current_test(), site, why
            )

    # -- reporting ------------------------------------------------------

    def unreported(self) -> list[Violation]:
        found = [v for v in self.violations.values() if not v.reported]
        for violation in found:
            violation.reported = True
        return found

    def summary(self) -> dict:
        """What the guard saw, as JSON-ready data: per role, ``writes``
        counts INSERT/UPDATE/DELETE needs, ``reads`` the SELECTs (plain
        reads and what writes read), ``locks`` the ``FOR UPDATE/SHARE``
        tables."""
        by_role: dict[str, dict[str, dict[str, int]]] = {}
        for (role, operation, table), tally in self.seen.items():
            by_role.setdefault(role or "unmarked", {}).setdefault(table, {})[operation] = tally.count

        def stats(tables: dict[str, dict[str, int]]) -> dict[str, int]:
            written = {t: {o: n for o, n in ops.items() if o in _WRITES} for t, ops in tables.items()}
            written = {t: ops for t, ops in written.items() if ops}
            return {
                "tables": len(written),
                "pairs": sum(len(ops) for ops in written.values()),
                "writes": sum(sum(ops.values()) for ops in written.values()),
                "reads": sum(ops.get("SELECT", 0) for ops in tables.values()),
                "read_tables": sum(1 for ops in tables.values() if "SELECT" in ops),
                "locks": sum(ops.get("LOCK", 0) for ops in tables.values()),
                "locked_tables": sum(1 for ops in tables.values() if "LOCK" in ops),
            }

        marked_tables = {
            table
            for role, tables in by_role.items()
            if role != "unmarked"
            for table, ops in tables.items()
            if set(ops) & _WRITES
        }
        unmarked_tables = {t for t, ops in by_role.get("unmarked", {}).items() if set(ops) & _WRITES}
        return {
            "roles": {role: stats(tables) for role, tables in sorted(by_role.items())},
            "tables_marked": len(marked_tables),
            "tables_only_unmarked": sorted(unmarked_tables - marked_tables),
            "violations": [v.describe() for v in self.violations.values()],
            "exempted": {" ".join(map(str, k)): n for k, n in self.exempted.items()},
            "detail": {
                role: {table: dict(sorted(ops.items())) for table, ops in sorted(tables.items())}
                for role, tables in sorted(by_role.items())
            },
            "first_tests": {
                f"{role or 'unmarked'} {operation} {table}": tally.first_test
                for (role, operation, table), tally in sorted(
                    self.seen.items(), key=lambda kv: (kv[0][0] or "", kv[0][1], kv[0][2])
                )
            },
            "unmarked_entries": (
                [
                    {"site": site, "operation": op, "table": table, "count": n}
                    for (site, op, table), n in self.unmarked_entries.most_common()
                ]
                if self.unmarked_entries is not None
                else None
            ),
        }

    def write_report(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.summary(), indent=2), encoding="utf-8")

    def summary_line(self) -> str:
        data = self.summary()
        roles = data["roles"]
        empty = dict.fromkeys(
            ("tables", "pairs", "writes", "reads", "read_tables", "locks", "locked_tables"), 0
        )
        parts = []
        for role in (API_ROLE, WORKER_ROLE):
            counts = roles.get(role, empty)
            parts.append(
                f"{role.removeprefix('marketai_domain_')} {counts['writes']} writes "
                f"({counts['pairs']} table-operations on {counts['tables']} tables), "
                f"{counts['reads']} reads of {counts['read_tables']} tables, "
                f"{counts['locks']} row locks on {counts['locked_tables']} tables"
            )
        unmarked = roles.get("unmarked", empty)
        return (
            "grant guard (#59): checked " + "; ".join(parts)
            + f"; {len(data['violations'])} missing grants, "
            + f"{sum(data['exempted'].values())} exempted statements; "
            + f"outside any role entry (not checked): {unmarked['writes']} writes on "
            + f"{unmarked['tables']} tables, {len(data['tables_only_unmarked'])} of them "
            + "never written under a role"
        )

    # -- installation ---------------------------------------------------

    def install(self) -> Callable[[], None]:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        event.listen(Engine, "before_cursor_execute", self.before_cursor_execute)

        def remove() -> None:
            event.remove(Engine, "before_cursor_execute", self.before_cursor_execute)

        return remove


_UNSET = object()


def _stack_contains(sites: dict[str, tuple[str, object] | None]) -> bool:
    """Whether a function in ``sites`` is sending this statement; a site
    with a condition ``(argument, value)`` counts only when called so."""
    frame = sys._getframe(2)
    while frame is not None:
        site = f"{frame.f_globals.get('__name__')}:{frame.f_code.co_qualname}"
        if site in sites:
            condition = sites[site]
            if condition is None or frame.f_locals.get(condition[0], _UNSET) == condition[1]:
                return True
        frame = frame.f_back
    return False


_PLUMBING = (
    os.path.join("app", "adapters", "persistence", "database.py"),
    os.path.join("tests", "support", "grant_guard.py"),
)


def _app_site() -> str:
    """Where the write came from: the innermost ``app/`` frame that is not
    the persistence plumbing, else the innermost repository frame."""
    frame = sys._getframe(1)
    repository_frame = None
    while frame is not None:
        filename = frame.f_code.co_filename
        if (
            filename.startswith(str(REPO_ROOT))
            and not filename.endswith(_PLUMBING)
            and "site-packages" not in filename
        ):
            relative = os.path.relpath(filename, REPO_ROOT)
            where = f"{relative}:{frame.f_lineno} in {frame.f_code.co_qualname}"
            if filename.startswith(APP_ROOT):
                return where
            repository_frame = repository_frame or where
        frame = frame.f_back
    return repository_frame or "outside the repository"


def _entry_site() -> str:
    """The outermost ``app/`` function on the stack (profiling only): which
    entry a test called to make an unmarked write."""
    frame = sys._getframe(1)
    outermost = None
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith(APP_ROOT):
            outermost = f"{frame.f_globals.get('__name__')}:{frame.f_code.co_qualname}"
        frame = frame.f_back
    return outermost or "tests"


def guard_from_environment(current_test: Callable[[], str]) -> GrantGuard | None:
    """The suite's guard, unless ``MARKETAI_GRANT_GUARD=off``."""
    if os.environ.get("MARKETAI_GRANT_GUARD", "on").lower() in {"off", "0", "false"}:
        return None
    return GrantGuard(load_grant_matrix(), current_test)


def failure_message(violations: list[Violation]) -> str:
    lines = [
        "grant guard (#59): PostgreSQL will refuse "
        + ("this statement" if len(violations) == 1 else f"these {len(violations)} statements")
        + " (SQLite has no roles):",
    ]
    lines.extend(violation.describe() for violation in violations)
    lines.append(
        "Grant it in deploy/postgres/domain-tenant-roles.sql (and the migration that "
        "needs it), or, when PostgreSQL takes another path than SQLite here, list the "
        "sending function in tests/support/grant_guard.py SQLITE_ONLY_WRITES."
    )
    return "\n".join(lines)


def mark_api_requests() -> Callable[[], None]:
    """Run every HTTP request any FastAPI app in the suite serves as the API
    role: the app built in ``app.main`` and the probe apps tests assemble from
    its routers alike."""
    from fastapi import FastAPI

    from app.platform.runtime_role import API_ROLE, entered_as

    original = FastAPI.__call__

    async def serve(self, scope, receive, send):
        if scope["type"] != "http":
            await original(self, scope, receive, send)
            return
        with entered_as(API_ROLE):
            await original(self, scope, receive, send)

    FastAPI.__call__ = serve

    def remove() -> None:
        FastAPI.__call__ = original

    return remove
