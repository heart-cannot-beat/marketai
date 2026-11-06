"""Fix library_idempotency.updated_at nullable to match model.

The Gate 0.2 migration (r1a2b3c4d5e6) added ``updated_at`` as nullable
and backfilled all rows with ``created_at``, but never altered the
column to NOT NULL.  The model declares ``nullable=False``, so
``alembic check`` sees a drift.  This migration tightens the column.

Revision ID: s1a2b3c4d5e6
Revises: r1a2b3c4d5e6
Create Date: 2026-09-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s1a2b3c4d5e6"
down_revision = "r1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE library_idempotency "
            "SET updated_at = created_at "
            "WHERE updated_at IS NULL"
        )
    )

    with op.batch_alter_table("library_idempotency", schema=None) as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("library_idempotency", schema=None) as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
