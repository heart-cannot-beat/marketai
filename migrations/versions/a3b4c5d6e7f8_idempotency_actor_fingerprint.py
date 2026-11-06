"""idempotency actor and fingerprint binding

Revision ID: a3b4c5d6e7f8
Revises: 245a7b96664e
Create Date: 2026-08-30 00:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = '245a7b96664e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('library_idempotency', schema=None) as batch_op:
        batch_op.add_column(sa.Column('initiator_id', sa.String(length=128), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('request_fingerprint', sa.String(length=64), nullable=False, server_default=''))

    with op.batch_alter_table('library_events', schema=None) as batch_op:
        batch_op.alter_column('event_type', type_=sa.String(length=100))


def downgrade() -> None:
    # Preserve immutable history without lying about the schema revision.  A
    # downgrade is safe only when every stored value fits the previous width;
    # otherwise fail before Alembic stamps 245a7b... and require an operator to
    # resolve the incompatible rows explicitly.
    connection = op.get_bind()
    too_long = connection.scalar(
        sa.text("SELECT COUNT(*) FROM library_events WHERE length(event_type) > 40")
    )
    if int(too_long or 0):
        raise RuntimeError(
            "Cannot downgrade: library_events contains event_type values longer than 40 characters"
        )

    with op.batch_alter_table('library_events', schema=None) as batch_op:
        batch_op.alter_column(
            'event_type',
            existing_type=sa.String(length=100),
            type_=sa.String(length=40),
            existing_nullable=False,
        )

    with op.batch_alter_table('library_idempotency', schema=None) as batch_op:
        batch_op.drop_column('request_fingerprint')
        batch_op.drop_column('initiator_id')
