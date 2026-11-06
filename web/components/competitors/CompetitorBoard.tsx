import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import useSWRInfinite from 'swr/infinite';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { tagLabelsOf, type TagLabels } from './AdTags';
import {
  KNOWN_FORMATS,
  ObservationCard,
  useFormatLabel,
} from './ObservationCard';
import { SnapshotSummary } from './SnapshotSummary';
import {
  RELEVANCE_VALUES,
  dataOf,
  formatCount,
  problemMessage,
  type CompetitorAd,
  type CompetitorScan,
} from './shared';

const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';
const SELECT =
  'w-full rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm';
const PAGE_SIZE = 24;
const DAY_THRESHOLDS = [30, 60, 90] as const;

type RelevanceFilter =
  | ''
  | 'relevant'
  | 'adjacent'
  | 'unrelated'
  | 'unassessed';

export interface BoardFilters {
  /** `all`, `self` (the product's own page) or a competitor ID. */
  competitor: string;
  relevance: RelevanceFilter;
  minDays: number | null;
  format: string;
}

const NO_FILTERS: BoardFilters = {
  competitor: 'all',
  relevance: '',
  minDays: null,
  format: '',
};

type AdQuery = {
  competitorId?: string;
  role?: 'competitor' | 'self';
  relevance?: Exclude<RelevanceFilter, ''>;
  minDaysRunning?: number;
  displayFormat?: string;
};

/** The list query of a filter set; `group` picks the own-page group. */
export function adQueryOf(filters: BoardFilters, group: 'main' | 'self') {
  const query: AdQuery = {};
  if (group === 'self' || filters.competitor === 'self') query.role = 'self';
  else if (filters.competitor !== 'all')
    query.competitorId = filters.competitor;
  if (filters.relevance) query.relevance = filters.relevance;
  if (filters.minDays) query.minDaysRunning = filters.minDays;
  if (filters.format) query.displayFormat = filters.format;
  return query;
}

/**
 * The scan's observations, a page at a time. `version` changes whenever the
 * scan moves on (more ads, a resume, a new snapshot), so the list follows.
 */
function useAdPages(scan: CompetitorScan, query: AdQuery | null) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const version = [
    scan.status,
    scan.observation_count,
    scan.snapshot_id ?? '',
    (scan.resumes ?? []).length,
  ].join(':');
  return useSWRInfinite(
    (index, previous: { next_cursor?: string | null } | null) => {
      if (!query) return null;
      if (previous && !previous.next_cursor) return null;
      return [
        'competitor-scan-ads',
        current.workspaceId,
        scan.scan_id,
        version,
        JSON.stringify(query),
        index === 0 ? null : previous?.next_cursor,
      ];
    },
    async ([, , scanId, , , cursor]: [
      string,
      string,
      string,
      string,
      string,
      string | null,
    ]) =>
      dataOf(
        await gateway.listCompetitorScanAds({
          workspaceSlug: current.slug,
          scanId,
          ...query!,
          cursor: cursor ?? undefined,
          limit: PAGE_SIZE,
        })
      ),
    { revalidateFirstPage: false }
  );
}

/**
 * The board and summary of the scan on screen (spec §9). Each observation is
 * what the Meta Ad Library showed, with its source and link; the judgments
 * on it are AI inference until a member flips or confirms them, and none of
 * it is evidence of what works (ADR 0014). The product's own page is its own
 * group and never counts towards crowding.
 */
