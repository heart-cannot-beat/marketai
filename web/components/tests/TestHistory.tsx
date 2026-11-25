import { createContext, useContext, useId, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { BUTTON, PRIMARY } from '@/components/ideas/PaidImageAction';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useMemberNames, useTestHistory } from '@/hooks/tests/useTestHistory';
import { formatDate, formatDateTime } from '@/lib/i18n/format';
import {
  productViewHref,
  takeToTestHref,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';
import { dayLabel, moneyLabel } from '@/lib/tests/take-to-test';
import {
  datesMoved,
  earlierTestHref,
  historyTestHref,
  runningTest,
  testStatus,
  uploader,
  type EarlierTest,
  type HistoryReport,
  type HistoryRound,
  type HistoryTest,
  type TestStatus,
} from '@/lib/tests/test-history';

const SECTION =
  'space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';

const STATUS_COPY: Record<TestStatus, string> = {
  setting_up: 'history-status-setting-up',
  running: 'history-status-running',
  ended: 'history-status-ended',
};

const STATUS_STYLE: Record<TestStatus, string> = {
  setting_up:
    'bg-amber-100 text-amber-900 dark:bg-amber-900 dark:text-amber-100',
  running: 'bg-sky-100 text-sky-900 dark:bg-sky-900 dark:text-sky-100',
  ended: 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100',
};

const ROUND_STATE_COPY: Record<HistoryRound['state'], string> = {
  reviewing: 'history-round-state-reviewing',
  handoff: 'history-round-state-handoff',
  awaiting_data: 'history-round-state-awaiting-data',
  evaluated: 'history-round-state-evaluated',
  closed: 'history-round-state-closed',
};

const COMPARISON_COPY: Record<HistoryTest['comparison'], string> = {
  claims: 'history-compare-claims',
  expressions: 'history-compare-expressions',
  single: 'history-compare-single',
};

const MUTED = 'text-gray-600 dark:text-gray-400';

/** Who the reader is and the names they can read, read once for every report line. */
const People = createContext<ReturnType<typeof useMemberNames>>({
  selfId: null,
  names: new Map(),
});

/**
 * "Test history" (design §5.10, §5.11, #258): the product's Tests view. Every
 * test taken from its kept ads, each round with its dates and each report
 * sent back (when and by whom), and the tests made the earlier way, which
 * open on the earlier tests page as before. A test opens on its own page;
 * one whose checks still run opens the "take to test" screen.
 */
export function TestHistory({ product }: { product: WorkspaceProduct }) {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const slug = current.slug;
  const titleId = useId();
  const { history, loading, failed, refresh } = useTestHistory(
    product.productId
  );
  const running = history ? runningTest(history) : null;
  const people = useMemberNames();

  let body;
  if (loading) {
    body = (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('history-loading')}</span>
      </div>
    );
  } else if (failed || !history) {
    body = (
      <div className="space-y-3" role="alert">
        <p className="text-sm">{t('history-problem')}</p>
        <button type="button" className={BUTTON} onClick={() => void refresh()}>
          {t('history-retry')}
        </button>
      </div>
    );
  } else if (!history.tests.length && !history.earlier_tests.length) {
    body = (
      <div className={SECTION} data-testid="history-empty">
        <p className={classNames('max-w-prose text-sm', MUTED)}>
          {t('history-empty')}
        </p>
        <GuardedLink
          href={productViewHref(slug, product.productId, 'ideas')}
          className={classNames(BUTTON, 'w-full sm:w-auto')}
        >
          {t('history-open-ideas')}
        </GuardedLink>
      </div>
    );
  } else {
    body = (
      <>
        {history.tests.length > 0 && (
          <HistorySection
            title={t('history-tests-title')}
            testId="history-tests"
          >
            {history.tests.map((test) => (
              <TestCard
                key={test.id}
                test={test}
                href={historyTestHref(slug, product.productId, test)}
              />
            ))}
          </HistorySection>
        )}
        {history.earlier_tests.length > 0 && (
          <HistorySection
            title={t('history-earlier-title')}
            intro={t('history-earlier-intro')}
            testId="history-earlier"
          >
            {history.earlier_tests.map((test) => (
              <EarlierCard
                key={test.id}
                test={test}
                href={earlierTestHref(slug, test.id)}
              />
            ))}
          </HistorySection>
        )}
      </>
    );
  }

  // One main action: the test in progress, or taking the kept ads to one.
  const action = running ? (
    <GuardedLink
      href={historyTestHref(slug, product.productId, running.test)}
      className={classNames(PRIMARY, 'w-full sm:w-auto')}
      data-testid="history-open-running"
    >
      {t('history-open-running')}
    </GuardedLink>
  ) : history && (product.ideas?.keptCount ?? 0) > 0 ? (
    <GuardedLink
      href={takeToTestHref(slug, product.productId)}
      className={classNames(PRIMARY, 'w-full sm:w-auto')}
      data-testid="history-take-to-test"
    >
      {t('history-take-to-test')}
    </GuardedLink>
  ) : null;

  return (
    <People.Provider value={people}>
      <section
        className="space-y-6"
        aria-labelledby={titleId}
        data-testid="test-history"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 space-y-1">
            <h2 id={titleId} className="text-xl font-bold sm:text-2xl">
              {t('history-title')}
            </h2>
            <p className={classNames('max-w-prose text-sm', MUTED)}>
              {t('history-intro')}
            </p>
          </div>
          {action}
        </div>
        {body}
      </section>
    </People.Provider>
  );
}

