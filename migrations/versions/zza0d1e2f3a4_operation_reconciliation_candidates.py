"""Let the Worker find Operations its idle reconcilers must settle (#22).

Revision ID: zza0d1e2f3a4
Revises: zz9c0d1e2f3a

The Worker settles two kinds of Operation when it is idle:

* ``jobs.reconcile_admitted_operations`` folds an aggregated (v1) Operation
  whose Jobs all ended inside ``marketai_claim_next_job_v2`` -- an epoch
  rejection or a lease recovery, with no Python transition to fold from;
* ``app.legacy_operations`` settles a legacy (v0) Operation whose Job ended
  without its handler ever running (``authorization_epoch_stale``,
  ``unknown_job_type``, or ``paid_call_outcome_unknown`` after a lease
  expiry).

Both start by listing candidates across Workspaces. ``marketai_domain_worker``
is NOBYPASSRLS and ``jobs``, ``operation_records`` and ``workspaces`` are
FORCE RLS behind a signed, current-epoch TenantContext, so an untenanted
session lists nothing: the v1 reconciler has never found a candidate on
PostgreSQL, and the v0 one could not either.

Two narrow SECURITY DEFINER functions answer only that question, the way
``marketai_claim_next_onboarding_fact`` and ``marketai_workspace_admission``
do. They are read-only and ``STABLE``, owned by the schema owner, pin
``search_path`` to ``''`` with every name schema-qualified, are EXECUTE-able
by ``marketai_domain_worker`` alone, cap the row count at 500, and return
ids plus the Workspace's current authorization epoch -- never a payload,
state or any other content. Every read and write that follows runs in a
TenantContext signed for that epoch, under the same RLS as any Worker
transaction. The grants are mirrored in
``deploy/postgres/domain-tenant-roles.sql``.

The legacy candidates' Job types come from the caller
(``app.legacy_operations.LEGACY_COMMANDS``) so the registry stays in one
place; the endings are fixed here because the claim function writes them.

No table changes. The previous release keeps running while this migrates and
simply never calls the functions; downgrade drops them, after which the
reconcilers list nothing on PostgreSQL again (the state before this
revision). SQLite has no RLS and lists candidates directly.
"""

from alembic import op


revision = "zza0d1e2f3a4"
down_revision = "zz9c0d1e2f3a"
branch_labels = None
depends_on = None

_LEGACY = "public.marketai_legacy_operation_candidates_v1(text[], integer)"
_ADMITTED = "public.marketai_admitted_operation_candidates_v1(integer)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_legacy_operation_candidates_v1(
            p_job_types text[],
            p_limit integer
        )
        RETURNS TABLE (
            workspace_id varchar,
            authorization_epoch integer,
            job_id varchar
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT job.workspace_id, workspace.authorization_epoch, job.id
              FROM public.jobs AS job
              JOIN public.workspaces AS workspace
                ON workspace.id = job.workspace_id
              LEFT JOIN public.operation_records AS operation
                ON operation.workspace_id = job.workspace_id
               AND operation.id = COALESCE(
                       job.operation_id, job.payload_json ->> 'operation_id'
                   )
             WHERE job.type = ANY (p_job_types)
               AND (
                    (job.state = 'failed'
                     AND job.error_code IN ('authorization_epoch_stale', 'unknown_job_type'))
                 OR (job.state = 'uncertain'
                     AND job.error_code = 'paid_call_outcome_unknown')
               )
               -- A Job that names its Operation is a candidate only while that
               -- Operation is open; one correlated through a domain row is
               -- re-checked by the caller under RLS.
               AND (operation.id IS NULL OR operation.state IN ('queued', 'running'))
             ORDER BY job.created_at DESC, job.id DESC
             LIMIT LEAST(GREATEST(COALESCE(p_limit, 0), 0), 500)
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LEGACY} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LEGACY} TO marketai_domain_worker")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.marketai_admitted_operation_candidates_v1(
            p_limit integer
        )
        RETURNS TABLE (
            workspace_id varchar,
            authorization_epoch integer,
            operation_id varchar
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
        AS $$
            SELECT operation.workspace_id, workspace.authorization_epoch, operation.id
              FROM public.operation_records AS operation
              JOIN public.workspaces AS workspace
                ON workspace.id = operation.workspace_id
             WHERE operation.admission_contract_version >= 1
               AND operation.state IN ('admitted', 'queued', 'running', 'uncertain')
               AND EXISTS (
                    SELECT 1 FROM public.jobs AS job
                     WHERE job.workspace_id = operation.workspace_id
                       AND job.operation_id = operation.id
               )
               -- Every Job has ended ...
               AND NOT EXISTS (
                    SELECT 1 FROM public.jobs AS job
                     WHERE job.workspace_id = operation.workspace_id
                       AND job.operation_id = operation.id
                       AND job.state NOT IN ('succeeded', 'failed', 'cancelled', 'uncertain')
               )
               -- ... and an uncertain Operation with an uncertain Job is
               -- already where its Jobs put it.
               AND NOT (
                    operation.state = 'uncertain'
                    AND EXISTS (
                        SELECT 1 FROM public.jobs AS job
                         WHERE job.workspace_id = operation.workspace_id
                           AND job.operation_id = operation.id
                           AND job.state = 'uncertain'
                    )
               )
             ORDER BY operation.updated_at, operation.id
             LIMIT LEAST(GREATEST(COALESCE(p_limit, 0), 0), 500)
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_ADMITTED} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ADMITTED} TO marketai_domain_worker")


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP FUNCTION IF EXISTS {_ADMITTED}")
    op.execute(f"DROP FUNCTION IF EXISTS {_LEGACY}")
