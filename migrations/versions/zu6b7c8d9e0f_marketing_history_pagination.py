"""Index stable, tenant-scoped marketing history keyset pagination."""
from alembic import op

revision = 'zu6b7c8d9e0f'
down_revision = 'zt5a6b7c8d9e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_marketing_projects_history', 'marketing_projects', ['workspace_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_marketing_projects_history', table_name='marketing_projects')
