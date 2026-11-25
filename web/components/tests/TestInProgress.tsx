import { useId, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { AdThumbnail } from '@/components/ideas/AdCard';
import { BUTTON, PRIMARY } from '@/components/ideas/PaidImageAction';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import {
  useTestInProgress,
  type TestInProgress as Run,
} from '@/hooks/tests/useTestInProgress';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { takeToTestHref } from '@/lib/products/workspace-products';
import { moneyLabel } from '@/lib/tests/take-to-test';
import { checklist, packageReady } from '@/lib/tests/test-in-progress';
import { ReportUpload } from './ReportUpload';
import { TestNumbers } from './TestNumbers';
import { TestResult } from './TestResult';
import { TestSchedule } from './TestSchedule';
import { Step } from './TestStartProgress';

const SECTION =
  'space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';

/**
 * "Test in progress" (design §5.7–§5.9, §5.11, #257): a test once its ad
 * package is made. Three steps -- put the package in Meta, send the report
 * back and match the ads, read the result once it is due --, the report
 * upload, the numbers so far (not a conclusion), the round's schedule with
 * moving the start and ending early, what ran differently from the plan,
 * and the ads the ideas changed after the start. Every number is the
 * server's.
 */
export function TestInProgress({
  productId,
  testId,
}: {
  productId: string;
  testId: string;
}) {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const run = useTestInProgress(testId);
  const takeHref = takeToTestHref(current.slug, productId);

  if (run.loading) {
    if (run.readProblem)
      return (
        <div className="space-y-3">
          <p className="text-sm" role="alert">
            {run.readProblem.code === 'marketing_test_not_found'
              ? t('run-not-found')
              : t('take-progress-problem')}
          </p>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void run.refresh()}
          >
            {t('take-retry')}
          </button>
        </div>
      );
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('take-loading')}</span>
      </div>
    );
  }

  const round = run.round!;
  if (!packageReady(round))
    return (
      <div className="space-y-3" data-testid="test-not-running">
        <h2 className="text-xl font-bold sm:text-2xl">
          {round.state === 'closed'
            ? t('take-closed')
            : t('run-no-package-title')}
        </h2>
        {round.state !== 'closed' && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('run-no-package-body')}
          </p>
        )}
        <GuardedLink href={takeHref} className={PRIMARY}>
          {round.state === 'closed'
            ? t('run-take-again')
            : t('run-open-take-to-test')}
        </GuardedLink>
      </div>
    );

  return <Running run={run} />;
}

function Running({ run }: { run: Run }) {
  const { t, i18n } = useTranslation('tests');
  const test = run.test!;
  const round = run.round!;
  const result = run.result!;
  const steps = checklist(round, result);
  const plan = test.plan_summary;
  const money = (value: string) =>
    moneyLabel(i18n.language, value, plan.currency);
  const closed = round.state === 'closed';

  const resultSection = (
    <div className={SECTION}>
      <TestResult run={run} />
    </div>
  );

  return (
    <div className="space-y-6" data-testid="test-in-progress">
      <div className="space-y-1">
        <h2 className="text-xl font-bold sm:text-2xl">
          {closed ? t('run-title-closed') : t('run-title')}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('run-summary', {
            count: round.handoff?.entries.length ?? 0,
            target: money(plan.target_cost),
            budget: money(plan.round_budget),
            days: plan.duration_days,
          })}
        </p>
      </div>

      <ol className="grid gap-3 sm:grid-cols-3" aria-label={t('run-steps')}>
        <Step
          number={1}
          title={t('run-step-run')}
          done={steps.done.run}
          active={steps.active === 'run'}
        >
          {steps.done.run ? t('run-step-run-done') : t('run-step-run-body')}
        </Step>
        <Step
          number={2}
          title={t('run-step-report')}
          done={steps.done.report}
          active={steps.active === 'report'}
        >
          {steps.done.report
            ? t('run-step-report-done')
            : t('run-step-report-body')}
        </Step>
        <Step
          number={3}
          title={t('run-step-result')}
          done={steps.done.result}
          active={steps.active === 'result'}
        >
          {result.mature ? t('run-step-result-due') : t('run-step-result-body')}
        </Step>
      </ol>

      <ChangedAds run={run} />

      {result.mature && resultSection}

      <div className={SECTION}>
        <Package run={run} />
      </div>

      <div className={SECTION}>
        <h3 className="text-lg font-semibold">{t('run-report-title')}</h3>
        <ReportUpload run={run} />
      </div>

      <div className={SECTION}>
        <TestNumbers run={run} />
      </div>

      {!result.mature && resultSection}

      <div className={SECTION}>
        <TestSchedule run={run} />
      </div>
    </div>
  );
}

