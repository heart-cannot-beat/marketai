import type { ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { PRIMARY } from '@/components/ideas/PaidImageAction';
import {
  SpendLimitLink,
  isSpendLimit,
} from '@/components/ideas/SpendLimitLink';
import type { UseTestQuote } from '@/hooks/tests/useTestQuote';
import { errorCodeMessage, errorMessage } from '@/lib/i18n/domain-errors';
import { costLabel, costsMore } from '@/lib/ideas/first-batch-view';
import { quoteReason } from '@/lib/ideas/ideas-problems';
import { dayLabel, moneyLabel, type TestQuote } from '@/lib/tests/take-to-test';

/** How full the bar under the verdict is, by tier: a picture of the words, not a number. */
const METER: Record<NonNullable<TestQuote['tier']>, string> = {
  enough: 'w-full',
  direction: 'w-1/2',
  rough: 'w-1/4',
};

/**
 * The right side of "take to test" (design §5.6, §5.11, #256): whether the
 * round's money is enough for a verdict, what each compared object may bring,
 * what a verdict needs and a suggestion when short, when the result is due,
 * the three steps after the start, and the one button "Start the test · at
 * most $X". Every number is the quote's; the page computes none.
 */
export function TestQuotePanel({
  quote,
  ready,
  starting,
  onStart,
  checkHref,
  ideasHref,
  notice,
}: {
  quote: UseTestQuote;
  /** The answers on the page are complete and valid. */
  ready: boolean;
  starting: boolean;
  onStart: () => void;
  /** Where the check before download opens. */
  checkHref: string;
  ideasHref: string;
  /** What the start command said, under the button. */
  notice?: ReactNode;
}) {
  const { t, i18n } = useTranslation('tests');
  const locale = i18n.language;
  const data = quote.kind === 'ok' ? quote.data : null;
  const current = quote.kind === 'ok' && quote.current;
  const canStart = Boolean(
    data && current && data.available && ready && !starting
  );
  const cost = data ? costLabel(locale, data.checks.maximum_cost) : '';

  return (
    <aside
      aria-labelledby="take-quote-heading"
      className="min-w-0 space-y-5 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5 lg:sticky lg:top-4"
      data-testid="test-quote"
    >
      <h3
        id="take-quote-heading"
        className="text-sm font-semibold text-gray-600 dark:text-gray-400"
      >
        {t('take-quote-heading')}
      </h3>
      {quote.kind === 'idle' && (
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {t('take-quote-idle')}
        </p>
      )}
      {quote.kind === 'loading' && (
        <p className="flex items-center gap-2 text-sm" role="status">
          <span
            className="loading loading-spinner loading-sm"
            aria-hidden="true"
          />
          {t('take-quote-loading')}
        </p>
      )}
      {quote.kind === 'problem' && (
        <div className="space-y-2">
          <p className="text-sm text-red-700 dark:text-red-300" role="alert">
            {errorMessage(t, quote.problem)}
          </p>
          <button
            type="button"
            className="btn btn-sm min-h-11"
            onClick={() => void quote.refresh()}
          >
            {t('take-retry')}
          </button>
        </div>
      )}
      {data && (
        <div
          className={classNames('space-y-5', !current && 'opacity-60')}
          aria-busy={!current}
        >
          {!current && (
            <p className="text-sm" role="status">
              {t('take-quote-stale')}
            </p>
          )}
          <Verdict quote={data} />
          <Schedule quote={data} />
          <Steps quote={data} />
          <Unavailable
            quote={data}
            checkHref={checkHref}
            ideasHref={ideasHref}
          />
        </div>
      )}
      <div className="space-y-3">
        <button
          type="button"
          className={classNames(PRIMARY, 'min-h-12 w-full text-base')}
          disabled={!canStart}
          onClick={onStart}
          data-testid="take-start"
        >
          {starting
            ? t('take-starting')
            : data && costsMore(data.checks.maximum_cost)
              ? t('take-start', { cost })
              : t('take-start-free')}
        </button>
        {data && costsMore(data.checks.maximum_cost) && (
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('take-start-cost-note', { count: data.checks.quantity })}
          </p>
        )}
        {notice}
        <GuardedLink
          href={ideasHref}
          className="flex min-h-11 items-center justify-center text-sm font-medium underline"
        >
          {t('take-back-to-ideas')}
        </GuardedLink>
      </div>
    </aside>
  );
}

