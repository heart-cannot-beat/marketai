import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  MinusCircleIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { formatDateTime } from '@/lib/i18n/format';
import { SimulatedBadge } from './CredentialNotice';
import { countryName } from './market';
import { ScanQuote } from './ScanQuote';
import { activeRecluster } from './SnapshotSummary';
import {
  ACTIVE_STATUSES,
  codeMessage,
  dataOf,
  formatCount,
  formatUsd,
  pageLibraryUrl,
  problemMessage,
  useCommand,
  type CompetitorScan,
} from './shared';

const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';

const STEPS = ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'] as const;

type Scan = CompetitorScan;
type Target = NonNullable<Scan['targets']>[number];

/** A fetch path's name: the competitor's page, or "Keyword “x”". */
function useOriginLabel(scan: Scan) {
  const { t } = useTranslation('competitors');
  return (origin: string) => {
    const target = (scan.targets ?? []).find((item) => item.origin === origin);
    if (target?.kind === 'keyword' || origin.startsWith('keyword:'))
      return t('target-keyword', {
        keyword: target?.keyword ?? origin.slice('keyword:'.length),
      });
    return (
      target?.page_name ||
      target?.observed_page_name ||
      target?.page_id ||
      origin
    );
  };
}

/**
 * The clusters revision of the latest completed recluster, when the scan's
 * own cluster step failed or was skipped (#353): that revision is the one in
 * effect, so the step reads as done by it. The API keeps the step's status.
 */
function supersedingRevision(scan: Scan): number | null {
  const step = (scan.steps ?? []).find((s) => s.step === 'cluster');
  if (step?.status !== 'failed' && step?.status !== 'skipped') return null;
  const latest = [...(scan.reclusters ?? [])]
    .reverse()
    .find((r) => r.status === 'completed' && r.cluster_revision != null);
  return latest?.cluster_revision ?? null;
}

/**
 * One scan as the Worker reports it (spec §5.1, §9): its steps in order, each
 * fetch path, what it cost so far, and — when it stopped short — why, with
 * "Resume" and its remaining quote (`partial`, a resumable `failed`) or the
 * pages awaiting reconciliation (`uncertain`). A cluster step a completed
 * recluster superseded shows as done by that revision; whether to offer
 * "Resume" is still the API's `can_resume`. A page that answered no ads and
 * that nothing has named may have a wrong ID: its row says so, with the
 * page's Ad Library link and, while the competitor is listed, "Change page".
 */
