"""Test projects name the ads-first ideas they came from (#247, ADR 0019, step 2 design §5.1).

Revision ID: zzs247a1b2c3
Revises: zzs241a1b2c3

Taking kept ads to a test opens a new marketing project with ``flow = "ads_test"`` under the same
product (a "test project"). ``marketing_projects`` gains ``source_project_id``: the ads-first ideas
project a test project came from, bound in the same Workspace by a Workspace-composite foreign key to
``marketing_projects`` itself and indexed by ``(workspace_id, source_project_id)``. It is null for
every existing row, plan-first and ads-first alike; a CHECK makes it required for a test project.

``marketing_projects`` already forces RLS and grants SELECT, INSERT and UPDATE to the API (and SELECT
and UPDATE to the Worker, SELECT to readonly) on the whole table, so the new column needs no grant.

The previous release keeps running while this migrates: it never reads or writes the new column, and no
test project exists until "take to test" (#249) ships. Downgrade refuses while any ``ads_test`` project
exists, because dropping the column would lose which ideas each test came from; with none, it drops the
check, the index, the foreign key and the column.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzs247a1b2c3"
down_revision = "zzs241a1b2c3"
branch_labels = None
depends_on = None

FOREIGN_KEY = "fk_marketing_project_source"
INDEX = "ix_marketing_projects_source"
CHECK = "ck_marketing_projects_test_project_source"


def upgrade():
    with op.batch_alter_table("marketing_projects") as batch:
        batch.add_column(sa.Column("source_project_id", sa.String(64), nullable=True))
        batch.create_foreign_key(FOREIGN_KEY, "marketing_projects", ["workspace_id", "source_project_id"],
                                 ["workspace_id", "id"], ondelete="RESTRICT")
        batch.create_index(INDEX, ["workspace_id", "source_project_id"])
        batch.create_check_constraint(op.f(CHECK), "flow <> 'ads_test' OR source_project_id IS NOT NULL")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text("SELECT COUNT(*) FROM marketing_projects WHERE flow = 'ads_test'")).scalar():
        raise RuntimeError("ads_test projects exist; dropping source_project_id would lose which ideas each test came from")
    with op.batch_alter_table("marketing_projects") as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.drop_index(INDEX)
        batch.drop_constraint(FOREIGN_KEY, type_="foreignkey")
        batch.drop_column("source_project_id")
