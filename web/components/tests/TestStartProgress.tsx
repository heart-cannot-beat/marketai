import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import { useRouter } from 'next/router';
import classNames from 'classnames';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { AdThumbnail } from '@/components/ideas/AdCard';
import { BUTTON, PRIMARY } from '@/components/ideas/PaidImageAction';
import { useStartProgress, type StartStage } from '@/hooks/tests/useTestStart';
import { errorMessage } from '@/lib/i18n/domain-errors';
import type { OpenAd } from '@/lib/tests/take-to-test';

/** The comparison checks the page names in its own words (`workflow.PAIRED_CHECKS`). */
const KNOWN_CHECKS = new Set([
  'belongs_to_intended_combination',
  'expressions_distinct',
  'comparison_faithful',
]);

const NOTE_LIMIT = 500;

/** Stages before the package exists: the test can still be cancelled. */
const CANCELLABLE: ReadonlySet<StartStage> = new Set<StartStage>([
  'checking',
  'findings',
  'unproven',
  'blocked',
]);

/**
 * A started test until its ad package is downloaded (design §5.3, §5.11,
 * #256): the "look together" checks running, then the package being made,
 * then its download. Checks that did not pass open by themselves, one ad
 * at a time: "this is fine" with a reason, or cancel the test to swap or
 * take out an ad -- nothing is in Meta yet. The page asks for the package
 * once every check has its result, also when it is opened again (plan E7).
 */
