"""Assert that stored strings fit their ``String(N)`` / ``varchar(N)`` widths.

The suite runs on SQLite, which ignores declared VARCHAR widths. PostgreSQL
rejects an over-long value with ``StringDataRightTruncation`` and the request
fails with a 500 -- the class of production-only failure behind PR #150 (ledger
keys) and PR #188 (Library idempotency keys composed from several ids).

Call :func:`assert_varchar_widths` at the end of an end-to-end flow: it walks
every Workspace-scoped mapped table in the test database and fails if any row
of the Workspace holds a value longer than its column allows.
"""
from __future__ import annotations

from sqlalchemy import String, func, inspect, or_, select

# Every module that declares mapped tables, so the metadata walk is complete
# even when the calling test imported only a few of them.
import app.adapters.persistence.models  # noqa: F401
import app.entitlements.cost_models  # noqa: F401
import app.entitlements.credentials  # noqa: F401
import app.entitlements.models  # noqa: F401
import app.entitlements.usage_models  # noqa: F401
import app.generation.models  # noqa: F401
import app.launch.models  # noqa: F401
import app.meta.models  # noqa: F401
import app.scale.models  # noqa: F401
from app.adapters.persistence.database import Base


def bounded_string_columns(table) -> list:
    return [
        column for column in table.columns
        if isinstance(column.type, String) and getattr(column.type, "length", None)
    ]


def varchar_width_violations(session, workspace_id: str | None = None) -> list[str]:
    """Every ``table.column`` value longer than its declared width.

    With ``workspace_id`` only that Workspace's rows are checked, and tables
    without a ``workspace_id`` column are skipped: the suite shares one
    database, and another test's fixture rows must not fail this one. Without
    it every table is checked whole.
    """
    existing = set(inspect(session.connection()).get_table_names())
    violations: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        columns = bounded_string_columns(table)
        if not columns:
            continue
        query = select(*columns).where(
            or_(*(func.length(column) > column.type.length for column in columns))
        )
        if workspace_id is not None:
            if "workspace_id" not in table.c:
                continue
            query = query.where(table.c.workspace_id == workspace_id)
        for row in session.execute(query):
            for column, value in zip(columns, row):
                if isinstance(value, str) and len(value) > column.type.length:
                    violations.append(
                        f"{table.name}.{column.name}: {len(value)} > varchar({column.type.length})"
                        f" -- {value[:80]!r}"
                    )
    return violations


def assert_varchar_widths(session, workspace_id: str | None = None) -> None:
    """Fail when a stored value would be rejected by PostgreSQL's varchar(N)."""
    violations = varchar_width_violations(session, workspace_id)
    assert not violations, (
        "values exceed their PostgreSQL column widths (SQLite does not enforce them):\n  "
        + "\n  ".join(violations)
    )
