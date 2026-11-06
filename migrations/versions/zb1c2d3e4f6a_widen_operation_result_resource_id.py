"""Widen operation result resource identifiers for committed blob keys.

Revision ID: zb1c2d3e4f6a
Revises: za1b2c3d4e5f

Upload verification records the committed object key as its Operation result
resource identifier. Server-owned keys include the Workspace, purpose, date,
and random object name, so the original 100-character limit was too small.
"""

import sqlalchemy as sa
from alembic import op


revision = "zb1c2d3e4f6a"
down_revision = "za1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("operation_records", schema=None) as batch_op:
        batch_op.alter_column(
            "result_resource_id",
            existing_type=sa.String(length=100),
            type_=sa.String(length=1000),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("operation_records", schema=None) as batch_op:
        batch_op.alter_column(
            "result_resource_id",
            existing_type=sa.String(length=1000),
            type_=sa.String(length=100),
            existing_nullable=True,
        )
