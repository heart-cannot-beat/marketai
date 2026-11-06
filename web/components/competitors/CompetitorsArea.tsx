import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useRouter } from 'next/router';
import useSWR, { useSWRConfig } from 'swr';
import { ChevronDownIcon, ChevronUpIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { formatDateTime } from '@/lib/i18n/format';
import { AddCompetitorForm } from './AddCompetitorForm';
import { CompetitorBoard } from './CompetitorBoard';
import { CompetitorDiscovery } from './CompetitorDiscovery';
import { CompetitorRow } from './CompetitorRow';
import { SimulatedBadge } from './CredentialNotice';
import { countryFromMarket } from './market';
import { ScanProgress } from './ScanProgress';
import { ScanSetup } from './ScanSetup';
import { activeRecluster } from './SnapshotSummary';
import {
  ACTIVE_STATUSES,
  POLL_MS,
  dataOf,
  formatCount,
  type CompetitorScan,
} from './shared';

/** The query parameter that keeps the scan on screen across reloads. */
export const SCAN_QUERY = 'competitor_scan';

const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';

/**
 * The product understanding page's "Competitors" area (spec §9): the list
 * and its confirmations, then scans. Observations only show what others say
 * (ADR 0014). Below the scan on screen, its board and summary (C-32), then
 * the advertisers its keywords found (C-33, a scan with keywords only).
 */
export function CompetitorsArea({
  productId,
  planMarket,
  defaultOpen,
}: {
  productId: string | null;
  planMarket: string | null;
  defaultOpen: boolean;
}) {
  const { t } = useTranslation('competitors');
  const [open, setOpen] = useState(defaultOpen);

  return (
    <section
      id="competitors"
      className="max-w-3xl space-y-4 rounded-xl border border-gray-200 p-4 sm:p-5"
      data-testid="competitors-area"
      aria-labelledby="competitors-title"
    >
      <button
        type="button"
        className="flex w-full items-start justify-between gap-3 text-left"
        aria-expanded={open}
        aria-controls="competitors-body"
        onClick={() => setOpen((value) => !value)}
      >
        <span>
          <h2 id="competitors-title" className="text-xl font-semibold">
            {t('area-title')}
          </h2>
          <span className="mt-1 block text-sm text-gray-600">
            {t('area-intro')}
          </span>
        </span>
        {open ? (
          <ChevronUpIcon className="mt-1 h-5 w-5 shrink-0" aria-hidden="true" />
        ) : (
          <ChevronDownIcon
            className="mt-1 h-5 w-5 shrink-0"
            aria-hidden="true"
          />
        )}
      </button>
      {open && (
        <div id="competitors-body" className="space-y-6">
          {productId ? (
            <CompetitorsBody productId={productId} planMarket={planMarket} />
          ) : (
            <p className="text-sm text-gray-600">{t('area-needs-product')}</p>
          )}
        </div>
      )}
    </section>
  );
}

function CompetitorsBody({
  productId,
  planMarket,
}: {
  productId: string;
  planMarket: string | null;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const router = useRouter();
  const { mutate } = useSWRConfig();
  const planCountry = countryFromMarket(planMarket);
  const [country, setCountry] = useState(planCountry ?? 'US');
  // A plan market edited later moves the default along with it.
  const [seenPlanCountry, setSeenPlanCountry] = useState(planCountry);
  if (planCountry !== seenPlanCountry) {
    setSeenPlanCountry(planCountry);
    if (planCountry) setCountry(planCountry);
  }
  const [openRow, setOpenRow] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);

  const competitors = useSWR(
    ['competitors', current.workspaceId, productId],
    async () =>
      dataOf(
        await gateway.listCompetitors({
          workspaceSlug: current.slug,
          productId,
          status: 'active',
        })
      ),
    {
      refreshInterval: (latest) =>
        latest?.items?.some(
          (c) =>
            c.latest_resolution &&
            ACTIVE_STATUSES.includes(c.latest_resolution.status)
        )
          ? POLL_MS
          : 0,
    }
  );
  const scans = useSWR(
    ['competitor-scans', current.workspaceId, productId],
    async () =>
      dataOf(
        await gateway.listCompetitorScans({
          workspaceSlug: current.slug,
          productId,
        })
      )
  );
  const routed =
    typeof router.query[SCAN_QUERY] === 'string'
      ? (router.query[SCAN_QUERY] as string)
      : null;
  // The scan just started or picked shows at once; the URL catches up.
  const [chosen, setChosen] = useState<string | null>(null);
  const [seenRouted, setSeenRouted] = useState(routed);
  if (routed !== seenRouted) {
    setSeenRouted(routed);
    setChosen(null);
  }
  const scanId = chosen ?? routed ?? scans.data?.items?.[0]?.scan_id ?? null;
  const scan = useSWR(
    scanId ? ['competitor-scan', current.workspaceId, scanId] : null,
    async () =>
      dataOf(
        await gateway.getCompetitorScan({
          workspaceSlug: current.slug,
          scanId: scanId!,
        })
      ),
    {
      // A recluster runs as its own Job while the scan stays finished.
      refreshInterval: (latest) =>
        latest &&
        (ACTIVE_STATUSES.includes(latest.status) || activeRecluster(latest))
          ? POLL_MS
          : 0,
    }
  );
  // The list's status follows the scan being watched; so do the competitors'
  // last scan and the page names that scan observed.
  const watchedStatus = scan.data?.status;
  const [listedStatus, setListedStatus] = useState(watchedStatus);
  if (watchedStatus !== listedStatus) {
    setListedStatus(watchedStatus);
    if (listedStatus) {
      void scans.mutate();
      void competitors.mutate();
    }
  }

  const items = competitors.data?.items ?? [];
  const scanItems = scans.data?.items ?? [];
  const confirmed = items.filter((c) => c.confirmation_status === 'confirmed');
  const showSetup = confirmed.length > 0 && (setupOpen || !scanId);

  async function selectScan(id: string | null) {
    setChosen(id);
    const query = { ...router.query };
    if (id) query[SCAN_QUERY] = id;
    else delete query[SCAN_QUERY];
    await router.replace({ pathname: router.pathname, query }, undefined, {
      shallow: true,
      scroll: false,
    });
  }

  /** "Change page" from a scan target: that competitor's row, opened. */
  function changePage(competitorId: string) {
    setOpenRow(competitorId);
    document
      .querySelector(
        `[data-testid="competitor-row"][data-competitor-id="${competitorId}"]`
      )
      ?.scrollIntoView?.({ block: 'center' });
  }

  async function started(created: CompetitorScan) {
    setSetupOpen(false);
    await mutate(
      ['competitor-scan', current.workspaceId, created.scan_id],
      created,
      {
        revalidate: false,
      }
    );
    await selectScan(created.scan_id);
    await scans.mutate();
  }

  return (
    <>
      <div className="space-y-3">
        <h3 className="text-base font-semibold">{t('list-title')}</h3>
        <AddCompetitorForm
          productId={productId}
          onAdded={async (added) => {
            await competitors.mutate();
            if (added.confirmation_status !== 'confirmed')
              setOpenRow(added.competitor_id);
          }}
        />
        {competitors.error && (
          <p className="text-sm text-red-700" role="alert">
            {t('list-error')}
          </p>
        )}
        {!competitors.data && !competitors.error && (
          <p className="text-sm text-gray-500" role="status">
            {t('list-loading')}
          </p>
        )}
        {competitors.data && items.length === 0 && (
          <p className="text-sm text-gray-600">{t('list-empty')}</p>
        )}
        {items.length > 0 && (
          <ul
            className="divide-y divide-gray-100"
            data-testid="competitor-list"
          >
            {items.map((competitor) => (
              <CompetitorRow
                key={competitor.competitor_id}
                competitor={competitor}
                country={country}
                open={openRow === competitor.competitor_id}
                onToggle={(next) =>
                  setOpenRow(next ? competitor.competitor_id : null)
                }
                onChanged={() => competitors.mutate()}
              />
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-3" data-testid="competitor-scan-area">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-base font-semibold">{t('scan-title')}</h3>
          {scanId && confirmed.length > 0 && !setupOpen && (
            <button
              type="button"
              className={SECONDARY}
              onClick={() => setSetupOpen(true)}
            >
              {t('scan-new')}
            </button>
          )}
        </div>
        {confirmed.length === 0 && competitors.data && (
          <p className="text-sm text-gray-600">{t('scan-needs-confirmed')}</p>
        )}
        {showSetup && (
          <>
            <ScanSetup
              productId={productId}
              competitors={confirmed}
              country={country}
              marketUnmatched={!!planMarket?.trim() && !planCountry}
              planMarket={planMarket?.trim() || null}
              onCountryChange={setCountry}
              onStarted={started}
            />
            {setupOpen && (
              <button
                type="button"
                className="text-sm underline"
                onClick={() => setSetupOpen(false)}
              >
                {t('scan-new-cancel')}
              </button>
            )}
          </>
        )}
        {scan.data && !setupOpen && (
          <ScanProgress
            scan={scan.data}
            listedCompetitorIds={items.map((c) => c.competitor_id)}
            onChangePage={changePage}
            onChanged={async (updated) => {
              await scan.mutate(updated, { revalidate: !updated });
              await scans.mutate();
            }}
          />
        )}
        {scan.error && (
          <p className="text-sm text-red-700" role="alert">
            {t('scan-load-error')}
          </p>
        )}
        {scan.data &&
          !setupOpen &&
          (scan.data.observation_count > 0 || scan.data.snapshot_id) && (
            <CompetitorBoard
              key={scan.data.scan_id}
              scan={scan.data}
              productId={productId}
              onScanChanged={async (updated) => {
                // `mutate(undefined)` would blank the scan (and the board)
                // until the refetch lands; revalidate in place instead.
                if (updated) await scan.mutate(updated, { revalidate: false });
                else await scan.mutate();
              }}
            />
          )}
        {scan.data && !setupOpen && !!scan.data.params?.keywords?.length && (
          <CompetitorDiscovery scan={scan.data} productId={productId} />
        )}
        {scanItems.length > 1 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-gray-600">
              {t('scan-history', { count: scanItems.length })}
            </summary>
            <ul className="mt-2 space-y-1">
              {scanItems.map((item) => (
                <li key={item.scan_id}>
                  <button
                    type="button"
                    className={`flex w-full flex-wrap items-center gap-2 rounded px-2 py-1 text-left ${
                      item.scan_id === scanId ? 'bg-gray-100 font-medium' : ''
                    }`}
                    aria-current={item.scan_id === scanId || undefined}
                    onClick={() => {
                      setSetupOpen(false);
                      void selectScan(item.scan_id);
                    }}
                  >
                    <span>
                      {item.created_at
                        ? formatDateTime(i18n.language, item.created_at, {
                            dateStyle: 'medium',
                            timeStyle: 'short',
                          })
                        : item.scan_id}
                    </span>
                    <span className="text-gray-600">
                      {t(`scan-status-${item.status}`)} ·{' '}
                      {t('scan-history-ads', {
                        count: item.observation_count,
                        formatted: formatCount(
                          i18n.language,
                          item.observation_count
                        ),
                      })}
                    </span>
                    {item.execution_mode === 'mock' && <SimulatedBadge />}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </>
  );
}
