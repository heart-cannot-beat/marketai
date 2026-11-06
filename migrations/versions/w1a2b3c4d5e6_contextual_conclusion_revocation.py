"""Revocation support on contextual_verdict_projections (Issue #49 Task 6).

Adds a nullable ``revoked_at`` column so
``app.generation.experiment_conclusions.project_contextual_conclusions``
(spec §0.7) can exclude a confirmed Winner/Learning/Loser conclusion once it
is revoked, without deleting the append-only projection row it revokes.
Nothing writes this column yet outside test fixtures -- the revocation
command itself is out of this task's scope -- but the read projection's
"non-revoked" filter needs somewhere to read from.

Revision ID: w1a2b3c4d5e6
Revises: v1a2b3c4d5e6
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "w1a2b3c4d5e6"
down_revision = "v1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contextual_verdict_projections", schema=None) as batch_op:
        batch_op.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("contextual_verdict_projections", schema=None) as batch_op:
        batch_op.drop_column("revoked_at")
