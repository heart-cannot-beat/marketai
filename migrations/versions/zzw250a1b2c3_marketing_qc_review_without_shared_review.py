"""A test round's review record of a kept ad names no shared review (#250, ADR 0019, plan E5).

Revision ID: zzw250a1b2c3
Revises: zzv252a1b2c3

``marketing_qc_reviews`` is a round's own review of one image. Until now every record was written together with
a review on the Workspace's shared review chain (``quality_reviews``), which the creative library and earlier
tests read too, and named it in ``quality_review_id`` (NOT NULL, composite foreign key). The "after the checks"
command of a test project (#250) writes only the round's own record for each kept ad -- its checks, and the
evidence each passed check stands on -- and nothing on the shared chain, so that an earlier test's review of the
same image stays exactly as it was (design §5.5). Such a record has no shared review to name: the column
becomes nullable. The composite foreign key stays; a NULL ``quality_review_id`` names nothing (MATCH SIMPLE).

RLS, the ``tenant_isolation`` policy and the grants are unchanged: they are table-wide (API SELECT and INSERT;
Worker and readonly SELECT), and nobody updates or deletes a record.

The previous release keeps running while this migrates: it always writes the column and never reads it. Downgrade
refuses while a record without a shared review exists: putting NOT NULL back would need a shared review the
test never wrote. With none, it makes the column NOT NULL again.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzw250a1b2c3"
down_revision = "zzv252a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_qc_reviews"


def upgrade():
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("quality_review_id", existing_type=sa.String(64), nullable=True)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE quality_review_id IS NULL")).scalar():
        raise RuntimeError("test review records without a shared review exist; they cannot name one")
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("quality_review_id", existing_type=sa.String(64), nullable=False)