export function CompetitorBoard({
  scan,
  productId,
  onScanChanged,
}: {
  scan: CompetitorScan;
  productId: string;
  onScanChanged: (scan?: CompetitorScan) => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const formatLabel = useFormatLabel();
  const [filters, setFilters] = useState<BoardFilters>(NO_FILTERS);
  const locale = i18n.language;

  const summary = useSWR(
    scan.snapshot_id
      ? [
          'competitor-scan-summary',
          current.workspaceId,
          scan.scan_id,
          scan.snapshot_id,
        ]
      : null,
    async () =>
      dataOf(
        await gateway.getCompetitorScanSummary({
          workspaceSlug: current.slug,
          scanId: scan.scan_id,
        })
      )
  );
  const main = useAdPages(scan, adQueryOf(filters, 'main'));
  const own = useAdPages(
    scan,
    filters.competitor === 'all' ? adQueryOf(filters, 'self') : null
  );
  const tagLabels = tagLabelsOf(summary.data);

  /** After a flip or a tags revision: the lists, the summary, the scan. */
  async function refresh() {
    await Promise.all([
      main.mutate(),
      own.mutate(),
      summary.mutate(),
      onScanChanged(),
    ]);
  }

  const targets = (scan.targets ?? []).filter(
    (target) => target.kind === 'page' && target.competitor_id
  );
  const competitors = targets.filter((target) => target.role !== 'self');
  const hasSelf = targets.some((target) => target.role === 'self');
  const set = (patch: Partial<BoardFilters>) =>
    setFilters((previous) => ({ ...previous, ...patch }));
  const filtered =
    filters.competitor !== 'all' ||
    !!filters.relevance ||
    !!filters.minDays ||
    !!filters.format;

  return (
    <div className="space-y-4" data-testid="competitor-board-area">
      <SnapshotSummary
        scan={scan}
        productId={productId}
        summary={summary.data ?? null}
        summaryError={summary.error}
        onRefrozen={async () => {
          await Promise.all([summary.mutate(), onScanChanged()]);
        }}
        onScanChanged={onScanChanged}
      />

      <section
        className="space-y-3"
        data-testid="competitor-board"
        aria-labelledby="competitor-board-title"
      >
        <h4 id="competitor-board-title" className="text-base font-semibold">
          {t('board-title')}
        </h4>
        <p className="text-sm text-gray-600">{t('board-intro')}</p>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <label className="block space-y-1 text-xs">
            <span className="text-gray-600">
              {t('board-filter-competitor')}
            </span>
            <select
              className={SELECT}
              value={filters.competitor}
              onChange={(event) => set({ competitor: event.target.value })}
              data-testid="competitor-board-filter-competitor"
            >
              <option value="all">{t('board-filter-all')}</option>
              {competitors.map((target) => (
                <option key={target.origin} value={target.competitor_id!}>
                  {target.page_name ||
                    target.observed_page_name ||
                    target.page_id ||
                    target.competitor_id}
                </option>
              ))}
              {hasSelf && <option value="self">{t('role-self')}</option>}
            </select>
          </label>
          <label className="block space-y-1 text-xs">
            <span className="text-gray-600">{t('board-filter-relevance')}</span>
            <select
              className={SELECT}
              value={filters.relevance}
              onChange={(event) =>
                set({ relevance: event.target.value as RelevanceFilter })
              }
              data-testid="competitor-board-filter-relevance"
            >
              <option value="">{t('board-filter-all')}</option>
              {[...RELEVANCE_VALUES, 'unassessed' as const].map((value) => (
                <option key={value} value={value}>
                  {t(`card-relevance-${value}`)}
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1 text-xs">
            <span className="text-gray-600">{t('board-filter-days')}</span>
            <select
              className={SELECT}
              value={filters.minDays ?? ''}
              onChange={(event) =>
                set({
                  minDays: event.target.value
                    ? Number(event.target.value)
                    : null,
                })
              }
              data-testid="competitor-board-filter-days"
            >
              <option value="">{t('board-filter-all')}</option>
              {DAY_THRESHOLDS.map((days) => (
                <option key={days} value={days}>
                  {t('board-filter-days-at-least', { days })}
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1 text-xs">
            <span className="text-gray-600">{t('board-filter-format')}</span>
            <select
              className={SELECT}
              value={filters.format}
              onChange={(event) => set({ format: event.target.value })}
              data-testid="competitor-board-filter-format"
            >
              <option value="">{t('board-filter-all')}</option>
              {KNOWN_FORMATS.map((format) => (
                <option key={format} value={format}>
                  {formatLabel(format)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {filtered && (
          <button
            type="button"
            className="text-sm underline"
            onClick={() => setFilters(NO_FILTERS)}
          >
            {t('board-filter-clear')}
          </button>
        )}

        <AdGroup
          testId="competitor-board-main"
          pages={main}
          exclude={filters.competitor === 'all' ? 'self' : null}
          excludedTotal={
            filters.competitor === 'all' ? (own.data?.[0]?.total ?? 0) : 0
          }
          tagLabels={tagLabels}
          onChanged={refresh}
          locale={locale}
          empty={filtered ? t('board-empty-filtered') : t('board-empty')}
        />

        {filters.competitor === 'all' && (own.data?.[0]?.total ?? 0) > 0 && (
          <div className="space-y-2" data-testid="competitor-board-self">
            <h5 className="text-sm font-semibold">{t('board-self-title')}</h5>
            <p className="text-xs text-gray-600">{t('board-self-hint')}</p>
            <AdGroup
              testId="competitor-board-self-list"
              pages={own}
              exclude={null}
              excludedTotal={0}
              tagLabels={tagLabels}
              onChanged={refresh}
              locale={locale}
              empty={t('board-empty-filtered')}
            />
          </div>
        )}
      </section>
    </div>
  );
}

function AdGroup({
  testId,
  pages,
  exclude,
  excludedTotal,
  tagLabels,
  onChanged,
  locale,
  empty,
}: {
  testId: string;
  pages: ReturnType<typeof useAdPages>;
  /** Shown in a group of its own (the product's own page). */
  exclude: 'self' | null;
  excludedTotal: number;
  tagLabels: TagLabels;
  onChanged: () => Promise<unknown>;
  locale: string;
  empty: string;
}) {
  const { t } = useTranslation('competitors');
  const loaded = (pages.data ?? []).flatMap((page) => page.items ?? []);
  const items: CompetitorAd[] = exclude
    ? loaded.filter((ad) => ad.role !== exclude)
    : loaded;
  const total = Math.max(
    0,
    (pages.data?.[0]?.total ?? 0) - (exclude ? excludedTotal : 0)
  );
  const more = !!pages.data?.[pages.data.length - 1]?.next_cursor;

  if (pages.error)
    return (
      <p className="text-sm text-red-700" role="alert">
        {problemMessage(t, pages.error)}
      </p>
    );
  if (!pages.data)
    return (
      <p className="text-sm text-gray-500" role="status">
        {t('board-loading')}
      </p>
    );
  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-600" data-testid={`${testId}-count`}>
        {t('board-count', {
          shown: formatCount(locale, items.length),
          total: formatCount(locale, total),
        })}
      </p>
      {items.length === 0 ? (
        <p className="text-sm text-gray-600">{empty}</p>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2" data-testid={testId}>
          {items.map((ad) => (
            <ObservationCard
              key={ad.ad_id}
              ad={ad}
              tagLabels={tagLabels}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
      {more && (
        <button
          type="button"
          className={SECONDARY}
          disabled={pages.isValidating}
          onClick={() => void pages.setSize(pages.size + 1)}
          data-testid={`${testId}-more`}
        >
          {t('board-more')}
        </button>
      )}
    </div>
  );
}