function HistorySection({
  title,
  intro,
  testId,
  children,
}: {
  title: string;
  intro?: string;
  testId: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className="space-y-3" aria-labelledby={id} data-testid={testId}>
      <div className="space-y-1">
        <h3 id={id} className="text-lg font-semibold">
          {title}
        </h3>
        {intro && (
          <p className={classNames('max-w-prose text-sm', MUTED)}>{intro}</p>
        )}
      </div>
      <ul role="list" className="space-y-3">
        {children}
      </ul>
    </section>
  );
}

function StatusBadge({ status }: { status: TestStatus }) {
  const { t } = useTranslation('tests');
  return (
    <span
      className={classNames(
        'self-start whitespace-nowrap rounded-full px-3 py-1 text-xs font-semibold',
        STATUS_STYLE[status]
      )}
    >
      {t(STATUS_COPY[status])}
    </span>
  );
}

function TestCard({ test, href }: { test: HistoryTest; href: string }) {
  const { t, i18n } = useTranslation('tests');
  const status = testStatus(test);
  const plan = test.plan_summary;
  const money = (value: string) =>
    moneyLabel(i18n.language, value, plan.currency);
  return (
    <li className={SECTION} data-testid="history-test" data-status={status}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <h4 className="font-semibold">
            {t('history-test-title', {
              day: formatDate(i18n.language, test.created_at, {
                dateStyle: 'long',
              }),
            })}
          </h4>
          <p className={classNames('break-words text-sm', MUTED)}>
            {t(COMPARISON_COPY[test.comparison])}
            {' · '}
            {t('history-plan', {
              target: money(plan.target_cost),
              budget: money(plan.round_budget),
              days: plan.duration_days,
            })}
          </p>
        </div>
        <StatusBadge status={status} />
      </div>
      <Rounds rounds={test.rounds} />
      <GuardedLink
        href={href}
        className={classNames(BUTTON, 'w-full sm:w-auto')}
        data-testid="history-open-test"
      >
        {status === 'setting_up'
          ? t('history-open-setup')
          : t('history-open-test')}
      </GuardedLink>
    </li>
  );
}

function EarlierCard({ test, href }: { test: EarlierTest; href: string }) {
  const { t, i18n } = useTranslation('tests');
  const status: TestStatus | null = test.rounds.length
    ? test.rounds.some((round) => round.state !== 'closed')
      ? 'running'
      : 'ended'
    : null;
  return (
    <li className={SECTION} data-testid="history-earlier-test">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <h4 className="break-words font-semibold">{test.product_name}</h4>
          <p className={classNames('text-sm', MUTED)}>
            {t('history-earlier-made', {
              day: formatDate(i18n.language, test.created_at, {
                dateStyle: 'long',
              }),
            })}
          </p>
        </div>
        {status && <StatusBadge status={status} />}
      </div>
      {test.rounds.length ? (
        <Rounds rounds={test.rounds} />
      ) : (
        <p className={classNames('text-sm', MUTED)}>
          {t('history-earlier-not-run')}
        </p>
      )}
      <GuardedLink
        href={href}
        className={classNames(BUTTON, 'w-full sm:w-auto')}
        data-testid="history-open-earlier"
      >
        {t('history-open-earlier')}
      </GuardedLink>
    </li>
  );
}

function Rounds({ rounds }: { rounds: HistoryRound[] }) {
  const { t, i18n } = useTranslation('tests');
  const day = (value: string) => dayLabel(i18n.language, value);
  return (
    <ol className="space-y-3">
      {rounds.map((round) => (
        <li
          key={round.id}
          className="space-y-2 border-l-2 border-gray-200 pl-3 dark:border-gray-700"
          data-testid="history-round"
        >
          <div className="space-y-0.5">
            <p className="break-words text-sm font-medium">
              {t('history-round', {
                number: round.ordinal,
                start: day(round.effective_start_date),
                end: day(round.effective_end_date),
              })}
              {' · '}
              {t(ROUND_STATE_COPY[round.state])}
            </p>
            {datesMoved(round) && (
              <p className={classNames('text-xs', MUTED)}>
                {t('history-round-planned', {
                  start: day(round.start_date),
                  end: day(round.end_date),
                })}
              </p>
            )}
          </div>
          {round.reports.length ? (
            <ul
              role="list"
              className="space-y-1"
              aria-label={t('history-reports')}
            >
              {round.reports.map((report) => (
                <ReportLine key={report.batch_id} report={report} />
              ))}
            </ul>
          ) : (
            <p className={classNames('text-xs', MUTED)}>
              {t('history-no-report')}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}

function ReportLine({ report }: { report: HistoryReport }) {
  const { t, i18n } = useTranslation('tests');
  const { selfId, names } = useContext(People);
  const who = uploader(report.uploaded_by, selfId, names);
  const parts = [
    formatDateTime(i18n.language, report.uploaded_at, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }),
    who?.kind === 'you'
      ? t('history-who-you')
      : who?.kind === 'member'
        ? who.name
        : who?.kind === 'other'
          ? t('history-who-other')
          : null,
    report.kind === 'correction'
      ? t('history-kind-correction')
      : t('history-kind-upload'),
    t('history-report-rows', { count: report.row_count }),
  ].filter((part): part is string => Boolean(part));
  return (
    <li
      className="break-words text-sm"
      data-testid="history-report"
      data-kind={report.kind}
    >
      {parts.join(' · ')}
    </li>
  );
}
