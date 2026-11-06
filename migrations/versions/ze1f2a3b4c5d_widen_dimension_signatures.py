"""Widen combination fingerprints and dimension signatures to unbounded text.

Revision ID: ze1f2a3b4c5d
Revises: zd1e2f3a4b5c

Template-sourced combinations are fingerprinted as
``<entry_id>:<dimension_id>=<value_id>`` joined per dimension
(``app.generation.preview._enumerate_combinations``). A Library entry id alone
is 36 characters, so a three-dimension template produces a fingerprint well
over the 64-character ``run_items.combination_fingerprint`` column and, with a
few more dimensions, over the 200-character signature columns. PostgreSQL
rejects the insert (``StringDataRightTruncation``), which turned Atomic
Submission of any template-driven Draft into a 500; SQLite never enforced the
width, so the test suite did not see it.

Widened here: ``run_items.combination_fingerprint`` /
``run_items.stable_dimension_signature`` (written by
``app.generation.submission``), ``assets.combo_id`` / ``assets.combo_code``
(the Worker copies the RunItem fingerprint into both), and
``experiment_version_candidate_concepts.dimension_key`` /
``stable_dimension_signature`` (projected from the same RunItem values by
``app.experiments.service``).
"""

import sqlalchemy as sa
from alembic import op


revision = "ze1f2a3b4c5d"
down_revision = "zd1e2f3a4b5c"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("run_items", "combination_fingerprint", 64, False, None),
    ("run_items", "stable_dimension_signature", 200, False, ""),
    ("assets", "combo_id", 64, False, None),
    ("assets", "combo_code", 200, False, None),
    ("experiment_version_candidate_concepts", "dimension_key", 200, False, None),
    ("experiment_version_candidate_concepts", "stable_dimension_signature", 200, False, None),
)


def upgrade() -> None:
    for table, column, width, nullable, server_default in _COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=width),
                type_=sa.Text(),
                existing_nullable=nullable,
                existing_server_default=server_default,
            )


def downgrade() -> None:
    for table, column, width, nullable, server_default in reversed(_COLUMNS):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.Text(),
                type_=sa.String(length=width),
                existing_nullable=nullable,
                existing_server_default=server_default,
            )
