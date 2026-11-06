"""Recovery preview columns and same_snapshot_resumable.

Makes resolution_previews.generation_draft_id and draft_revision nullable
(recovery previews have no draft). Adds recovery_cost_currency. Adds
bidirectional CHECK constraints enforcing field presence by purpose.
Adds run_items.same_snapshot_resumable.

Revision ID: q1a2b3c4d5e6
Revises: p1a2b3c4d5e6
"""
from alembic import op
import sqlalchemy as sa

revision = "q1a2b3c4d5e6"
down_revision = "p1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    # -- resolution_previews --
    with op.batch_alter_table("resolution_previews", schema=None) as batch_op:
        # Make draft columns nullable for run_recovery purpose
        batch_op.alter_column("generation_draft_id", existing_type=sa.String(64),
                              nullable=True)
        batch_op.alter_column("draft_revision", existing_type=sa.Integer(),
                              nullable=True)
        # Add recovery cost currency
        batch_op.add_column(
            sa.Column("recovery_cost_currency", sa.String(3), nullable=True)
        )
        # Bidirectional CHECK: initial_generation requires draft, forbids recovery
        batch_op.create_check_constraint(
            "ck_resolution_preview_initial_fields",
            "purpose != 'initial_generation' OR ("
            "generation_draft_id IS NOT NULL AND draft_revision IS NOT NULL "
            "AND recovery_run_id IS NULL AND recovery_command_kind IS NULL"
            ")",
        )
        # Bidirectional CHECK: run_recovery requires recovery fields, forbids draft
        batch_op.create_check_constraint(
            "ck_resolution_preview_recovery_fields",
            "purpose != 'run_recovery' OR ("
            "recovery_run_id IS NOT NULL AND recovery_command_kind IS NOT NULL "
            "AND recovery_max_additional_cost IS NOT NULL "
            "AND recovery_cost_currency IS NOT NULL "
            "AND generation_draft_id IS NULL AND draft_revision IS NULL"
            ")",
        )
        # Currency format: 3 uppercase letters
        batch_op.create_check_constraint(
            "ck_resolution_preview_recovery_ccy_fmt",
            "recovery_cost_currency IS NULL OR ("
            "length(recovery_cost_currency) = 3 "
            "AND substr(recovery_cost_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(recovery_cost_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(recovery_cost_currency, 3, 1) BETWEEN 'A' AND 'Z'"
            ")",
        )
        # Cost non-negative
        batch_op.create_check_constraint(
            "ck_resolution_preview_recovery_cost_nn",
            "recovery_max_additional_cost IS NULL OR "
            "recovery_max_additional_cost >= 0",
        )

    # -- run_items --
    with op.batch_alter_table("run_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("same_snapshot_resumable", sa.Boolean(),
                       nullable=False, server_default=sa.false())
        )


def downgrade():
    with op.batch_alter_table("run_items", schema=None) as batch_op:
        batch_op.drop_column("same_snapshot_resumable")

    with op.batch_alter_table("resolution_previews", schema=None) as batch_op:
        batch_op.drop_constraint("ck_resolution_preview_recovery_cost_nn",
                                 type_="check")
        batch_op.drop_constraint("ck_resolution_preview_recovery_ccy_fmt",
                                 type_="check")
        batch_op.drop_constraint("ck_resolution_preview_recovery_fields",
                                 type_="check")
        batch_op.drop_constraint("ck_resolution_preview_initial_fields",
                                 type_="check")
        batch_op.drop_column("recovery_cost_currency")
        batch_op.alter_column("draft_revision", existing_type=sa.Integer(),
                              nullable=False)
        batch_op.alter_column("generation_draft_id", existing_type=sa.String(64),
                              nullable=False)
