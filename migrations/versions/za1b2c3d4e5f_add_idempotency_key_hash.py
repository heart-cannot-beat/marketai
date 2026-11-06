"""Add idempotency_key_hash to provider_call_attempts.

Revision ID: za1b2c3d4e5f
Revises: z1a2b3c4d5e6

A SHA-256 hash of the idempotency key, persisted in Phase 1 before
external dispatch. Recovery code queries by this indexed column to find
the original attempt and reconcile after SIGKILL without re-dispatching.
"""

import sqlalchemy as sa
from alembic import op


revision = "za1b2c3d4e5f"
down_revision = "ad2_target_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_call_attempts",
        sa.Column("idempotency_key_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_provider_call_attempts_idempotency_key_hash",
        "provider_call_attempts",
        ["idempotency_key_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_call_attempts_idempotency_key_hash",
        table_name="provider_call_attempts",
    )
    op.drop_column("provider_call_attempts", "idempotency_key_hash")
