"""AI QC calls keep why a returned output was refused (#295); earlier calls have none."""
from alembic import op
import sqlalchemy as sa

revision = "zy0f1a2b3c4d"
down_revision = "zx9e0f1a2b3c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("marketing_ai_qc_calls") as batch:
        batch.add_column(sa.Column("error_detail", sa.JSON()))


def downgrade():
    with op.batch_alter_table("marketing_ai_qc_calls") as batch:
        batch.drop_column("error_detail")
