import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR, { useSWRConfig } from 'swr';
import { ArrowTopRightOnSquareIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { SimulatedBadge } from './CredentialNotice';
import {
  ACTIVE_STATUSES,
  POLL_MS,
  dataOf,
  formatCount,
  useCommand,
  type CompetitorDiscovery as Discovery,
  type CompetitorScan,
} from './shared';

const PRIMARY =
  'rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-40';
const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';

/** Step reasons with their own sentence; anything else is "stopped". */
const REASONS = ['no_candidates', 'calls_failed', 'ordinals_exhausted'];

/**
 * Competitor discovery (spec §8): the advertisers a scan's keywords found
 * outside the product's list, judged by AI one by one (an inference, never
 * a fact, ADR 0014). A member accepts one -- the confirmation card shows the
 * page it binds, whose ID comes from the ads, so there is no resolve and no
 * charge -- or rejects it, and later scans leave it out. An accepted
 * competitor that was archived is recommended again, "open" with that
 * accept as its decision, to be decided anew (#330). Personal accounts
 * never reach this list, and no avatar is shown (§10).
 */
export function CompetitorDiscovery({
  scan,
  productId,
}: {
  scan: CompetitorScan;
  productId: string;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { mutate } = useSWRConfig();
  const active = ACTIVE_STATUSES.includes(scan.status);
  // A new run of the scan (a resume) or its end refetches at once.
  const key = [
    'competitor-discoveries',
    current.workspaceId,
    scan.scan_id,
    scan.job_id,
    scan.status,
  ];
  const listed = useSWR(
    key,
    async () =>
      dataOf(
        await gateway.listCompetitorScanDiscoveries({
          workspaceSlug: current.slug,
          scanId: scan.scan_id,
        })
      ),
    { refreshInterval: active ? POLL_MS : 0 }
  );
  const command = useCommand();
  const [open, setOpen] = useState<{
    pageId: string;
    action: 'accept' | 'reject';
  } | null>(null);
  const [reason, setReason] = useState('');

  const data = listed.data;
  const items = data?.items ?? [];

  async function decide(item: Discovery, decision: 'accept' | 'reject') {
    const done = await command.run(
      `discovery:${item.advertiser_page_id}:${decision}`,
      async (action) =>
        dataOf(
          await gateway.decideCompetitorDiscovery({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            scanId: scan.scan_id,
            advertiserPageId: item.advertiser_page_id,
            body: {
              decision,
              ...(decision === 'reject' && reason.trim()
                ? { reason: reason.trim() }
                : {}),
            },
          })
        )
    );
    if (!done) return;
    setOpen(null);
    setReason('');
    await listed.mutate();
    if (decision === 'accept')
      await mutate(['competitors', current.workspaceId, productId]);
  }

  return (
    <div className="space-y-3" data-testid="competitor-discovery">
      <div className="space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-base font-semibold">{t('discovery-title')}</h3>
          {scan.execution_mode === 'mock' && <SimulatedBadge />}
        </div>
        <p className="text-sm text-gray-600">{t('discovery-intro')}</p>
      </div>
      {listed.error && (
        <p className="text-sm text-red-700" role="alert">
          {t('discovery-load-error')}
        </p>
      )}
      {!data && !listed.error && (
        <p className="text-sm text-gray-500" role="status">
          {t('discovery-loading')}
        </p>
      )}
      {data && (
        <DiscoveryStatus
          status={data.status}
          reason={data.reason ?? null}
          active={active}
        />
      )}
      {data && data.advertisers > 0 && (
        <p className="text-sm text-gray-600" data-testid="discovery-counts">
          {t('discovery-counts', {
            count: data.advertisers,
            recommended: formatCount(i18n.language, data.recommended),
            formatted: formatCount(i18n.language, data.advertisers),
            unrelated: formatCount(i18n.language, data.unrelated),
          })}
          {data.over_cap > 0 &&
            ` ${t('discovery-over-cap', {
              count: data.over_cap,
              formatted: formatCount(i18n.language, data.over_cap),
            })}`}
        </p>
      )}
      {data && data.status === 'completed' && items.length === 0 && (
        <p className="text-sm text-gray-600">{t('discovery-none')}</p>
      )}
      {items.length > 0 && (
        <ul className="space-y-3" data-testid="discovery-list">
          {items.map((item) => (
            <DiscoveryItem
              key={item.advertiser_page_id}
              item={item}
              open={
                open?.pageId === item.advertiser_page_id ? open.action : null
              }
              busy={command.busy}
              error={command.errorFor(`discovery:${item.advertiser_page_id}:`)}
              reason={reason}
              onReason={setReason}
              onOpen={(action) => {
                command.clearError();
                setReason('');
                setOpen(
                  action ? { pageId: item.advertiser_page_id, action } : null
                );
              }}
              onDecide={(decision) => void decide(item, decision)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function DiscoveryStatus({
  status,
  reason,
  active,
}: {
  status: string;
  reason: string | null;
  active: boolean;
}) {
  const { t } = useTranslation('competitors');
  if (status === 'pending' || status === 'running')
    return (
      <p className="text-sm text-gray-600" role="status">
        {t(active ? 'discovery-waiting' : 'discovery-not-run')}
      </p>
    );
  if (status === 'skipped')
    return (
      <p className="text-sm text-gray-600">
        {t(
          reason && REASONS.includes(reason)
            ? `discovery-reason-${reason}`
            : 'discovery-reason-stopped'
        )}
      </p>
    );
  if (status === 'partial' || status === 'failed')
    return (
      <p
        className="text-sm text-amber-800"
        role="status"
        data-testid="discovery-incomplete"
      >
        {t(
          reason && REASONS.includes(reason)
            ? `discovery-reason-${reason}`
            : 'discovery-reason-stopped'
        )}{' '}
        {t('discovery-resume-hint')}
      </p>
    );
  return null;
}

function DiscoveryItem({
  item,
  open,
  busy,
  error,
  reason,
  onReason,
  onOpen,
  onDecide,
}: {
  item: Discovery;
  open: 'accept' | 'reject' | null;
  busy: string | null;
  error: string | null;
  reason: string;
  onReason: (value: string) => void;
  onOpen: (action: 'accept' | 'reject' | null) => void;
  onDecide: (decision: 'accept' | 'reject') => void;
}) {
  const { t, i18n } = useTranslation('competitors');
  const name = item.advertiser_name || item.advertiser_page_id;
  const deciding = busy?.startsWith(`discovery:${item.advertiser_page_id}:`);
  const decidable = item.state === 'open' || item.state === 'rejected';

  return (
    <li
      className="space-y-3 rounded-lg border border-gray-200 p-3"
      data-testid="discovery-item"
      data-page-id={item.advertiser_page_id}
      data-state={item.state}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="break-words font-medium">{name}</p>
          <p className="text-xs text-gray-500">
            {t('candidate-page-id', { id: item.advertiser_page_id })}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1 text-xs">
          <span
            className={`rounded px-1.5 py-0.5 font-medium ${
              item.relevance === 'relevant'
                ? 'bg-green-100 text-green-900'
                : 'bg-sky-100 text-sky-900'
            }`}
            data-testid="discovery-relevance"
          >
            {t(`discovery-relevance-${item.relevance}`)}
          </span>
          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700">
            {t('discovery-ai-inference')}
          </span>
        </div>
      </div>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
        <Fact label={t('discovery-categories')}>
          {item.categories?.length
            ? item.categories.join(', ')
            : t('candidate-unknown')}
        </Fact>
        <Fact label={t('discovery-observations')}>
          {t('discovery-observation-count', {
            count: item.observation_count,
            formatted: formatCount(i18n.language, item.observation_count),
          })}
        </Fact>
        <Fact label={t('candidate-landing-domains')}>
          {item.landing_domains?.length
            ? item.landing_domains.join(', ')
            : t('candidate-none')}
        </Fact>
        <Fact label={t('discovery-keywords')}>
          {(item.keywords ?? []).join(', ') || t('candidate-none')}
        </Fact>
      </dl>
      {item.reason && (
        <p className="text-sm text-gray-700" data-testid="discovery-reason">
          {t('discovery-reason', { reason: item.reason })}
        </p>
      )}
      <a
        href={item.library_url}
        target="_blank"
        rel="noopener noreferrer nofollow"
        className="inline-flex items-center gap-1 text-sm underline"
      >
        {t('discovery-library-link')}
        <ArrowTopRightOnSquareIcon className="h-4 w-4" aria-hidden="true" />
      </a>

      {item.state === 'accepted' && (
        <p className="text-sm font-medium text-green-800" role="status">
          {t('discovery-state-accepted')}
        </p>
      )}
      {item.state === 'competitor' && (
        <p className="text-sm text-gray-600">
          {t('discovery-state-competitor')}
        </p>
      )}
      {item.state === 'open' && item.decision?.decision === 'accept' && (
        <p className="text-sm text-gray-600" data-testid="discovery-archived">
          {t('discovery-state-archived')}
        </p>
      )}
      {item.state === 'rejected' && (
        <p className="text-sm text-gray-600" data-testid="discovery-rejected">
          {item.decision?.reason
            ? t('discovery-state-rejected-reason', {
                reason: item.decision.reason,
              })
            : t('discovery-state-rejected')}
        </p>
      )}

      {decidable && open === null && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={PRIMARY}
            disabled={!!busy}
            onClick={() => onOpen('accept')}
          >
            {t('discovery-accept')}
          </button>
          {item.state === 'open' && (
            <button
              type="button"
              className={SECONDARY}
              disabled={!!busy}
              onClick={() => onOpen('reject')}
            >
              {t('discovery-reject')}
            </button>
          )}
        </div>
      )}

      {open === 'accept' && (
        <div
          className="space-y-3 rounded-lg border border-gray-200 bg-gray-50 p-3"
          data-testid="discovery-confirm-card"
        >
          <p className="text-sm font-medium">
            {t('discovery-confirm-title', { name })}
          </p>
          <p className="text-sm text-gray-600">{t('discovery-confirm-free')}</p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={PRIMARY}
              disabled={!!busy}
              onClick={() => onDecide('accept')}
            >
              {deciding ? t('discovery-confirming') : t('discovery-confirm')}
            </button>
            <button
              type="button"
              className={SECONDARY}
              disabled={!!busy}
              onClick={() => onOpen(null)}
            >
              {t('discovery-cancel')}
            </button>
          </div>
        </div>
      )}

      {open === 'reject' && (
        <div className="space-y-2" data-testid="discovery-reject-form">
          <label className="block text-sm">
            <span className="text-gray-700">
              {t('discovery-reject-reason')}
            </span>
            <textarea
              className="mt-1 block w-full rounded-lg border border-gray-300 p-2 text-sm"
              rows={2}
              maxLength={500}
              value={reason}
              onChange={(event) => onReason(event.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={PRIMARY}
              disabled={!!busy}
              onClick={() => onDecide('reject')}
            >
              {t('discovery-reject-confirm')}
            </button>
            <button
              type="button"
              className={SECONDARY}
              disabled={!!busy}
              onClick={() => onOpen(null)}
            >
              {t('discovery-cancel')}
            </button>
          </div>
        </div>
      )}

      {error && (
        <p className="text-sm text-red-700" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

function Fact({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}
