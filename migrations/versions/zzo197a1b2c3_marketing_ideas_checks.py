"""Ads-first ideas checks: single-image checks without a round (#197, ADR 0018).

Revision ID: zzo197a1b2c3
Revises: zzn193a1b2c3

An ads-first ad has no round, so its automatic check hangs on the ideas (``marketing_projects``) instead.
The existing ``marketing_ai_qc_calls`` table keeps both kinds of call: ``round_id`` becomes nullable, the
new ``project_id`` names the ideas, and a check says exactly one of the two is set. An ideas check is a
single-image check only (``kind = 'single'``); comparing images waits for a frozen round.

The table already enforces RLS with the ``tenant_isolation`` policy and its grants are table-wide (API
SELECT and INSERT, Worker SELECT and UPDATE, readonly SELECT), so the new column is covered by both.
``project_id`` is bound to its project by a composite ``(workspace_id, project_id)`` foreign key, so a
check can never name another Workspace's ideas. Round queries filter on ``round_id`` and never see an
ideas check.

One privilege is new: a first batch admits the checks of the images it made in the Worker (maintainer
decision on #197; the cost contract §11 lists the steps), so the Worker gains INSERT on this table.
It still cannot delete a call, and forced RLS keeps every row in the Job's Workspace. The grant is
mirrored in ``deploy/postgres/domain-tenant-roles.sql``.

The previous release keeps running while this migrates: it never writes ``project_id`` and every row it
writes names a round. Downgrade refuses while any ideas check exists: dropping them would lose paid
results, and ``round_id`` could not become NOT NULL again. It revokes the Worker's INSERT.
"""
from alembic import op
import sqlalchemy as sa

revision = "zzo197a1b2c3"
down_revision = "zzn193a1b2c3"
branch_labels = None
depends_on = None

TABLE = "marketing_ai_qc_calls"


def upgrade():
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("round_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("project_id", sa.String(64)))
        batch.create_foreign_key(op.f("fk_marketing_ai_qc_calls_workspace_id_marketing_projects"), "marketing_projects",
                                 ["workspace_id", "project_id"], ["workspace_id", "id"], ondelete="RESTRICT")
        batch.create_unique_constraint("uq_marketing_ai_qc_project_request",
                                       ["workspace_id", "project_id", "asset_version_id", "request_hash"])
        batch.create_check_constraint("ai_qc_subject", "(round_id IS NULL) <> (project_id IS NULL)")
        batch.create_check_constraint("ai_qc_project_single", "project_id IS NULL OR kind = 'single'")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'GRANT INSERT ON TABLE "{TABLE}" TO marketai_domain_worker')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # The table FORCEs RLS: without this a role that cannot bypass it counts no rows and the guard passes.
        bind.execute(sa.text("SELECT set_config('row_security', 'off', true)"))
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE project_id IS NOT NULL")).scalar():
        raise RuntimeError("ideas checks exist; dropping them would lose paid check results")
    if bind.dialect.name == "postgresql":
        op.execute(f'REVOKE INSERT ON TABLE "{TABLE}" FROM marketai_domain_worker')
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(op.f("ck_marketing_ai_qc_calls_ai_qc_project_single"), type_="check")
        batch.drop_constraint(op.f("ck_marketing_ai_qc_calls_ai_qc_subject"), type_="check")
        batch.drop_constraint("uq_marketing_ai_qc_project_request", type_="unique")
        batch.drop_constraint(op.f("fk_marketing_ai_qc_calls_workspace_id_marketing_projects"), type_="foreignkey")
        batch.drop_column("project_id")
        batch.alter_column("round_id", existing_type=sa.String(64), nullable=False)
