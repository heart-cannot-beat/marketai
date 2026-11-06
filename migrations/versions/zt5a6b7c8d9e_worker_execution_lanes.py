"""Isolate worker lanes and tenant-verified budget reads.

Existing Workers omit selectors and retain FIFO behavior. Keeping the v2
wrapper preserves the authenticated epoch and crash-recovery checks.
Budget admission reads only a signed-tenant aggregate, never raw ledger rows.
"""
from alembic import op
import sqlalchemy as sa

revision = 'zt5a6b7c8d9e'
down_revision = 'zs4f5a6b7c8d'
branch_labels = None
depends_on = None

ANCHOR = 'ORDER BY queued.created_at, queued.id'
FILTER = """AND (NULLIF(current_setting('marketai.job_types', true), '') IS NULL
                   OR current_setting('marketai.job_types', true) = 'null'
                   OR queued.type IN (SELECT json_array_elements_text(current_setting('marketai.job_types', true)::json)))
               AND (NULLIF(current_setting('marketai.excluded_job_types', true), '') IS NULL
                   OR queued.type NOT IN (SELECT json_array_elements_text(current_setting('marketai.excluded_job_types', true)::json)))
             """


def replace_dispatcher(before, after):
    definition = op.get_bind().scalar(sa.text(
        "SELECT pg_get_functiondef('public.marketai_claim_next_job_v2_legacy(text,integer)'::regprocedure)"))
    if definition.count(before) != 1:
        raise RuntimeError('Audited dispatcher selection changed; lane migration requires review')
    op.execute(sa.text(definition.replace(before, after)))


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        replace_dispatcher(ANCHOR, FILTER + ANCHOR)
        op.execute("CREATE INDEX ix_generation_recipe_marketing_project ON generation_recipe_snapshots "
            "(workspace_id, (((canonical_json -> 'selection'::text) -> 'marketing_intent'::text) ->> 'project_id'::text))")
        op.execute('''CREATE FUNCTION public.marketai_settled_cost_total(candidate_workspace text, since_at timestamptz)
            RETURNS numeric LANGUAGE plpgsql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public AS $$
            BEGIN
                IF public.marketai_context_is_current(candidate_workspace) IS DISTINCT FROM TRUE THEN
                    RAISE EXCEPTION 'A current authenticated workspace context is required' USING ERRCODE = '42501';
                END IF;
                RETURN (SELECT COALESCE(SUM(CASE WHEN entry.direction = 'decrease'
                    THEN -entry.budget_amount ELSE entry.budget_amount END), 0)
                    FROM public.provider_cost_ledger AS entry
                    WHERE entry.workspace_id = candidate_workspace
                      AND entry.entry_type IN ('reported_cost', 'estimated_settlement', 'adjustment')
                      AND (since_at IS NULL OR entry.created_at >= since_at));
            END $$''')
        op.execute('REVOKE ALL ON FUNCTION public.marketai_settled_cost_total(text,timestamptz) FROM PUBLIC')
        op.execute('GRANT EXECUTE ON FUNCTION public.marketai_settled_cost_total(text,timestamptz) TO marketai_domain_api, marketai_domain_worker')
    op.create_index('ix_jobs_lane_ready', 'jobs', ['type', 'state', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_jobs_lane_ready', table_name='jobs')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION public.marketai_settled_cost_total(text,timestamptz)')
        op.drop_index('ix_generation_recipe_marketing_project', table_name='generation_recipe_snapshots')
        replace_dispatcher(FILTER + ANCHOR, ANCHOR)