function Verdict({ quote }: { quote: TestQuote }) {
  const { t, i18n } = useTranslation('tests');
  if (!quote.tier || quote.subjects.length === 0) return null;
  const kind = quote.subjects[0].kind;
  const orders = quote.subjects[0].expected_orders;
  const money = (value: string) =>
    moneyLabel(i18n.language, value, quote.currency);
  return (
    <div className="space-y-3" data-testid="test-verdict">
      <p className="text-2xl font-bold leading-tight">
        {t(`take-tier-${quote.tier}`)}
      </p>
      <div
        aria-hidden="true"
        className="h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"
      >
        <div
          className={classNames(
            'h-2 rounded-full',
            quote.tier === 'enough'
              ? 'bg-emerald-600'
              : quote.tier === 'direction'
                ? 'bg-amber-500'
                : 'bg-red-500',
            METER[quote.tier]
          )}
        />
      </div>
      <p className="text-sm">
        {t(`take-orders-${kind}`, { count: orders })}{' '}
        {t(`take-tier-${quote.tier}-body`)}
      </p>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('take-min', { count: quote.min_purchases })}
      </p>
      {quote.tier !== 'enough' && quote.needed_budget && (
        <p className="text-sm font-semibold" data-testid="test-needed">
          {t('take-needed', { amount: money(quote.needed_budget) })}
        </p>
      )}
      {quote.tier !== 'enough' && quote.suggested_subjects && (
        <p className="text-sm">
          {t('take-suggest', { count: quote.suggested_subjects })}
        </p>
      )}
    </div>
  );
}

function Schedule({ quote }: { quote: TestQuote }) {
  const { t, i18n } = useTranslation('tests');
  const day = (value: string) => dayLabel(i18n.language, value);
  return (
    <div className="space-y-1 text-sm text-gray-700 dark:text-gray-300">
      <p>
        {t('take-schedule', {
          count: quote.duration_days,
          start: day(quote.start_date),
          end: day(quote.end_date),
          lag: quote.attribution_lag_days,
        })}
      </p>
      <p className="font-semibold text-gray-900 dark:text-gray-100">
        {t('take-conclusion', { date: day(quote.conclusion_date) })}
      </p>
    </div>
  );
}

function Steps({ quote }: { quote: TestQuote }) {
  const { t, i18n } = useTranslation('tests');
  const steps = [
    ['take-step-download', t('take-step-download-body')],
    [
      'take-step-meta',
      t('take-step-meta-body', {
        amount: moneyLabel(i18n.language, quote.round_budget, quote.currency),
      }),
    ],
    ['take-step-report', t('take-step-report-body')],
  ] as const;
  return (
    <div className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700">
      <h4 className="text-sm font-semibold">{t('take-steps-heading')}</h4>
      <ol className="space-y-3">
        {steps.map(([title, body], index) => (
          <li key={title} className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gray-900 text-xs font-bold text-white dark:bg-gray-100 dark:text-gray-900"
            >
              {index + 1}
            </span>
            <div className="min-w-0 text-sm">
              <p className="font-semibold">{t(title)}</p>
              <p className="text-gray-600 dark:text-gray-400">{body}</p>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Why the quote cannot start a test yet, and where that is sorted out. */
function Unavailable({
  quote,
  checkHref,
  ideasHref,
}: {
  quote: TestQuote;
  checkHref: string;
  ideasHref: string;
}) {
  const { t } = useTranslation('tests');
  const reason = quote.unavailable_reason;
  if (quote.available || !reason) return null;
  // The landing page question itself says what is missing.
  if (reason === 'landing_page_required') return null;
  let sentence: string;
  let action: ReactNode = null;
  if (reason === 'ads_not_exportable') {
    sentence = t('take-unavailable-ads_not_exportable', {
      count: quote.not_exportable.length,
    });
    action = (
      <GuardedLink href={checkHref} className="btn btn-sm min-h-11 bg-white">
        {t('take-go-check')}
      </GuardedLink>
    );
  } else if (reason === 'checks_unavailable') {
    sentence = quoteReason(t, quote.checks.unavailable_reason);
    if (isSpendLimit(quote.checks.unavailable_reason))
      action = <SpendLimitLink />;
  } else {
    sentence = errorCodeMessage(t, reason);
    if (reason === 'no_kept_ads' || reason === 'ideas_not_ready')
      action = (
        <GuardedLink href={ideasHref} className="btn btn-sm min-h-11 bg-white">
          {t('take-back-to-ideas')}
        </GuardedLink>
      );
  }
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
      role="note"
      data-testid="test-unavailable"
    >
      <p className="min-w-0 flex-1 basis-48">{sentence}</p>
      {action}
    </div>
  );
}
