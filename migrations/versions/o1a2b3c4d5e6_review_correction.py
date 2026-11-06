"""Review correction model: supersedes_review_id and cleared enum value.

Adds ``quality_reviews.supersedes_review_id`` -- a nullable self-referencing
FK recording that this review corrects (clears) an earlier one -- plus a
unique constraint enforcing that a prior review can be superseded by at most
one later review (spec §6 -- review undo). The ``review_value`` CHECK
constraint is widened to accept the new ``cleared`` value alongside the
existing ``approved`` / ``rejected`` / ``needs_changes``.

The self-reference is a tenant-composite FK (``workspace_id``,
``supersedes_review_id``) -> (``workspace_id``, ``id``), not a naked
``id`` reference: PostgreSQL FK triggers run outside RLS, so a plain
``supersedes_review_id -> quality_reviews.id`` FK would let a row in one
Workspace reference (and probe the existence of) a row in another
(tests/test_postgres_rls.py::test_workspace_owned_foreign_keys_are_tenant_composite).
That requires ``quality_reviews`` to carry a
``UNIQUE (workspace_id, id)`` constraint as the FK's target, added here
alongside every other direct-tenant table.

``op.batch_alter_table`` is used throughout (SQLite has no
``ALTER ... ADD CONSTRAINT``; on PostgreSQL the same call issues a plain
in-place ``ALTER TABLE``), matching the convention set by
``n1_generation_assets_expand``.

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
"""
from alembic import op
import sqlalchemy as sa

revision = "o1a2b3c4d5e6"
down_revision = "n1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("quality_reviews", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("supersedes_review_id", sa.String(64), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_quality_reviews_workspace_id_id",
            ["workspace_id", "id"],
        )
        batch_op.create_unique_constraint(
            "uq_quality_reviews_supersedes",
            ["supersedes_review_id"],
        )
        batch_op.create_foreign_key(
            "fk_quality_reviews_supersedes_review_id_quality_reviews",
            "quality_reviews",
            ["workspace_id", "supersedes_review_id"],
            ["workspace_id", "id"],
        )
        batch_op.drop_constraint("ck_quality_review_review_value", type_="check")
        batch_op.create_check_constraint(
            "ck_quality_review_review_value",
            "review_value IS NULL OR review_value IN ('approved', 'rejected', "
            "'needs_changes', 'cleared')",
        )


def downgrade():
    with op.batch_alter_table("quality_reviews", schema=None) as batch_op:
        batch_op.drop_constraint("ck_quality_review_review_value", type_="check")
        batch_op.create_check_constraint(
            "ck_quality_review_review_value",
            "review_value IS NULL OR review_value IN ('approved', 'rejected', "
            "'needs_changes')",
        )
        batch_op.drop_constraint(
            "fk_quality_reviews_supersedes_review_id_quality_reviews",
            type_="foreignkey",
        )
        batch_op.drop_constraint("uq_quality_reviews_supersedes", type_="unique")
        batch_op.drop_constraint(
            "uq_quality_reviews_workspace_id_id", type_="unique"
        )
        batch_op.drop_column("supersedes_review_id")
