"""Bind the Meta observation clock to one immutable metric contract.

Revision ID: d884bc1b4a90
Revises: af7931e01923
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d884bc1b4a90"
down_revision = "af7931e01923"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("meta_accounts") as batch_op:
        batch_op.add_column(sa.Column("observation_contract_hash", sa.String(length=64), nullable=True))

    # Existing observation time is only valid for the newest completed contract.
    # Reset it to that sync's completion time instead of crediting time collected
    # under an older metric definition.
    op.execute("""
        UPDATE meta_accounts
        SET observation_contract_hash = (
                SELECT s.contract_hash
                FROM meta_sync_runs AS s
                WHERE s.account_id = meta_accounts.id AND s.state = 'completed'
                ORDER BY s.completed_at DESC
                LIMIT 1
            ),
            observation_started_at = (
                SELECT s.completed_at
                FROM meta_sync_runs AS s
                WHERE s.account_id = meta_accounts.id AND s.state = 'completed'
                ORDER BY s.completed_at DESC
                LIMIT 1
            )
        WHERE EXISTS (
            SELECT 1 FROM meta_sync_runs AS s
            WHERE s.account_id = meta_accounts.id AND s.state = 'completed'
        )
    """)


def downgrade() -> None:
    with op.batch_alter_table("meta_accounts") as batch_op:
        batch_op.drop_column("observation_contract_hash")
