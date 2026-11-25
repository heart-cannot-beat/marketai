import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { AdThumbnail } from '@/components/ideas/AdCard';
import { PRIMARY } from '@/components/ideas/PaidImageAction';
import type { TestInProgress } from '@/hooks/tests/useTestInProgress';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { formatDateTime } from '@/lib/i18n/format';
import { dayLabel } from '@/lib/tests/take-to-test';
import type { TestResult as Result } from '@/lib/tests/test-in-progress';

const STATUS_STYLE: Record<Result['comparisons'][number]['status'], string> = {
  meets_goal:
    'bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100',
  misses_goal: 'bg-red-100 text-red-900 dark:bg-red-900 dark:text-red-100',
  not_clear: 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100',
};

/**
 * The round's minimal result (design §5.9, #257). Before the result day the
 * page says when it can be read. From then on "See the result" (free)
 * evaluates the round and shows each compared group: meets the goal, misses
 * it, or cannot tell yet and how many orders it is short -- for these ads
 * only. The one-line verdict and the next step are step 3's.
 */
export function TestResult({ run }: { run: TestInProgress }) {
  const { t, i18n } = useTranslation('tests');
  const id = useId();
  const result = run.result!;
  const [problem, setProblem] = useState<ProblemDetails | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const read = Boolean(result.evaluation_id);
  const headlines = new Map(
    (run.round?.handoff?.entries ?? []).map((entry) => [
      entry.asset_version_id,
      entry.headline,
    ])
  );

  async function see() {
    const outcome = await run.seeResult();
    setUncertain(outcome.kind === 'uncertain');
    setProblem(outcome.kind === 'refused' ? outcome.problem : null);
  }

  return (
    <section
      className="space-y-4"
      aria-labelledby={`${id}-title`}
      data-testid="test-result"
    >
      <h3 id={`${id}-title`} className="text-lg font-semibold">
        {t('run-result-title')}
      </h3>
      {!result.mature ? (
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('run-result-not-yet', {
            day: dayLabel(i18n.language, result.schedule.result_date),
          })}
        </p>
      ) : (
        <>
          {(!read || !result.current) && (
            <div className="space-y-2">
              <p className="text-sm">
                {read ? t('run-result-outdated') : t('run-result-due')}
              </p>
              <button
                type="button"
                className={classNames(PRIMARY, 'w-full sm:w-auto')}
                disabled={run.busy}
                onClick={() => void see()}
                data-testid="see-result"
              >
                {read ? t('run-result-see-again') : t('run-result-see')}
              </button>
              <p className="text-xs text-gray-600 dark:text-gray-400">
                {t('run-result-free')}
              </p>
            </div>
          )}
        </>
      )}

      {read && (
        <div className="space-y-3" data-testid="result-comparisons">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('run-result-scope')}
            {result.schedule.ended_early &&
              ` ${t('run-result-ended-early', {
                day: dayLabel(
                  i18n.language,
                  result.schedule.effective_end_date
                ),
              })}`}
          </p>
          <ul role="list" className="space-y-3">
            {result.comparisons.map((comparison) => (
              <li
                key={comparison.id}
                className="flex flex-col gap-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700 sm:flex-row sm:items-center"
              >
                <div className="flex shrink-0 gap-1">
                  {comparison.asset_version_ids.map((version) => (
                    <AdThumbnail
                      key={version}
                      versionId={version}
                      alt={headlines.get(version) ?? comparison.label}
                      size="xs"
                    />
                  ))}
                </div>
                <div className="min-w-0 flex-1 space-y-1 text-sm">
                  <p className="break-words font-semibold">
                    {comparison.label}
                  </p>
                  <p>
                    {t('run-result-orders', {
                      count: Number(comparison.purchases),
                      orders: comparison.purchases,
                    })}
                    {comparison.remaining_purchases > 0 &&
                      ` · ${t('run-result-short', { count: comparison.remaining_purchases })}`}
                  </p>
                </div>
                <span
                  className={classNames(
                    'self-start rounded-full px-3 py-1 text-xs font-semibold sm:self-center',
                    STATUS_STYLE[comparison.status]
                  )}
                >
                  {t(`run-result-status-${comparison.status}`)}
                </span>
              </li>
            ))}
          </ul>
          {result.evaluated_at && (
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {t('run-result-read-at', {
                at: formatDateTime(i18n.language, result.evaluated_at),
              })}
            </p>
          )}
        </div>
      )}

      {uncertain && (
        <p className="text-sm" role="status">
          {t('run-no-answer')}
        </p>
      )}
      {problem && (
        <p className="text-sm text-red-700 dark:text-red-300" role="alert">
          {errorMessage(t, problem)}
        </p>
      )}
    </section>
  );
}
