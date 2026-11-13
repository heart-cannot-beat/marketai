"""Let the Worker open the next Scale Episode when it rolls one over (Issue #92).

Revision ID: zzb1e2f3a4b5
Revises: zza0d1e2f3a4

A Scale evaluation whose sealed snapshot carries another delivery
comparability hash than the current Episode's (the delivery configuration
changed, spec §3.3 step 3) closes that Episode and opens the next one in the
same Phase A transaction (``app.scale.service._apply_episode_rollover``;
Scale contract §1 item 4 and §16.2). Evaluation runs only in the Worker
(``scale_evaluate`` -> ``run_scale_evaluation``), and ``f5eeeab4e9b9`` gave
the Worker UPDATE but no INSERT on ``scale_episodes``, so on PostgreSQL every
rollover failed with ``permission denied for table scale_episodes`` and
rolled the whole transaction back.

* The Worker gains INSERT on ``scale_episodes`` (API: SELECT, INSERT, UPDATE;
  Worker: SELECT, INSERT, UPDATE; Readonly: SELECT; nobody DELETE). Forced
  RLS still scopes every write to the caller's Workspace and Meta mode. The
  grant is mirrored in ``deploy/postgres/domain-tenant-roles.sql``.
* ``f5eeeab4e9b9`` is history and stays as it is.

No schema change, so the previous release keeps running while this migrates.
GRANT is idempotent: the Release applies the roles script before and after
Alembic, so on an existing cluster this repeats a grant the script may already
have made. SQLite checks no grants.
"""

from alembic import op


revision = "zzb1e2f3a4b5"
down_revision = "zza0d1e2f3a4"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute("GRANT INSERT ON TABLE scale_episodes TO marketai_domain_worker")


def downgrade() -> None:
    if _is_postgres():
        op.execute("REVOKE INSERT ON TABLE scale_episodes FROM marketai_domain_worker")
