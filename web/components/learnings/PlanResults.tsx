import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { RoundView } from './RoundView';

type TestPlanResults = components['schemas']['TestPlanResults'];

/**
 * A test plan's results (Issue #166): its directions and factor values with
 * metrics. Read-only since the legacy flow was frozen (#337): the plan page
 * no longer imports results or offers 「按结论生成下一轮」. Rendered by the
 * plan page's `PlanNextRoundSlot`.
 */
export function PlanResults({ planId }: { planId: string }) {
  const { t } = useTranslation('learnings');
  const query = useDomainQuery('get_test_plan_results', { plan_id: planId });
  const results = query.data as TestPlanResults | undefined;
  if (!results) {
    return query.problem ? (
      <div className="alert alert-error" role="alert">
        <span>{t('load-error')}</span>
      </div>
    ) : null;
  }
  return (
    <section
      className="space-y-4 border-t border-base-300 pt-4"
      aria-label={t('plan-results-title')}
      data-testid="plan-results"
    >
      <h3 className="text-base font-semibold">{t('plan-results-title')}</h3>
      {results.evidence_notice && (
        <div className="alert alert-warning py-2 text-sm" role="note">
          <span>{results.evidence_notice}</span>
        </div>
      )}
      {results.round ? (
        <RoundView round={results.round} />
      ) : (
        <p className="max-w-prose text-sm text-base-content/70">
          {t('plan-results-empty')}
        </p>
      )}
    </section>
  );
}
