"""Split AI QC into single-image and paired checks; earlier one-time calls keep a NULL kind."""
from alembic import op
import sqlalchemy as sa

revision = "zx9e0f1a2b3c"
down_revision = "zw8d9e0f1a2b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("marketing_ai_qc_calls") as batch:
        batch.add_column(sa.Column("kind", sa.String(16)))
        batch.create_check_constraint("ai_qc_kind", "kind IS NULL OR kind IN ('single', 'paired')")


def downgrade():
    with op.batch_alter_table("marketing_ai_qc_calls") as batch:
        batch.drop_constraint(op.f("ck_marketing_ai_qc_calls_ai_qc_kind"), type_="check")
        batch.drop_column("kind")
