"""Retention policy enforcement for operational artifacts.

Defines retention boundaries:
  - Application logs: 30 days (configurable)
  - Security / audit logs: 90 days (configurable)
  - Traces: 7 days (configurable)

Immutable audit events and financial evidence are protected: they cannot
be deleted by retention enforcement at any age.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from .. import config

PROTECTED_TABLES: frozenset[str] = frozenset({
    "audit_events",
    "cost_ledger",
    "launch_audit_log",
    "verdict_decisions",
    "library_audit",
})


class RetentionProtectionError(Exception):
    """Raised when a retention sweep attempts to delete protected evidence."""

    def __init__(self, table_name: str) -> None:
        self.table_name = table_name
        super().__init__(
            f"Retention enforcement cannot delete from protected table {table_name!r}"
        )


class RetentionCategory:
    """A category of operational data with a defined retention boundary."""

    def __init__(
        self,
        name: str,
        table_name: str,
        retention_days: int,
        timestamp_column: str = "created_at",
    ) -> None:
        self.name = name
        self.table_name = table_name
        self.retention_days = retention_days
        self.timestamp_column = timestamp_column


def default_categories() -> list[RetentionCategory]:
    return [
        RetentionCategory(
            "app_logs",
            "worker_logs",
            config.RETENTION_APP_LOG_DAYS,
        ),
        RetentionCategory(
            "security_logs",
            "security_logs",
            config.RETENTION_SECURITY_LOG_DAYS,
        ),
        RetentionCategory(
            "traces",
            "traces",
            config.RETENTION_TRACE_DAYS,
        ),
    ]


def enforce_retention(
    session: Any,
    categories: list[RetentionCategory] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete operational artifacts past their retention boundary.

    Returns {category_name: deleted_count}.
    Raises RetentionProtectionError if any category targets a protected table.
    """
    from .ops_alerts import AlertCategory, Severity, emit

    now = now or datetime.now(timezone.utc)
    categories = categories if categories is not None else default_categories()
    results: dict[str, int] = {}

    for cat in categories:
        if cat.table_name in PROTECTED_TABLES:
            raise RetentionProtectionError(cat.table_name)

        cutoff = now - timedelta(days=cat.retention_days)
        from sqlalchemy import text

        result = session.execute(
            text(
                f"DELETE FROM {cat.table_name} "
                f"WHERE {cat.timestamp_column} < :cutoff"
            ),
            {"cutoff": cutoff},
        )
        deleted = result.rowcount or 0
        results[cat.name] = deleted

        if deleted > 0:
            emit(
                Severity.INFO,
                AlertCategory.RETENTION,
                f"Retention sweep: deleted {deleted} rows from {cat.table_name}",
                details={
                    "category": cat.name,
                    "table": cat.table_name,
                    "retention_days": cat.retention_days,
                    "cutoff": cutoff.isoformat(),
                    "deleted": deleted,
                },
            )

    return results


def validate_no_protected_deletion(
    categories: list[RetentionCategory],
) -> None:
    """Pre-flight check that no category targets a protected table."""
    for cat in categories:
        if cat.table_name in PROTECTED_TABLES:
            raise RetentionProtectionError(cat.table_name)
