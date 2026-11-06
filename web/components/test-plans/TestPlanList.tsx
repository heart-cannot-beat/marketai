import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { FeatureEmptyState } from '@/components/workspace/FeatureEmptyState';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { formatDateTime } from '@/lib/i18n/format';
import type { TestPlanSummary } from '@/lib/test-plans/plan-model';
import { GenerationDefaultsBanner } from './GenerationDefaultsBanner';
import { LegacyTestsNotice } from './LegacyTestsNotice';

type TFunction = ReturnType<typeof useTranslation>['t'];

/** One status word per plan: where it is in draft → sample → full. */
export function planStatusLabel(plan: TestPlanSummary, t: TFunction): string {
  if (plan.status === 'drafting') return t('status-drafting');
  if (plan.status === 'failed') return t('status-failed');
  const stages = plan.run_stages ?? [];
  if (stages.includes('full')) return t('status-full');
  if (stages.includes('sample')) return t('status-sampled');
  return t('status-ready');
}

function formatDate(
  value: string | null | undefined,
  locale: string
): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return formatDateTime(locale, date, {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * `/w/{slug}/tests` (Issue #165): every test plan of the Workspace across
 * products, newest first, by product name, goal, scale and progress --
 * never by id. View-only since the legacy flow was frozen (#337); an empty
 * Workspace is pointed to the marketing test. The「生成默认值」editor lives
 * in Settings only.
 */
export function TestPlanList() {
  const { t, i18n } = useTranslation('tests');
  const { current } = useWorkspace();
  const slug = current.slug;
  const query = useDomainQuery('list_test_plans', {});
  const plans = ((query.data as { items?: TestPlanSummary[] } | undefined)
    ?.items ?? []) as TestPlanSummary[];

  return (
    <div className="space-y-6">
      <div className="max-w-prose">
        <h1 className="text-2xl font-bold outline-none">{t('list-title')}</h1>
        <p className="mt-1 text-sm text-base-content/70">{t('list-intro')}</p>
      </div>

      <LegacyTestsNotice />

      <GenerationDefaultsBanner />

      {query.isLoading && plans.length === 0 && (
        <div className="flex justify-center py-12" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
          <span className="sr-only">{t('list-loading')}</span>
        </div>
      )}

      {!query.isLoading && query.problem && plans.length === 0 && (
        <div className="alert alert-error" role="alert">
          <span>{t('list-error')}</span>
          <button type="button" className="btn btn-sm" onClick={query.refresh}>
            {t('retry')}
          </button>
        </div>
      )}

      {!query.isLoading && !query.problem && plans.length === 0 && (
        <FeatureEmptyState
          title={t('list-empty-title')}
          description={t('list-empty-description')}
          action={
            <Link
              href={`/w/${slug}/experiments`}
              className="btn btn-primary btn-sm mt-2"
            >
              {t('list-empty-action')}
            </Link>
          }
        />
      )}

      {plans.length > 0 && (
        <ul className="divide-y divide-base-300 rounded-box border border-base-300 bg-base-100">
          {plans.map((plan) => {
            const name = plan.product_name?.trim() || t('unnamed-product');
            const created = formatDate(plan.created_at, i18n.language);
            return (
              <li key={plan.plan_id} data-testid="test-plan-row">
                <Link
                  href={`/w/${slug}/tests/${plan.plan_id}`}
                  className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 hover:bg-base-200/60 focus-visible:bg-base-200/60"
                  aria-label={t('list-open', { name })}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{name}</span>
                    <span className="mt-0.5 flex flex-wrap gap-x-3 text-sm text-base-content/60">
                      <span>
                        {t(`goal-${plan.goal}`, { defaultValue: plan.goal })}
                      </span>
                      <span>
                        {t(`scale-${plan.scale}`, { defaultValue: plan.scale })}
                      </span>
                      <span>
                        {t('list-directions', { count: plan.direction_count })}
                      </span>
                      {created && (
                        <span>{t('list-created', { date: created })}</span>
                      )}
                    </span>
                  </span>
                  <span className="badge badge-outline">
                    {planStatusLabel(plan, t)}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
