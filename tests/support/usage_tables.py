"""The tables a terminal ``transition_operation`` reads and writes (Issue #8 S2).

Settling an Operation records its Usage in the same transaction: it reads the
Operation's execution mode from ``operation_usage.MODE_EVIDENCE`` and appends
to ``usage_events``. A test that builds a scratch SQLite schema from a subset
of ``Base.metadata`` adds these tables so a terminal transition can commit.
"""
from __future__ import annotations

from sqlalchemy import Table


def operation_usage_tables() -> list[Table]:
    from app.entitlements.operation_usage import MODE_EVIDENCE, mapped_tables
    from app.entitlements.usage_models import UsageEventRecord

    tables = mapped_tables()
    names = {e.table for e in MODE_EVIDENCE} | {e.via_table for e in MODE_EVIDENCE if e.via_table}
    return [UsageEventRecord.__table__, *(tables[name] for name in sorted(names))]
