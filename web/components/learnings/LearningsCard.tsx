import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { formatDateTime } from '@/lib/i18n/format';
import { thousands, type ProductLearnings } from '@/lib/learnings/format';
import { NextRoundButton } from './NextRoundButton';
import { ResultsImportDialog } from './ResultsImportDialog';
import { RoundView } from './RoundView';

export interface LearningsCardProps {
  productId: string;
  /** Called with the next round's plan id (e.g. to open the plan page). */
  onNextRoundCreated?: (planId: string) => void;
}

function verdictCounts(learnings: ProductLearnings) {
  let winners = 0;
  let losers = 0;
  for (const round of learnings.rounds ?? []) {
    for (const factor of round.factors ?? []) {
      for (const value of factor.values ?? []) {
        if (value.verdict === 'winner') winners++;
        if (value.verdict === 'loser') losers++;
      }
    }
  }
  return { winners, losers };
}

function ReviewSignals({ learnings }: { learnings: ProductLearnings }) {
  const { t } = useTranslation('learnings');
  const signals = learnings.review_signals;
  if (!signals) return null;
  return (
    <div className="space-y-2" data-testid="review-signals">
      <p className="text-sm font-medium">
        {t('review-signals-title', { round: signals.round_number })}
      </p>
      {/* The server's `note` is fixed Chinese text; the copy is ours. */}
      <p className="text-xs text-base-content/60">{t('review-signals-note')}</p>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
        {(signals.factors ?? []).flatMap((factor) =>
          (factor.values ?? []).map((value) => (
            <div
              key={`${factor.key}:${value.value_id}`}
              className="flex justify-between gap-3"
            >
              <dt className="text-base-content/80">
                {t('review-signal-label', {
                  factor: factor.label,
                  value: value.label,
                })}
              </dt>
              <dd className="tabular-nums text-base-content/60">
                {t('review-signal-row', {
                  approved: value.approved,
                  assets: value.assets,
                })}
              </dd>
            </div>
          ))
        )}
      </dl>
    </div>
  );
}

/**
 * 「这个产品我们已经学到了什么」(contract §3.3, Issue #166): the latest
 * learnings snapshot of one product -- per factor value a verdict, the lift
 * against the other values with its 95% interval, and n -- plus the import
 * flow and "按结论生成下一轮". With no ad data it says so plainly and shows
 * human review only as a weak signal; demo (Mock) data carries its notice.
 */
export function LearningsCard({
  productId,
  onNextRoundCreated,
}: LearningsCardProps) {
  const { t, i18n } = useTranslation('learnings');
  const query = useDomainQuery('get_product_learnings', {
    product_id: productId,
  });
  const [importing, setImporting] = useState(false);
  const learnings = query.data as ProductLearnings | undefined;
  const rounds = learnings?.rounds ?? [];
  const highlights = learnings?.highlights ?? [];
  const unattributable = learnings?.unattributable ?? [];
  const latest = rounds[0];
  const earlier = rounds.slice(1);
  const counts = learnings
    ? verdictCounts(learnings)
    : { winners: 0, losers: 0 };
  const thresholds = learnings?.thresholds ?? {};

  return (
    <section
      className="rounded-box border border-base-300 bg-base-100 p-5 sm:p-6"
      aria-labelledby={`learnings-${productId}`}
      data-testid="learnings-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-prose">
          <h2 id={`learnings-${productId}`} className="text-lg font-semibold">
            {t('card-title')}
          </h2>
          <p className="mt-1 text-sm text-base-content/70">{t('card-intro')}</p>
        </div>
        {learnings?.status === 'ready' && (
          <div className="flex flex-wrap items-start gap-2">
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setImporting(true)}
            >
              {t('import-more')}
            </button>
            <NextRoundButton
              productId={productId}
              snapshotId={learnings.snapshot_id}
              canSeed={learnings.can_seed_next_round}
              winners={counts.winners}
              losers={counts.losers}
              onCreated={onNextRoundCreated}
            />
          </div>
        )}
      </div>

      {query.isLoading && !learnings && (
        <div className="flex justify-center py-8" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
          <span className="sr-only">{t('loading')}</span>
        </div>
      )}

      {query.problem && !learnings && (
        <div className="alert alert-error mt-4" role="alert">
          <span>
            {query.problem.status === 403 ? t('forbidden') : t('load-error')}
          </span>
          {query.problem.status !== 403 && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void query.refresh()}
            >
              {t('retry')}
            </button>
          )}
        </div>
      )}

      {/* `evidence_notice` says a notice is due; it reads by mode (#286). */}
      {learnings?.evidence_notice && learnings.evidence_mode && (
        <div className="alert alert-warning mt-4 py-2 text-sm" role="note">
          <span>{t(`evidence-notice-${learnings.evidence_mode}`)}</span>
        </div>
      )}

      {learnings?.status === 'no_data' && (
        <div className="mt-5 space-y-4">
          <div className="border-l-4 border-base-300 pl-4">
            <p className="font-medium">{t('no-data-title')}</p>
            <p className="mt-1 max-w-prose text-sm text-base-content/70">
              {t('no-data-body')}
            </p>
          </div>
          <ReviewSignals learnings={learnings} />
        </div>
      )}

      {learnings?.status === 'ready' && latest && (
        <div className="mt-5 space-y-6">
          {highlights.length > 0 && (
            <ul className="space-y-2" aria-label={t('highlights-title')}>
              {highlights.map((sentence) => (
                <li
                  key={sentence}
                  className="text-base font-medium leading-relaxed"
                >
                  {sentence}
                </li>
              ))}
            </ul>
          )}
          <div className="space-y-4">
            <p className="text-sm text-base-content/60">
              <span className="font-medium text-base-content">
                {t('round-label', { number: latest.round_number })}
              </span>{' '}
              {t('round-totals', {
                impressions: thousands(
                  latest.totals.impressions ?? 0,
                  i18n.language
                ),
                clicks: thousands(latest.totals.clicks ?? 0, i18n.language),
                assets: latest.totals.assets ?? 0,
              })}
            </p>
            <RoundView round={latest} />
          </div>
          {earlier.length > 0 && (
            <details>
              <summary className="cursor-pointer text-sm text-base-content/70">
                {t('earlier-rounds', { count: earlier.length })}
              </summary>
              <div className="mt-4 space-y-8">
                {earlier.map((round) => (
                  <div key={round.plan_id} className="space-y-3">
                    <p className="text-sm font-medium">
                      {t('round-label', { number: round.round_number })}
                    </p>
                    <RoundView round={round} />
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}

      {unattributable.length > 0 && (
        <p className="mt-4 text-sm text-base-content/60">
          {t('unattributable-title', {
            count: unattributable.length,
          })}
        </p>
      )}

      {learnings?.status === 'ready' && (
        <p className="mt-4 max-w-prose text-xs text-base-content/50">
          {t('method-note', {
            impressions: thousands(
              thresholds.min_impressions ?? '1000',
              i18n.language
            ),
            clicks: thresholds.min_clicks ?? '30',
            conversions: thresholds.min_conversions ?? '10',
            assets: thresholds.min_assets ?? '2',
          })}
          {learnings.computed_at &&
            ` ${t('computed-at', {
              time: formatDateTime(i18n.language, learnings.computed_at),
            })}`}
        </p>
      )}

      {learnings?.status === 'no_data' && (
        <div className="mt-5">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => setImporting(true)}
          >
            {t('import-results')}
          </button>
        </div>
      )}

      <ResultsImportDialog
        productId={productId}
        open={importing}
        onClose={() => setImporting(false)}
        onCommitted={() => void query.refresh()}
      />
    </section>
  );
}