export function ScanProgress({
  scan,
  onChanged,
  listedCompetitorIds = [],
  onChangePage,
}: {
  scan: Scan;
  onChanged: (scan?: Scan) => Promise<unknown>;
  /** The competitors on the list now: only theirs can change page here. */
  listedCompetitorIds?: string[];
  onChangePage?: (competitorId: string) => void;
}) {
  const { t, i18n } = useTranslation('competitors');
  const locale = i18n.language;
  const active = ACTIVE_STATUSES.includes(scan.status);
  const failedStep =
    (scan.steps ?? []).find((step) => step.status === 'failed')?.step ??
    scan.stage ??
    'fetch';
  const mock = scan.execution_mode === 'mock';
  const reclustered = supersedingRevision(scan);

  return (
    <div
      className="space-y-4"
      data-testid="competitor-scan-progress"
      data-status={scan.status}
      data-mode={scan.execution_mode}
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className="rounded bg-gray-100 px-2 py-0.5 font-medium"
          data-testid="competitor-scan-status"
        >
          {t(`scan-status-${scan.status}`)}
        </span>
        {mock && <SimulatedBadge />}
        <span className="text-gray-600">
          {countryName(locale, scan.country)}
          {scan.created_at &&
            ` · ${formatDateTime(locale, scan.created_at, {
              dateStyle: 'medium',
              timeStyle: 'short',
            })}`}
        </span>
      </div>
      {scan.waits > 0 && active && (
        <p className="text-sm text-gray-600" role="status">
          {t('turn-waiting')}
        </p>
      )}

      <ol className="grid gap-1 text-sm sm:grid-cols-2">
        {STEPS.map((name) => {
          const step = (scan.steps ?? []).find((s) => s.step === name);
          const status = step?.status ?? 'pending';
          const superseded = name === 'cluster' && reclustered != null;
          return (
            <li
              key={name}
              className="flex items-start gap-2"
              data-testid={`competitor-scan-step-${name}`}
              data-status={status}
              data-superseded={superseded ? 'true' : undefined}
            >
              <StepIcon status={superseded ? 'completed' : status} />
              <span className="min-w-0">
                <span className="font-medium">{t(`step-${name}`)}</span>
                <span className="text-gray-600">
                  {' · '}
                  {superseded
                    ? t('step-cluster-superseded', { revision: reclustered })
                    : step?.status === 'skipped' &&
                        step.reason === 'not_implemented'
                      ? t('step-not-implemented')
                      : t(`step-status-${status}`)}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      {(scan.targets ?? []).length > 0 && (
        <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white text-sm">
          {(scan.targets ?? []).map((target) => (
            <TargetRow
              key={target.origin}
              target={target}
              country={scan.country}
              onChangePage={
                onChangePage &&
                target.competitor_id &&
                listedCompetitorIds.includes(target.competitor_id)
                  ? () => onChangePage(target.competitor_id!)
                  : undefined
              }
            />
          ))}
        </ul>
      )}

      <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-gray-500">{t('scan-observations')}</dt>
          <dd data-testid="competitor-scan-observations">
            {formatCount(locale, scan.observation_count)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">{t('scan-media')}</dt>
          <dd>
            {t('scan-media-value', {
              available: formatCount(locale, scan.media?.available ?? 0),
              planned: formatCount(locale, scan.media?.planned ?? 0),
              unavailable: formatCount(locale, scan.media?.unavailable ?? 0),
            })}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-gray-500">{t('scan-cost')}</dt>
          <dd data-testid="competitor-scan-cost">
            {scan.cost
              ? mock
                ? t('scan-cost-mock', {
                    estimate: scan.cost.simulated_usd
                      ? formatUsd(locale, scan.cost.simulated_usd)
                      : t('quote-no-estimate'),
                  })
                : t('scan-cost-value', {
                    credits: formatCount(
                      locale,
                      scan.cost.market_data.credits_charged
                    ),
                    data: formatUsd(locale, scan.cost.market_data.usd),
                    llm: formatUsd(locale, scan.cost.llm.usd),
                  })
              : t('scan-cost-none')}
          </dd>
        </div>
      </dl>

      {scan.status === 'completed' && (
        <p className="text-sm text-green-800" role="status">
          {t('scan-completed', {
            count: scan.observation_count,
          })}
        </p>
      )}
      {scan.status === 'partial' && (
        <Alert tone="warning">
          <p className="font-medium">{t('scan-partial')}</p>
          {scan.failure && (
            <p>{codeMessage(t, scan.failure.code, scan.failure.params)}</p>
          )}
        </Alert>
      )}
      {scan.status === 'failed' && (
        <Alert tone="error">
          <p className="font-medium" data-testid="competitor-scan-failed-step">
            {t('scan-failed', { step: t(`step-${failedStep}`) })}
          </p>
          <p>
            {codeMessage(
              t,
              scan.failure?.code ?? 'competitor_scan_failed',
              scan.failure?.params
            )}
          </p>
        </Alert>
      )}
      {scan.status === 'uncertain' && (
        <Reconciliation scan={scan} onChanged={onChanged} />
      )}
      {scan.can_resume && !active && (
        <ResumeOffer scan={scan} onChanged={onChanged} />
      )}
      {(scan.resumes ?? []).length > 0 && (
        <p className="text-xs text-gray-500">
          {t('scan-resumed', { count: (scan.resumes ?? []).length })}
        </p>
      )}
    </div>
  );
}

function StepIcon({ status }: { status: string }) {
  const className = 'mt-0.5 h-4 w-4 shrink-0';
  if (status === 'completed')
    return (
      <CheckCircleIcon
        className={`${className} text-green-700`}
        aria-hidden="true"
      />
    );
  if (status === 'failed')
    return (
      <XCircleIcon className={`${className} text-red-700`} aria-hidden="true" />
    );
  if (status === 'partial')
    return (
      <ExclamationTriangleIcon
        className={`${className} text-amber-700`}
        aria-hidden="true"
      />
    );
  if (status === 'running')
    return (
      <span
        className={`${className} animate-spin rounded-full border-2 border-gray-300 border-t-gray-900`}
        aria-hidden="true"
      />
    );
  return (
    <MinusCircleIcon
      className={`${className} text-gray-400`}
      aria-hidden="true"
    />
  );
}

/**
 * A wrong page ID and a page with no ads running both answer an empty page
 * (#355); only a page no scan has named can be the first.
 */
function maybeWrongPage(target: Target) {
  return (
    target.kind === 'page' &&
    target.status === 'completed' &&
    target.ads_seen === 0 &&
    !!target.page_id &&
    !target.page_name &&
    !target.observed_page_name
  );
}

function TargetRow({
  target,
  country,
  onChangePage,
}: {
  target: Target;
  country: string;
  onChangePage?: () => void;
}) {
  const { t, i18n } = useTranslation('competitors');
  return (
    <li
      className="flex flex-col gap-1 p-3 sm:flex-row sm:items-start sm:justify-between"
      data-testid="competitor-scan-target"
      data-status={target.status}
    >
      <div className="min-w-0">
        <p className="break-words font-medium">
          {target.kind === 'keyword'
            ? t('target-keyword', { keyword: target.keyword ?? '' })
            : target.page_name || target.observed_page_name || target.page_id}
          {target.role === 'self' && (
            <span className="ml-2 text-xs font-normal text-gray-500">
              {t('role-self')}
            </span>
          )}
        </p>
        {target.failure && (
          <p className="text-red-700">
            {codeMessage(t, target.failure.code, target.failure.params)}
          </p>
        )}
        {maybeWrongPage(target) && (
          <div
            className="mt-1 space-y-1 text-amber-900"
            data-testid="competitor-scan-target-empty"
          >
            <p>{t('target-empty-page')}</p>
            <p className="flex flex-wrap gap-x-3 gap-y-1">
              <a
                className="underline"
                href={pageLibraryUrl(target.page_id!, country)}
                target="_blank"
                rel="noopener noreferrer"
              >
                {t('target-empty-library')}
              </a>
              {onChangePage && (
                <button
                  type="button"
                  className="underline"
                  onClick={onChangePage}
                >
                  {t('row-change-page')}
                </button>
              )}
            </p>
          </div>
        )}
      </div>
      <p className="shrink-0 text-gray-600">
        {t(`target-status-${target.status}`)}
        {' · '}
        {t('target-progress', {
          fetched: target.pages_fetched,
          planned: target.pages_planned,
          ads: formatCount(i18n.language, target.ads_seen),
        })}
      </p>
    </li>
  );
}

function Alert({
  tone,
  children,
}: {
  tone: 'warning' | 'error';
  children: React.ReactNode;
}) {
  return (
    <div
      role="alert"
      className={`space-y-1 rounded-lg border p-3 text-sm ${
        tone === 'error'
          ? 'border-red-200 bg-red-50 text-red-900'
          : 'border-amber-200 bg-amber-50 text-amber-900'
      }`}
    >
      {children}
    </div>
  );
}

/** "Resume" with what it would reserve: only the remaining pages and steps. */
function ResumeOffer({
  scan,
  onChanged,
}: {
  scan: Scan;
  onChanged: (scan?: Scan) => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  // Re-quoted after every resume and reconciliation; never while a recluster
  // runs, which the backend refuses to quote (#355).
  const quote = useSWR(
    activeRecluster(scan)
      ? null
      : [
          'competitor-scan-resume-quote',
          current.workspaceId,
          scan.scan_id,
          scan.status,
          (scan.resumes ?? []).length,
          (scan.reconciliation ?? []).filter((r) => r.status !== 'pending')
            .length,
        ],
    async () =>
      dataOf(
        await gateway.quoteCompetitorScanResume({
          workspaceSlug: current.slug,
          scanId: scan.scan_id,
        })
      )
  );

  async function resume() {
    const resumed = await command.run(
      `resume:${scan.scan_id}:${(scan.resumes ?? []).length}`,
      async (action) =>
        dataOf(
          await gateway.resumeCompetitorScan({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            scanId: scan.scan_id,
          })
        )
    );
    if (resumed) await onChanged(resumed);
  }

  return (
    <div className="space-y-3" data-testid="competitor-scan-resume">
      <h4 className="text-sm font-medium">{t('resume-title')}</h4>
      <p className="text-sm text-gray-600">
        {scan.status === 'uncertain'
          ? t('resume-intro-uncertain')
          : t('resume-intro')}
      </p>
      {quote.error && (
        <p className="text-sm text-red-700" role="alert">
          {problemMessage(t, quote.error)}
        </p>
      )}
      {!quote.data && !quote.error && (
        <p className="text-sm text-gray-500" role="status">
          {t('quote-loading')}
        </p>
      )}
      {quote.data && (
        <>
          <ScanQuote quote={quote.data} />
          <button
            type="button"
            className={BUTTON}
            disabled={!!command.busy}
            onClick={() => void resume()}
            data-testid="competitor-scan-resume-button"
          >
            {quote.data.execution_mode === 'mock'
              ? t('resume-start-mock')
              : t('resume-start', {
                  usd: formatUsd(i18n.language, quote.data.max_usd),
                })}
          </button>
        </>
      )}
      {command.errorFor('resume:') && (
        <p className="text-sm text-red-700" role="alert">
          {command.errorFor('resume:')}
        </p>
      )}
    </div>
  );
}

/**
 * Pages whose outcome was lost after the dispatch fence (spec §5.1). The
 * automatic check compares the free credit balance; anything it cannot tell
 * is left to the Owner, whose verdict only settles the cost — the page is
 * fetched again on resume.
 */
function Reconciliation({
  scan,
  onChanged,
}: {
  scan: Scan;
  onChanged: (scan?: Scan) => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const originLabel = useOriginLabel(scan);
  const isOwner = current.role === 'OWNER';
  const entries = scan.reconciliation ?? [];

  async function mark(attemptId: string, outcome: 'charged' | 'not_charged') {
    const updated = await command.run(
      `reconcile:${scan.scan_id}:${attemptId}:${outcome}`,
      async (action) =>
        dataOf(
          await gateway.reconcileCompetitorScan({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            scanId: scan.scan_id,
            body: { attempt_id: attemptId, outcome },
          })
        )
    );
    if (updated) await onChanged(updated);
  }

  return (
    <div
      className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950"
      data-testid="competitor-scan-reconciliation"
    >
      <p className="font-medium">{t('reconcile-title')}</p>
      <p>{t('reconcile-intro')}</p>
      <ul className="space-y-3">
        {entries.map((entry) => {
          const check = entry.balance_check;
          return (
            <li
              key={entry.attempt_id}
              className="space-y-1 rounded border border-amber-200 bg-white p-3"
              data-testid="competitor-scan-reconciliation-entry"
              data-status={entry.status}
            >
              <p className="font-medium">
                {t('reconcile-page', {
                  origin: originLabel(entry.origin),
                  page: entry.page,
                })}
              </p>
              <p>
                {t(`reconcile-status-${entry.status}`)}
                {entry.source && ` · ${t(`reconcile-source-${entry.source}`)}`}
                {entry.decided_at &&
                  ` · ${formatDateTime(i18n.language, entry.decided_at, {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                  })}`}
              </p>
              {check && (
                <p
                  className="text-gray-700"
                  data-testid="competitor-scan-balance-check"
                  data-outcome={check.outcome}
                >
                  {t(`reconcile-check-${check.outcome}`)}
                  {check.reason &&
                    ` ${t(`reconcile-reason-${check.reason}`, {
                      difference: check.difference ?? '',
                    })}`}
                </p>
              )}
              {entry.status === 'pending' &&
                (isOwner ? (
                  <div className="flex flex-wrap gap-2 pt-1">
                    <button
                      type="button"
                      className={SECONDARY}
                      disabled={!!command.busy}
                      onClick={() => void mark(entry.attempt_id, 'charged')}
                    >
                      {t('reconcile-mark-charged')}
                    </button>
                    <button
                      type="button"
                      className={SECONDARY}
                      disabled={!!command.busy}
                      onClick={() => void mark(entry.attempt_id, 'not_charged')}
                    >
                      {t('reconcile-mark-not-charged')}
                    </button>
                  </div>
                ) : (
                  <p className="text-gray-600">{t('reconcile-owner-only')}</p>
                ))}
            </li>
          );
        })}
      </ul>
      {command.errorFor('reconcile:') && (
        <p className="text-red-700" role="alert">
          {command.errorFor('reconcile:')}
        </p>
      )}
    </div>
  );
}
