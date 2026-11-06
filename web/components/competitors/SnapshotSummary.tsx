import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useRouter } from 'next/router';
import useSWR from 'swr';
import { InformationCircleIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { formatDateTime } from '@/lib/i18n/format';
import { libraryUrlOf } from './AdThumbnail';
import { SimulatedBadge } from './CredentialNotice';
import { DraftingSwitch } from './DraftingSwitch';
import {
  ACTIVE_STATUSES,
  codeMessage,
  dataOf,
  formatCount,
  formatUsd,
  problemMessage,
  useCommand,
  type CompetitorScan,
  type CompetitorSnapshotAd,
  type CompetitorSnapshotSummary,
} from './shared';

type MarketingProjectDetail = components['schemas']['MarketingProjectDetail'];

const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';

/**
 * Whether the plan on screen cites an older snapshot than the newest one of
 * its product and market (spec §7.2; `MarketingProjectDetail`'s
 * `competitor_snapshot_stale`, #273). This reads the projection the
 * workspace already loaded with plan review — the same SWR entry, no
 * request of its own — and a projection without the field shows no hint.
 */
export function useSnapshotStale() {
  const router = useRouter();
  const { current } = useWorkspace();
  const projectId =
    typeof router.query.test === 'string' ? router.query.test : null;
  const project = useSWR<
    Pick<MarketingProjectDetail, 'competitor_snapshot_stale'>
  >(
    projectId ? ['marketing-project', current.workspaceId, projectId] : null,
    null
  );
  return project.data?.competitor_snapshot_stale === true;
}

/** The newest recluster, while it is queued or running. */
export function activeRecluster(scan: CompetitorScan) {
  const latest = (scan.reclusters ?? []).at(-1);
  return latest && ACTIVE_STATUSES.includes(latest.status) ? latest : null;
}

/**
 * The summary of the scan's current snapshot (spec §9): the buying claims
 * with how many advertisers and ads say them and the longest run, the
 * relevant ads no cluster holds yet, adjacent, long-running and own-page
 * ads, and the AI's one-sentence conclusion. Clusters and the conclusion are
 * AI inference. "Refreeze" (free, no AI) and "Recluster" (paid, quoted
 * first) act on the scan; the drafting switch acts on the product.
 */
export function SnapshotSummary({
  scan,
  productId,
  summary,
  summaryError,
  onRefrozen,
  onScanChanged,
}: {
  scan: CompetitorScan;
  productId: string;
  summary: CompetitorSnapshotSummary | null;
  summaryError: unknown;
  onRefrozen: () => Promise<unknown>;
  onScanChanged: (scan?: CompetitorScan) => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const locale = i18n.language;
  const stale = useSnapshotStale();

  return (
    <section
      className="space-y-4 rounded-lg border border-gray-200 bg-gray-50 p-3 sm:p-4"
      data-testid="competitor-summary"
      data-snapshot-id={summary?.snapshot_id ?? ''}
      aria-labelledby="competitor-summary-title"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h4 id="competitor-summary-title" className="text-base font-semibold">
          {t('summary-title')}
        </h4>
        {summary?.mock && <SimulatedBadge />}
        {summary?.created_at && (
          <span className="text-xs text-gray-500">
            {t('summary-frozen-at', {
              at: formatDateTime(locale, summary.created_at, {
                dateStyle: 'medium',
                timeStyle: 'short',
              }),
            })}
          </span>
        )}
      </div>
      <p className="text-sm text-gray-600">{t('summary-intro')}</p>

      {stale && (
        <p
          className="flex items-start gap-2 rounded-lg border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900"
          role="status"
          data-testid="competitor-snapshot-stale"
        >
          <InformationCircleIcon
            className="h-5 w-5 shrink-0"
            aria-hidden="true"
          />
          <span>{t('summary-stale')}</span>
        </p>
      )}

      {summaryError ? (
        <p className="text-sm text-red-700" role="alert">
          {problemMessage(t, summaryError)}
        </p>
      ) : null}
      {!summary && !summaryError && scan.snapshot_id && (
        <p className="text-sm text-gray-500" role="status">
          {t('summary-loading')}
        </p>
      )}
      {!summary && !summaryError && !scan.snapshot_id && (
        <p
          className="text-sm text-gray-600"
          data-testid="competitor-summary-empty"
        >
          {ACTIVE_STATUSES.includes(scan.status)
            ? t('summary-waiting')
            : t('summary-none')}
        </p>
      )}
      {summary?.pending_changes && (
        <p
          className="text-sm text-amber-900"
          role="status"
          data-testid="competitor-summary-pending"
        >
          {t('summary-pending')}
        </p>
      )}

      <SnapshotActions
        scan={scan}
        summary={summary}
        onRefrozen={onRefrozen}
        onScanChanged={onScanChanged}
      />

      {summary && <SummaryBody summary={summary} />}

      <DraftingSwitch productId={productId} />
    </section>
  );
}

function SummaryBody({ summary }: { summary: CompetitorSnapshotSummary }) {
  const { t, i18n } = useTranslation('competitors');
  const locale = i18n.language;
  const counts = summary.counts;
  const sources = summary.relevance_sources;

  return (
    <div className="space-y-4">
      {summary.conclusion && (
        <div className="space-y-1" data-testid="competitor-summary-conclusion">
          <AiInference />
          <p className="break-words text-sm">{summary.conclusion.text}</p>
        </div>
      )}

      {counts && (
        <dl
          className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-5"
          data-testid="competitor-summary-counts"
        >
          {(
            [
              'observations',
              'relevant',
              'adjacent',
              'unrelated',
              'unassessed',
            ] as const
          ).map((name) => (
            <div key={name}>
              <dt className="text-xs text-gray-500">
                {t(`summary-count-${name}`)}
              </dt>
              <dd data-testid={`competitor-summary-count-${name}`}>
                {formatCount(locale, counts[name])}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {sources && (counts?.relevant ?? 0) > 0 && (
        <p
          className="text-xs text-gray-600"
          data-testid="competitor-summary-sources"
        >
          {t('summary-sources', {
            ai: formatCount(locale, sources.ai_inferred),
            other: formatCount(locale, sources.non_ai),
          })}
        </p>
      )}

      <div className="space-y-2">
        <h5 className="text-sm font-semibold">{t('summary-clusters-title')}</h5>
        {(summary.clusters ?? []).length === 0 ? (
          <p className="text-sm text-gray-600">{t('summary-clusters-none')}</p>
        ) : (
          <ul className="space-y-2">
            {(summary.clusters ?? []).map((cluster) => (
              <li
                key={cluster.cluster_id}
                className="space-y-1 rounded-lg border border-gray-200 bg-white p-3 text-sm"
                data-testid="competitor-summary-cluster"
                data-cluster-id={cluster.cluster_id}
                data-advertisers={cluster.advertiser_count}
                data-observations={cluster.observation_count}
              >
                <div className="flex flex-wrap items-center gap-2">
                  {cluster.ai_inference && <AiInference />}
                  {cluster.self_included && (
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-700">
                      {t('summary-cluster-self')}
                    </span>
                  )}
                </div>
                <p className="break-words font-medium">{cluster.claim}</p>
                <p className="text-gray-600">
                  {t('summary-cluster-stats', {
                    advertisers: formatCount(locale, cluster.advertiser_count),
                    ads: formatCount(locale, cluster.observation_count),
                  })}
                  {cluster.max_days_running != null &&
                    ` · ${t('summary-cluster-longest', {
                      count: cluster.max_days_running,
                    })}`}
                </p>
                {(cluster.advertisers ?? []).length > 0 && (
                  <p className="break-words text-xs text-gray-600">
                    {(cluster.advertisers ?? [])
                      .map((advertiser) =>
                        advertiser.role === 'self'
                          ? t('summary-advertiser-self', {
                              name:
                                advertiser.advertiser_name ??
                                advertiser.advertiser_page_id ??
                                '',
                            })
                          : (advertiser.advertiser_name ??
                            advertiser.advertiser_page_id ??
                            '')
                      )
                      .join(t('card-tags-separator'))}
                  </p>
                )}
                {(cluster.ads ?? []).length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-gray-600">
                      {t('summary-cluster-ads')}
                    </summary>
                    <SnapshotAdList ads={cluster.ads ?? []} />
                  </details>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <SnapshotAdSection
        id="unclustered"
        title={t('summary-unclustered-title')}
        hint={t('summary-unclustered-hint')}
        ads={summary.unclustered ?? []}
      />
      <SnapshotAdSection
        id="adjacent"
        title={t('summary-adjacent-title')}
        hint={t('summary-adjacent-hint')}
        ads={summary.adjacent ?? []}
      />
      <SnapshotAdSection
        id="long-running"
        title={t('summary-long-running-title', {
          days: summary.long_running_days,
        })}
        hint={t('summary-long-running-hint')}
        ads={summary.long_running ?? []}
      />
      <SnapshotAdSection
        id="self"
        title={t('summary-self-title')}
        hint={t('summary-self-hint')}
        ads={summary.self_observations ?? []}
      />

      {(summary.dimension_counts ?? []).length > 0 && (
        <details
          className="text-sm"
          data-testid="competitor-summary-dimensions"
        >
          <summary className="cursor-pointer font-semibold">
            {t('summary-dimensions-title')}
          </summary>
          <p className="mt-1 text-xs text-gray-600">
            {t('summary-dimensions-hint')}
          </p>
          <ul className="mt-2 space-y-2">
            {(summary.dimension_counts ?? []).map((factor) => (
              <li key={factor.factor_id}>
                <p className="break-words text-xs font-medium">
                  {factor.label}
                </p>
                <ul className="mt-0.5 space-y-0.5 text-xs text-gray-600">
                  {(factor.values ?? [])
                    .filter((value) => value.confirmed + value.suggested > 0)
                    .map((value) => (
                      <li key={value.value_id} className="break-words">
                        {t('summary-dimension-value', {
                          value: value.label,
                          confirmed: formatCount(locale, value.confirmed),
                          suggested: formatCount(locale, value.suggested),
                        })}
                      </li>
                    ))}
                </ul>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function AiInference() {
  const { t } = useTranslation('competitors');
  return (
    <span
      className="inline-flex rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-900"
      data-testid="competitor-ai-inference"
    >
      {t('card-source-ai')}
    </span>
  );
}

function SnapshotAdSection({
  id,
  title,
  hint,
  ads,
}: {
  id: string;
  title: string;
  hint: string;
  ads: CompetitorSnapshotAd[];
}) {
  const { i18n } = useTranslation('competitors');
  return (
    <div
      className="space-y-1"
      data-testid={`competitor-summary-${id}`}
      data-count={ads.length}
    >
      <h5 className="text-sm font-semibold">
        {title}
        <span className="ml-2 font-normal text-gray-500">
          {formatCount(i18n.language, ads.length)}
        </span>
      </h5>
      <p className="text-xs text-gray-600">{hint}</p>
      {ads.length > 0 && <SnapshotAdList ads={ads} />}
    </div>
  );
}

/** A summary list shows its first few ads until asked for all of them. */
const LIST_PREVIEW = 5;

function SnapshotAdList({ ads }: { ads: CompetitorSnapshotAd[] }) {
  const { t } = useTranslation('competitors');
  const [all, setAll] = useState(false);
  const shown = all ? ads : ads.slice(0, LIST_PREVIEW);
  return (
    <>
      <ul className="mt-1 space-y-1 text-xs">
        {shown.map((ad) => {
          const library = libraryUrlOf(ad);
          return (
            <li
              key={ad.ad_id}
              className="break-words"
              data-testid="competitor-summary-ad"
              data-ad-id={ad.ad_id}
            >
              <span className="font-medium">
                {ad.advertiser_name ?? ad.advertiser_page_id ?? ''}
              </span>
              {ad.days_running != null &&
                ` · ${t('card-days-running', { count: ad.days_running })}`}
              {ad.title && ` · ${ad.title}`}
              {ad.ai_inferred && ` · ${t('card-source-ai')}`}
              {library && (
                <>
                  {' · '}
                  <a
                    href={library}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline"
                  >
                    {t('card-library-link')}
                  </a>
                </>
              )}
            </li>
          );
        })}
      </ul>
      {ads.length > LIST_PREVIEW && (
        <button
          type="button"
          className="text-xs underline"
          aria-expanded={all}
          onClick={() => setAll((value) => !value)}
          data-testid="competitor-summary-ads-toggle"
        >
          {all
            ? t('summary-ads-show-fewer')
            : t('summary-ads-show-all', { count: ads.length })}
        </button>
      )}
    </>
  );
}

/** "Refreeze" (free, deterministic) and "Recluster" (paid, quoted first). */
function SnapshotActions({
  scan,
  summary,
  onRefrozen,
  onScanChanged,
}: {
  scan: CompetitorScan;
  summary: CompetitorSnapshotSummary | null;
  onRefrozen: () => Promise<unknown>;
  onScanChanged: (scan?: CompetitorScan) => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const [refrozen, setRefrozen] = useState<'created' | 'unchanged' | null>(
    null
  );
  const [quoting, setQuoting] = useState(false);
  const reclustering = activeRecluster(scan);
  const busy = ACTIVE_STATUSES.includes(scan.status) || !!reclustering;
  const latest = (scan.reclusters ?? []).at(-1);

  // Not while a recluster runs: the confirmed one's answer lands before
  // `quoting` turns off, and the backend refuses that quote (#355).
  const quote = useSWR(
    quoting && !busy
      ? [
          'competitor-scan-recluster-quote',
          current.workspaceId,
          scan.scan_id,
          (scan.reclusters ?? []).length,
        ]
      : null,
    async () =>
      dataOf(
        await gateway.quoteCompetitorScanRecluster({
          workspaceSlug: current.slug,
          scanId: scan.scan_id,
        })
      )
  );

  async function refreeze() {
    setRefrozen(null);
    const snapshot = await command.run(
      `refreeze:${scan.scan_id}`,
      async (action) =>
        dataOf(
          await gateway.refreezeCompetitorScan({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            scanId: scan.scan_id,
          })
        )
    );
    if (!snapshot) return;
    setRefrozen(snapshot.created ? 'created' : 'unchanged');
    await onRefrozen();
  }

  async function recluster() {
    const updated = await command.run(
      `recluster:${scan.scan_id}:${(scan.reclusters ?? []).length}`,
      async (action) =>
        dataOf(
          await gateway.reclusterCompetitorScan({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            scanId: scan.scan_id,
          })
        )
    );
    if (!updated) return;
    setQuoting(false);
    await onScanChanged(updated);
  }

  const mock = quote.data?.execution_mode === 'mock';

  return (
    <div className="space-y-3" data-testid="competitor-snapshot-actions">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={SECONDARY}
          disabled={busy || !!command.busy}
          onClick={() => void refreeze()}
          data-testid="competitor-refreeze"
        >
          {t('summary-refreeze')}
        </button>
        <button
          type="button"
          className={SECONDARY}
          disabled={busy || !!command.busy || quoting}
          onClick={() => setQuoting(true)}
          data-testid="competitor-recluster"
        >
          {t('summary-recluster')}
        </button>
      </div>
      <p className="text-xs text-gray-500">{t('summary-actions-note')}</p>
      {refrozen && (
        <p
          className="text-sm text-green-800"
          role="status"
          data-testid="competitor-refreeze-result"
          data-created={refrozen === 'created'}
        >
          {refrozen === 'created'
            ? t('summary-refreeze-created')
            : t('summary-refreeze-unchanged')}
        </p>
      )}
      {command.errorFor('refreeze:') && (
        <p
          className="text-sm text-red-700"
          role="alert"
          data-testid="competitor-refreeze-error"
        >
          {command.errorFor('refreeze:')}
        </p>
      )}

      {quoting && !busy && (
        <div
          className="space-y-3 rounded-lg border border-gray-200 bg-white p-3 text-sm"
          data-testid="competitor-recluster-quote"
          data-mode={quote.data?.execution_mode ?? ''}
        >
          <p className="font-medium">{t('summary-recluster-title')}</p>
          <p className="text-gray-600">{t('summary-recluster-intro')}</p>
          {quote.error && (
            <p className="text-red-700" role="alert">
              {problemMessage(t, quote.error)}
            </p>
          )}
          {!quote.data && !quote.error && (
            <p className="text-gray-500" role="status">
              {t('quote-loading')}
            </p>
          )}
          {quote.data && (
            <>
              {mock && (
                <p className="flex flex-wrap items-center gap-2">
                  <SimulatedBadge />
                  <span className="text-gray-700">{t('quote-mock-note')}</span>
                </p>
              )}
              <p className="flex flex-wrap justify-between gap-x-3 font-medium">
                <span>{t('quote-llm-line')}</span>
                <span data-testid="competitor-recluster-quote-total">
                  {mock
                    ? t('quote-mock-amount', {
                        estimate: quote.data.simulated_usd
                          ? formatUsd(i18n.language, quote.data.simulated_usd)
                          : t('quote-no-estimate'),
                      })
                    : t('quote-llm-amount', {
                        usd: formatUsd(i18n.language, quote.data.max_usd),
                      })}
                </span>
              </p>
              <p className="text-gray-600">
                {t('quote-llm-detail', {
                  calls: quote.data.llm.calls,
                  model: quote.data.llm.model,
                })}
                {' · '}
                {t('summary-recluster-relevant', {
                  count: quote.data.relevant_count,
                })}
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={!!command.busy}
                  onClick={() => void recluster()}
                  data-testid="competitor-recluster-confirm"
                >
                  {mock
                    ? t('summary-recluster-start-mock')
                    : t('summary-recluster-start', {
                        usd: formatUsd(i18n.language, quote.data.max_usd),
                      })}
                </button>
                <button
                  type="button"
                  className={SECONDARY}
                  onClick={() => setQuoting(false)}
                >
                  {t('close')}
                </button>
              </div>
            </>
          )}
          {command.errorFor('recluster:') && (
            <p className="text-red-700" role="alert">
              {command.errorFor('recluster:')}
            </p>
          )}
        </div>
      )}

      {latest && (
        <p
          className={`text-sm ${
            latest.status === 'failed' ? 'text-red-700' : 'text-gray-600'
          }`}
          role="status"
          data-testid="competitor-recluster-status"
          data-status={latest.status}
        >
          {t(`summary-recluster-status-${latest.status}`)}
          {latest.status === 'completed' &&
            latest.cluster_revision != null &&
            summary?.cluster_revision === latest.cluster_revision &&
            ` ${t('summary-recluster-shown', {
              revision: latest.cluster_revision,
            })}`}
          {latest.status === 'failed' &&
            latest.failure &&
            ` ${codeMessage(
              t,
              typeof latest.failure.code === 'string'
                ? latest.failure.code
                : null,
              (latest.failure.params as Record<string, unknown>) ?? null
            )}`}
        </p>
      )}
    </div>
  );
}