/**
 * The ads the ideas changed after the test started (design §5.8): the
 * running test keeps what it started on, but the Meta ad still shows the
 * old content, so the page suggests stopping it or making it again.
 */
function ChangedAds({ run }: { run: Run }) {
  const { t } = useTranslation('tests');
  const id = useId();
  const changed = run.progress?.changed_ads ?? [];
  if (!changed.length) return null;
  const headlines = new Map(
    (run.round?.handoff?.entries ?? []).map((entry) => [
      entry.asset_version_id,
      entry.headline,
    ])
  );
  return (
    <section
      className="space-y-3 rounded-xl border border-amber-400 bg-amber-50 p-4 text-amber-950 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100 sm:p-5"
      aria-labelledby={`${id}-title`}
      role="alert"
      data-testid="changed-ads"
    >
      <h3 id={`${id}-title`} className="font-semibold">
        {t('run-changed-title', { count: changed.length })}
      </h3>
      <ul role="list" className="space-y-3">
        {changed.map((ad) => (
          <li key={ad.asset_version_id} className="flex items-start gap-3">
            <AdThumbnail
              versionId={ad.asset_version_id}
              alt={headlines.get(ad.asset_version_id) ?? ''}
              size="xs"
            />
            <div className="min-w-0 space-y-1 text-sm">
              <p className="break-words font-semibold">
                {headlines.get(ad.asset_version_id)}
              </p>
              {ad.changes.map((change) => (
                <p key={change}>{t(`run-changed-${change}`)}</p>
              ))}
            </div>
          </li>
        ))}
      </ul>
      <p className="text-sm">{t('run-changed-advice')}</p>
    </section>
  );
}

/** Step 1: the ad package, still downloadable as the test started (design §5.8). */
function Package({ run }: { run: Run }) {
  const { t } = useTranslation('tests');
  const id = useId();
  const entries = run.round?.handoff?.entries ?? [];
  const [url, setUrl] = useState<string | null>(null);
  const [problem, setProblem] = useState<ProblemDetails | null>(null);
  const [uncertain, setUncertain] = useState(false);

  async function download() {
    const outcome = await run.download();
    setUncertain(outcome.kind === 'uncertain');
    setProblem(outcome.kind === 'refused' ? outcome.problem : null);
    if (outcome.kind === 'done' && outcome.data.download_grant?.url)
      setUrl(outcome.data.download_grant.url);
  }

  return (
    <section className="space-y-3" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="text-lg font-semibold">
        {t('take-download-title', { count: entries.length })}
      </h3>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('take-ready-next')}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className={classNames(BUTTON, 'w-full sm:w-auto')}
          disabled={run.busy}
          onClick={() => void download()}
          data-testid="package-download"
        >
          {t('take-download')}
        </button>
        {url && (
          <a
            className="text-sm font-semibold underline"
            href={url}
            target="_blank"
            rel="noreferrer"
          >
            {t('take-download-open')}
          </a>
        )}
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer font-medium">
          {t('run-package-names')}
        </summary>
        <ul
          role="list"
          className="mt-2 divide-y divide-gray-200 border-y border-gray-200 dark:divide-gray-700 dark:border-gray-700"
        >
          {entries.map((entry) => (
            <Entry
              key={entry.entry_id}
              versionId={entry.asset_version_id}
              headline={entry.headline}
            >
              {t('take-ad-name', { name: entry.ad_name })}
            </Entry>
          ))}
        </ul>
      </details>
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

function Entry({
  versionId,
  headline,
  children,
}: {
  versionId: string;
  headline: string;
  children: ReactNode;
}) {
  return (
    <li className="flex items-center gap-3 py-2">
      <AdThumbnail versionId={versionId} alt={headline} size="xs" />
      <div className="min-w-0 text-sm">
        <p className="truncate font-medium">{headline}</p>
        <p className="break-words text-xs text-gray-600 dark:text-gray-400">
          {children}
        </p>
      </div>
    </li>
  );
}
