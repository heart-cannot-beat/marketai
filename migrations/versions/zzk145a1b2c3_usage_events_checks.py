"""``usage_events`` gets the CHECK constraints of spec §2.12 (#145, #8 S3).

Revision ID: zzk145a1b2c3
Revises: zzj113a1b2c3

``k1a2b3c4d5e6`` created ``usage_events`` with only its unique key, so the
metric registry (``app/entitlements/usage.py``, ``METRICS``) was the only
thing that kept a row's shape legal. Writers started on 2026-11-08 (#69,
``provider_calls``) and 2026-11-09 (#87, four more metrics); every row they
write goes through ``UsageRecorder.record_many`` and satisfies all four
constraints below.

Upgrade adds, on ``usage_events``:

* ``ck_usage_events_measurement_kind_valid``: ``delta`` or ``gauge``;
* ``ck_usage_events_direction_matches_kind``: a ``delta`` goes ``increase``
  or ``decrease``, a ``gauge`` is ``set``. A kind that is neither is left to
  the check above, so every bad row is refused by the one constraint it
  breaks (PostgreSQL tests CHECKs in name order and reports the first);
* ``ck_usage_events_quantity_nonnegative``: ``quantity >= 0``;
* ``ck_usage_events_execution_mode_valid``: ``byok``, ``sandbox``, ``mock``
  or ``not_applicable``.

Units and which metric may use which kind stay in the registry, not here.

PostgreSQL adds each constraint ``NOT VALID`` and then validates it, so a
row written before the upgrade that breaks one fails the release instead of
being let through; the whole revision then rolls back. Alembic runs the
revision in one transaction (``migrations/env.py``): the ``ACCESS
EXCLUSIVE`` lock of ``ADD CONSTRAINT`` is held until the commit, so writers
wait for the validating scans too; the table holds a few days of Beta usage.
``ON CONFLICT DO NOTHING``, the shape ``record_many`` sends, skips unique
violations only, so a CHECK violation still raises and is never counted as a
duplicate. The append-only trigger ``trg_usage_events_immutable``, RLS and
every grant are untouched: none of them is part of the constraint DDL.

SQLite rebuilds the table in batch mode. SQLite DDL is not transactional
here, so a row that breaks a check is counted first and refuses the upgrade
before the rebuild starts. ``usage_events`` has no trigger there
(``k1a2b3c4d5e6`` installs the append-only trigger on PostgreSQL only), but
any trigger the table has is put back after the rebuild, which would
otherwise drop it.

Downgrade drops the four constraints and nothing else; the rows stay.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "zzk145a1b2c3"
down_revision = "zzj113a1b2c3"
branch_labels = None
depends_on = None

_TABLE = "usage_events"

#: (logical name, predicate). Batch operations apply the
#: ``ck_%(table_name)s_%(constraint_name)s`` naming convention; PostgreSQL
#: DDL spells the full name (``_full_name``).
_CHECKS = (
    ("measurement_kind_valid", "measurement_kind IN ('delta', 'gauge')"),
    (
        "direction_matches_kind",
        "(measurement_kind <> 'delta' OR direction IN ('increase', 'decrease')) "
        "AND (measurement_kind <> 'gauge' OR direction = 'set')",
    ),
    ("quantity_nonnegative", "quantity >= 0"),
    (
        "execution_mode_valid",
        "execution_mode IN ('byok', 'sandbox', 'mock', 'not_applicable')",
    ),
)


def _full_name(name: str) -> str:
    return f"ck_{_TABLE}_{name}"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _sqlite_triggers() -> list[tuple[str, str]]:
    return [
        (row.name, row.sql)
        for row in op.get_bind().execute(sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = :table ORDER BY name"
        ), {"table": _TABLE})
    ]


def _rebuild_sqlite(change) -> None:
    """Rebuild ``usage_events`` in batch mode and put its triggers back."""
    triggers = _sqlite_triggers()
    with op.batch_alter_table(_TABLE, schema=None, recreate="always") as batch_op:
        change(batch_op)
    remaining = {name for name, _ in _sqlite_triggers()}
    for name, sql in triggers:
        if name not in remaining:
            op.execute(sql)


def _refuse_rows_that_break_a_check() -> None:
    conn = op.get_bind()
    broken = {
        _full_name(name): conn.execute(
            sa.text(f'SELECT count(*) FROM "{_TABLE}" WHERE NOT ({predicate})')
        ).scalar_one()
        for name, predicate in _CHECKS
    }
    broken = {name: count for name, count in broken.items() if count}
    if broken:
        raise RuntimeError(
            f"{_TABLE} has rows that break its new checks, nothing was changed: "
            + ", ".join(f"{name}: {count}" for name, count in broken.items())
        )


def upgrade() -> None:
    if _is_postgres():
        for name, predicate in _CHECKS:
            op.execute(
                f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT "{_full_name(name)}" '
                f"CHECK ({predicate}) NOT VALID"
            )
        for name, _ in _CHECKS:
            op.execute(f'ALTER TABLE "{_TABLE}" VALIDATE CONSTRAINT "{_full_name(name)}"')
        return

    _refuse_rows_that_break_a_check()

    def add(batch_op) -> None:
        for name, predicate in _CHECKS:
            batch_op.create_check_constraint(name, predicate)

    _rebuild_sqlite(add)


def downgrade() -> None:
    if _is_postgres():
        for name, _ in reversed(_CHECKS):
            op.execute(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT IF EXISTS "{_full_name(name)}"')
        return

    def drop(batch_op) -> None:
        for name, _ in reversed(_CHECKS):
            batch_op.drop_constraint(name, type_="check")

    _rebuild_sqlite(drop)
