"""Health readiness stats function.

A SECURITY DEFINER function owned by domain_owner (BYPASSRLS) that
returns cross-tenant aggregate health stats.  Runtime roles call it
through GRANT EXECUTE -- they never see individual rows.

Revision ID: p1a2b3c4d5e6
Revises: o1a2b3c4d5e6
"""
from alembic import op


revision = "p1a2b3c4d5e6"
down_revision = "o1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("""
        CREATE OR REPLACE FUNCTION marketai_health_stats_v1()
        RETURNS jsonb
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT jsonb_build_object(
                'job_queue_depth',
                (SELECT count(*) FROM public.jobs
                 WHERE state IN ('queued', 'retry_wait')),
                'uncertain_jobs',
                (SELECT count(*) FROM public.jobs
                 WHERE state = 'uncertain'),
                'running_jobs',
                (SELECT count(*) FROM public.jobs
                 WHERE state IN ('leased', 'running')),
                'oldest_heartbeat_age_seconds',
                COALESCE(
                    (SELECT EXTRACT(EPOCH FROM (now() - MIN(heartbeat_at)))
                     FROM public.jobs
                     WHERE state IN ('leased', 'running')
                       AND heartbeat_at IS NOT NULL),
                    0
                ),
                'stale_operations',
                (SELECT count(*) FROM public.operation_records
                 WHERE state NOT IN (
                     'succeeded', 'failed', 'partially_succeeded', 'cancelled'
                 )
                 AND updated_at < now() - interval '1 hour')
            )
        $$
    """)

    for role in ("marketai_domain_api", "marketai_domain_worker"):
        op.execute(
            f"GRANT EXECUTE ON FUNCTION marketai_health_stats_v1() TO {role}"
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP FUNCTION IF EXISTS marketai_health_stats_v1()")
