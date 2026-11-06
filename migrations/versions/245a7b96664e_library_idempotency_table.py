"""library idempotency table

Revision ID: 245a7b96664e
Revises: df25fa1c9683
Create Date: 2026-08-29 23:49:38.402999
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '245a7b96664e'
down_revision: Union[str, Sequence[str], None] = 'df25fa1c9683'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RLS_UP = """
ALTER TABLE library_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE library_idempotency FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON library_idempotency
    USING (workspace_id = marketai_current_workspace());
"""

_RLS_DOWN = """
DROP POLICY IF EXISTS tenant_isolation ON library_idempotency;
ALTER TABLE library_idempotency DISABLE ROW LEVEL SECURITY;
"""


def upgrade() -> None:
    op.create_table('library_idempotency',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('command', sa.String(length=40), nullable=False),
    sa.Column('response_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_library_idempotency_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_library_idempotency')),
    sa.UniqueConstraint('workspace_id', 'idempotency_key', name='uq_library_idempotency_key')
    )
    with op.batch_alter_table('library_idempotency', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_library_idempotency_workspace_id'), ['workspace_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.execute(_RLS_UP)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_RLS_DOWN)
    with op.batch_alter_table('library_idempotency', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_library_idempotency_workspace_id'))

    op.drop_table('library_idempotency')
