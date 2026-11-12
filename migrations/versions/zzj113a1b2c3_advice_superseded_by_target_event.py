"""An Advice thread may be superseded by a Target event (#113, #101 S7).

Revision ID: zzj113a1b2c3
Revises: zzi132a1b2c3

Contract §15.2 and §18: when the Target changes, the Advice formed under
the old one no longer applies and is superseded. ``ck_sate_superseded_shape``
required every ``superseded`` thread event to name a successor thread, so
Advice could be superseded only when the same evaluation reopened a thread
of the same type. A withdrawn Target leaves no economic Advice to succeed
it, and a published one is often re-evaluated much later or not at all
(evidence older than the Policy's freshness lag waits for the next
scheduled evaluation).

Upgrade, on ``scale_advice_thread_events``:

* adds ``semantic_event_id``, the ``scale_target_lifecycle_events`` row
  (publish or withdraw) that made the thread inapplicable;
* replaces ``ck_sate_superseded_shape``: a ``superseded`` event names a
  successor thread, the semantic event, or both.

The column is nullable and every existing row satisfies the new check. No
grant changes: the Worker already inserts thread events, and the table's
append-only trigger is untouched. On PostgreSQL the table stays under RLS
and ``FORCE ROW LEVEL SECURITY``; adding a nullable column rewrites nothing.

Downgrade restores the old check and drops the column, so a release can
always be rolled back. While a superseded event names no successor, the
old check cannot hold for it: PostgreSQL gets it back ``NOT VALID`` (new
rows only), SQLite goes without it, the column stays (the history is
append-only), and the migration log says how many rows are the reason.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op


revision = "zzj113a1b2c3"
down_revision = "zzi132a1b2c3"
branch_labels = None
depends_on = None

_log = logging.getLogger("alembic.runtime.migration")

_TABLE = "scale_advice_thread_events"
_COLUMN = "semantic_event_id"
#: The short logical name; batch operations apply the ``ck_%(table_name)s_``
#: naming convention (see ``zzh129a1b2c3``).
_CHECK = "ck_sate_superseded_shape"
_CHECK_FULL_NAME = f"ck_{_TABLE}_{_CHECK}"
_SHAPE_BEFORE = "(event_type != 'superseded') OR (successor_thread_id IS NOT NULL)"
_SHAPE_AFTER = (
    "(event_type != 'superseded') OR (successor_thread_id IS NOT NULL) "
    "OR (semantic_event_id IS NOT NULL)"
)


def _column_names() -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if _COLUMN not in _column_names():
            batch_op.add_column(sa.Column(_COLUMN, sa.String(length=64), nullable=True))
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, _SHAPE_AFTER)


def downgrade() -> None:
    conn = op.get_bind()
    postgres = conn.dialect.name == "postgresql"
    if postgres:
        conn.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    successorless = conn.execute(sa.text(
        f"SELECT count(*) FROM {_TABLE} "
        "WHERE event_type = 'superseded' AND successor_thread_id IS NULL"
    )).scalar_one()
    if not successorless:
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.drop_constraint(_CHECK, type_="check")
            batch_op.create_check_constraint(_CHECK, _SHAPE_BEFORE)
            batch_op.drop_column(_COLUMN)
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
    if postgres:
        op.execute(
            f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT "{_CHECK_FULL_NAME}" '
            f"CHECK ({_SHAPE_BEFORE}) NOT VALID"
        )
    _log.warning(
        "%d Advice thread event(s) were superseded by a Target event with no successor thread; "
        "%s; %s is kept", successorless,
        "ck_sate_superseded_shape is back NOT VALID (new rows only)" if postgres
        else "ck_sate_superseded_shape stays off while they exist",
        _COLUMN,
    )