export function TestStartProgress({
  testId,
  ideasHref,
  runHref,
  onRestart,
}: {
  testId: string;
  ideasHref: string;
  /** The test's "test in progress" screen (#257), where its reports go once the package is out. */
  runHref: string;
  /** The test was cancelled elsewhere: back to the questions. */
  onRestart: () => void;
}) {
  const { t } = useTranslation('tests');
  const router = useRouter();
  const progress = useStartProgress(testId);
  const { stage, round } = progress;
  const [confirming, setConfirming] = useState(false);
  // A package that was ready before this page opened has been handed out:
  // the test carries on on its own screen. One made while the page is open
  // is shown here first, to download, with the way on.
  const sawWork = useRef(false);
  useEffect(() => {
    if (stage === 'loading') return;
    if (stage !== 'ready') sawWork.current = true;
    else if (!sawWork.current) void router.replace(runHref);
  }, [stage, router, runHref]);

  if (stage === 'loading') {
    if (progress.problem)
      return (
        <div className="space-y-3">
          <p className="text-sm" role="alert">
            {t('take-progress-problem')}
          </p>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void progress.refresh()}
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

  if (stage === 'closed')
    return (
      <div className="space-y-3" data-testid="test-closed">
        <h2 className="text-xl font-bold sm:text-2xl">{t('take-closed')}</h2>
        <div className="flex flex-wrap gap-3">
          <button type="button" className={PRIMARY} onClick={onRestart}>
            {t('take-restart')}
          </button>
          <GuardedLink href={ideasHref} className={BUTTON}>
            {t('take-back-to-ideas')}
          </GuardedLink>
        </div>
      </div>
    );

  async function cancel() {
    const outcome = await progress.cancel();
    if (outcome.kind === 'done') void router.push(ideasHref);
  }

  const packaged = stage === 'ready' || stage === 'package_failed';
  const total = progress.progress.total;

  return (
    <div className="space-y-6" data-testid="test-start-progress">
      <div className="space-y-1">
        <h2 className="text-xl font-bold sm:text-2xl">
          {stage === 'ready'
            ? t('take-ready-title')
            : t('take-progress-title', { count: total })}
        </h2>
        {stage !== 'ready' && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('take-progress-intro')}
          </p>
        )}
      </div>

      <ol
        className="grid gap-3 sm:grid-cols-3"
        aria-label={t('take-progress-steps')}
      >
        <Step
          number={1}
          title={t('take-progress-step-checks')}
          done={stage !== 'checking'}
          active={stage === 'checking'}
        >
          {stage === 'checking'
            ? t('take-progress-checks-count', {
                answered: progress.progress.answered,
                total,
              })
            : t('take-progress-checks-done')}
        </Step>
        <Step
          number={2}
          title={t('take-progress-step-package')}
          done={packaged}
          active={stage === 'packaging'}
        >
          {stage === 'ready'
            ? t('take-progress-package-ready')
            : stage === 'package_failed'
              ? t('take-progress-package-failed')
              : stage === 'packaging'
                ? t('take-progress-package-making')
                : t('take-progress-package-waiting')}
        </Step>
        <Step
          number={3}
          title={t('take-progress-step-download')}
          done={false}
          active={stage === 'ready'}
        >
          {stage === 'ready'
            ? t('take-progress-download-now')
            : t('take-progress-download-later')}
        </Step>
      </ol>

      {(stage === 'checking' || stage === 'packaging') && (
        <p className="flex items-center gap-2 text-sm" role="status">
          <span
            className="loading loading-spinner loading-sm"
            aria-hidden="true"
          />
          {stage === 'checking'
            ? t('take-progress-checking')
            : t('take-progress-package-making')}
        </p>
      )}

      {progress.uncertain && (
        <p className="text-sm text-gray-700 dark:text-gray-300" role="status">
          {t('take-waiting-answer')}
        </p>
      )}

      {stage === 'findings' && (
        <OpenFindings
          ads={progress.open}
          busy={progress.busy}
          onAccept={(notes) => void progress.accept(notes)}
        />
      )}

      {stage === 'unproven' && (
        <section
          className="space-y-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
          aria-labelledby="take-unproven-title"
        >
          <h3 id="take-unproven-title" className="font-semibold">
            {t('take-unproven-title')}
          </h3>
          <ul role="list" className="space-y-2">
            {progress.unproven.map((ad) => (
              <li key={ad.assetVersionId} className="flex items-center gap-3">
                <AdThumbnail
                  versionId={ad.assetVersionId}
                  alt={ad.headline}
                  size="xs"
                />
                <div className="min-w-0 text-sm">
                  <p className="font-semibold">{ad.headline}</p>
                  <p>
                    {t(`take-unproven-${ad.reason}`, {
                      defaultValue: t('take-unproven-no_evidence'),
                    })}
                  </p>
                </div>
              </li>
            ))}
          </ul>
          <p className="text-sm">{t('take-unproven-help')}</p>
        </section>
      )}

      {stage === 'blocked' && progress.problem && (
        <div className="space-y-2">
          <p className="text-sm text-red-700 dark:text-red-300" role="alert">
            {errorMessage(t, progress.problem)}
          </p>
          <button
            type="button"
            className={BUTTON}
            disabled={progress.busy}
            onClick={() => void progress.refresh()}
          >
            {t('take-refresh')}
          </button>
        </div>
      )}

      {stage === 'package_failed' && (
        <div className="space-y-2">
          <p className="text-sm" role="alert">
            {t('take-package-failed')}
          </p>
          <button
            type="button"
            className={BUTTON}
            disabled={progress.busy}
            onClick={() => void progress.retryPackage()}
          >
            {t('take-package-retry')}
          </button>
        </div>
      )}

      {stage === 'ready' && (
        <section
          className="space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5"
          aria-labelledby="take-download-title"
          data-testid="test-package"
        >
          <h3 id="take-download-title" className="font-semibold">
            {t('take-download-title', {
              count: round?.handoff?.entries.length ?? total,
            })}
          </h3>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={classNames(PRIMARY, 'w-full sm:w-auto')}
              disabled={progress.busy}
              onClick={() => void progress.download()}
            >
              {t('take-download')}
            </button>
            {progress.downloadUrl && (
              <a
                className="text-sm font-semibold underline"
                href={progress.downloadUrl}
                target="_blank"
                rel="noreferrer"
              >
                {t('take-download-open')}
              </a>
            )}
          </div>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('take-ready-next')}
          </p>
          <GuardedLink
            href={runHref}
            className={classNames(BUTTON, 'w-full sm:w-auto')}
            data-testid="test-open-run"
          >
            {t('take-open-run')}
          </GuardedLink>
          <ul
            role="list"
            className="divide-y divide-gray-200 border-y border-gray-200 dark:divide-gray-700 dark:border-gray-700"
          >
            {round?.handoff?.entries.map((entry) => (
              <li key={entry.entry_id} className="flex items-center gap-3 py-2">
                <AdThumbnail
                  versionId={entry.asset_version_id}
                  alt={entry.headline}
                  size="xs"
                />
                <div className="min-w-0 text-sm">
                  <p className="truncate font-medium">{entry.headline}</p>
                  <p className="break-words text-xs text-gray-600 dark:text-gray-400">
                    {t('take-ad-name', { name: entry.ad_name })}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {stage !== 'blocked' &&
        progress.problem &&
        stage !== 'findings' &&
        stage !== 'unproven' && (
          <p className="text-sm text-red-700 dark:text-red-300" role="alert">
            {errorMessage(t, progress.problem)}
          </p>
        )}

      {CANCELLABLE.has(stage) && (
        <div className="space-y-2 border-t border-gray-200 pt-4 dark:border-gray-700">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('take-cancel-help')}
          </p>
          {confirming ? (
            <div className="flex flex-wrap gap-3">
              <button
                type="button"
                className={PRIMARY}
                disabled={progress.busy}
                onClick={() => void cancel()}
              >
                {t('take-cancel-confirm')}
              </button>
              <button
                type="button"
                className={BUTTON}
                onClick={() => setConfirming(false)}
              >
                {t('take-cancel-keep')}
              </button>
            </div>
          ) : (
            <button
              type="button"
              className={BUTTON}
              onClick={() => setConfirming(true)}
            >
              {t('take-cancel')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** One step of a three-step list; the "test in progress" screen (#257) uses it too. */
export function Step({
  number,
  title,
  done,
  active,
  children,
}: {
  number: number;
  title: string;
  done: boolean;
  active: boolean;
  children: ReactNode;
}) {
  return (
    <li
      className={classNames(
        'flex items-start gap-3 rounded-lg border p-3',
        active
          ? 'border-gray-900 bg-white dark:border-gray-100 dark:bg-gray-900'
          : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900'
      )}
      aria-current={active ? 'step' : undefined}
    >
      <span
        aria-hidden="true"
        className={classNames(
          'flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold',
          done
            ? 'bg-emerald-600 text-white'
            : 'bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900'
        )}
      >
        {done ? '✓' : number}
      </span>
      <div className="min-w-0 text-sm">
        <p className="font-semibold">{title}</p>
        <p className="text-gray-600 dark:text-gray-400">{children}</p>
      </div>
    </li>
  );
}

/**
 * The ads whose "look together" check did not pass, and only those (design
 * §5.3): each finding in words, and one reason per ad for "this is fine".
 * Swapping or taking out an ad means cancelling the test, offered below.
 */
function OpenFindings({
  ads,
  busy,
  onAccept,
}: {
  ads: OpenAd[];
  busy: boolean;
  onAccept: (notes: Record<string, string>) => void;
}) {
  const { t } = useTranslation('tests');
  const id = useId();
  const [notes, setNotes] = useState<Record<string, string>>({});
  const complete = ads.every((ad) => (notes[ad.assetVersionId] ?? '').trim());
  return (
    <section
      className="space-y-4 rounded-xl border border-amber-300 bg-amber-50 p-4 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100 sm:p-5"
      aria-labelledby={`${id}-title`}
      data-testid="test-open-findings"
    >
      <div className="space-y-1">
        <h3 id={`${id}-title`} className="font-semibold">
          {t('take-findings-title', { count: ads.length })}
        </h3>
        <p className="text-sm">{t('take-findings-intro')}</p>
      </div>
      <ul role="list" className="space-y-4">
        {ads.map((ad) => (
          <li
            key={ad.assetVersionId}
            className="space-y-3 rounded-lg bg-white p-3 text-gray-900 dark:bg-gray-900 dark:text-gray-100"
            data-testid="test-open-ad"
          >
            <div className="flex items-center gap-3">
              <AdThumbnail versionId={ad.assetVersionId} alt={ad.headline} />
              <p className="min-w-0 font-semibold">{ad.headline}</p>
            </div>
            <ul role="list" className="space-y-2">
              {ad.findings.map((finding) => (
                <li key={finding.check_id} className="space-y-1 text-sm">
                  <p className="font-semibold">
                    {KNOWN_CHECKS.has(finding.check_id)
                      ? t(`take-check-${finding.check_id}`)
                      : t('take-check-other')}
                    <span className="ml-2 inline-block rounded-full bg-amber-100 px-2 py-px text-xs font-semibold text-amber-900 dark:bg-amber-900 dark:text-amber-100">
                      {t(
                        finding.status === 'fail'
                          ? 'take-finding-status-fail'
                          : 'take-finding-status-unknown'
                      )}
                    </span>
                  </p>
                  {finding.rationale && <p>{finding.rationale}</p>}
                  {finding.suggested_fix && (
                    <p className="text-gray-600 dark:text-gray-400">
                      {t('take-finding-fix', { fix: finding.suggested_fix })}
                    </p>
                  )}
                </li>
              ))}
            </ul>
            <label className="block space-y-1 text-sm">
              <span className="font-semibold">{t('take-accept-label')}</span>
              <textarea
                className="textarea textarea-bordered min-h-20 w-full text-sm"
                maxLength={NOTE_LIMIT}
                value={notes[ad.assetVersionId] ?? ''}
                onChange={(event) =>
                  setNotes({
                    ...notes,
                    [ad.assetVersionId]: event.target.value,
                  })
                }
              />
            </label>
          </li>
        ))}
      </ul>
      <div className="space-y-2">
        <button
          type="button"
          className={classNames(PRIMARY, 'w-full sm:w-auto')}
          disabled={!complete || busy}
          onClick={() => onAccept(notes)}
        >
          {t('take-accept')}
        </button>
        <p className="text-xs">{t('take-accept-help')}</p>
      </div>
    </section>
  );
}
