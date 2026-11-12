"""Let the Worker list the Workspaces owed today's storage_bytes Job (#148).

Revision ID: zzl148a1b2c3
Revises: zzk145a1b2c3

Usage S5 records each Workspace's stored bytes once per UTC day
(``app.entitlements.storage_usage``). The Worker enqueues one
``usage.storage_bytes`` Job per Workspace, and that Job sums the Workspace's
byte columns and appends the fact in a TenantContext signed for the
Workspace, under RLS. Listing the Workspaces cannot run in a TenantContext:
``workspaces`` and ``jobs`` are FORCE RLS and the Worker does not yet know
which Workspaces there are.

``marketai_storage_usage_workspaces_v1(usage_date, limit)`` answers only that
question, the way ``marketai_admitted_operation_candidates_v1`` does for the
Operation reconciler. It returns the id and current ``authorization_epoch``
of each Workspace that is ``active``, not suspended, and has no Job keyed
``usage-storage-bytes:{usage_date}:{epoch}`` yet -- the key
``storage_usage_job_key`` builds -- in id order, at most ``limit`` (capped at
500) rows. It returns no other column and writes nothing: the Jobs are
enqueued by the Worker in each Workspace's own signed context. The epoch is
returned because that context must be signed for it, and it is in the key so
that a Job the claim rejected for a stale epoch does not hide the Workspace
for the rest of the day.

Like the other narrow entries it is ``STABLE``, owned by the schema owner,
pins ``search_path`` to ``''`` with every name schema-qualified, and is
EXECUTE-able by ``marketai_domain_worker`` alone. The Worker already holds
every table privilege the Job needs (SELECT on the five tables with byte
columns, INSERT on ``jobs``, ``audit_events`` and ``usage_events``), so no
table grant changes. The grant is mirrored in
``deploy/postgres/domain-tenant-roles.sql``; the release applies that file
before and after Alembic, so the grant here is an idempotent repeat on an
existing cluster.

The previous release keeps running while this migrates and never calls the
function. Downgrade drops it, after which the new Worker's dispatch fails
each interval (reported, never fatal) until the code is rolled back too; the
rows already written stay. SQLite has no RLS and no roles: there the Worker
lists the Workspaces directly.
"""

from alembic import op


revision = "zzl148a1b2c3"
down_revision = "zzk145a1b2c3"
branch_labels = None
depends_on = None

_FUNCTION = "public.marketai_storage_usage_workspaces_v1(date, integer)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_storage_usage_workspaces_v1(
            p_usage_date date,
            p_limit integer
        )
        RETURNS TABLE (
            workspace_id varchar,
            authorization_epoch integer
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT workspace.id, workspace.authorization_epoch
              FROM public.workspaces AS workspace
             WHERE workspace.lifecycle = 'active'
               AND NOT workspace.suspended
               AND NOT EXISTS (
                    SELECT 1 FROM public.jobs AS job
                     WHERE job.workspace_id = workspace.id
                       AND job.idempotency_key = 'usage-storage-bytes:'
                           || to_char(p_usage_date, 'YYYY-MM-DD')
                           || ':' || workspace.authorization_epoch
               )
             ORDER BY workspace.id
             LIMIT LEAST(GREATEST(COALESCE(p_limit, 0), 0), 500)
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION} TO marketai_domain_worker")


def downgrade() -> None:
    if _is_postgres():
        op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}")
