import { useCallback, useMemo } from 'react';
import { useTranslation } from 'next-i18next';
import { useAssets } from './AssetsProvider';
import { useAssetGallery } from '@/hooks/assets/useAssetGallery';
import { FilterBar } from './FilterBar';
import { AssetGrid } from './AssetGrid';
import { SelectionBar } from './SelectionBar';
import type { AssetFilters } from '@/hooks/assets/useAssetsUrlState';
import { isActionAllowed } from '@/hooks/assets/useAssetActions';
import {
  reviewQueueStorageKey,
  seedReviewQueueSnapshot,
} from '@/hooks/assets/useReviewMode';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import {
  describeRun,
  type RunListEntry,
  type RunOrigin,
} from '@/lib/assets/run-display';

interface GalleryZoneProps {
  /**
   * Mirrors what `BatchRail` renders in the `lg:block` sidebar
   * (`pages/w/[slug]/assets.tsx`) so the same run filter is reachable
   * below the `<lg` breakpoint, where `BatchRail` is `hidden` -- spec §14
   * row 13 ("Responsive layout (<1024px)").
   */
  runs: RunListEntry[];
  activeRunId: string | undefined;
  onSelectRun: (runId: string | undefined) => void;
  /** Same naming inputs as `BatchRail` -- the strip never shows a `gr_` id. */
  origins?: ReadonlyMap<string, RunOrigin>;
  productNameFor?: (
    productId: string | null | undefined
  ) => string | null | undefined;
}

type FilterKey = keyof AssetFilters;

export function GalleryZone({
  runs,
  activeRunId,
  onSelectRun,
  origins,
  productNameFor,
}: GalleryZoneProps) {
  const { t } = useTranslation('assets');
  const { t: tc } = useTranslation('common');
  const { urlState, selection } = useAssets();
  const { current } = useWorkspace();
  const gallery = useAssetGallery(urlState.filters);

  const activeFilters = useMemo(() => {
    const f: Partial<Record<FilterKey, string>> = {};
    if (urlState.filters.provider) f.provider = urlState.filters.provider;
    if (urlState.filters.reviewValue)
      f.reviewValue = urlState.filters.reviewValue;
    if (urlState.filters.originKind) f.originKind = urlState.filters.originKind;
    if (urlState.filters.executionMode)
      f.executionMode = urlState.filters.executionMode;
    if (urlState.filters.aspectRatio)
      f.aspectRatio = urlState.filters.aspectRatio;
    if (urlState.filters.qcState) f.qcState = urlState.filters.qcState;
    if (urlState.filters.tag) f.tag = urlState.filters.tag;
    return f;
  }, [urlState.filters]);

  const handleFilterChange = useCallback(
    (key: FilterKey, value: string | undefined) => {
      urlState.setFilter(key, value);
    },
    [urlState]
  );

  const handleOpenVersion = useCallback(
    (versionId: string) => {
      urlState.setOverlay(versionId);
    },
    [urlState]
  );

  const handleExport = useCallback(() => {
    urlState.setExportId('new');
  }, [urlState]);

  const handleReviewMode = useCallback(() => {
    const selectedInGalleryOrder = gallery.items
      .filter((item) => selection.isSelected(item.id))
      .map((item) => item.id);
    const loadedIds = new Set(selectedInGalleryOrder);
    const selectedOutsideLoadedPages = Array.from(selection.selectedIds).filter(
      (id) => !loadedIds.has(id)
    );
    seedReviewQueueSnapshot(
      reviewQueueStorageKey(current.slug, urlState.filters),
      [...selectedInGalleryOrder, ...selectedOutsideLoadedPages]
    );
    urlState.setReviewMode(true);
  }, [current.slug, gallery.items, selection, urlState]);

  const canExport = isActionAllowed(gallery.allowedActions, 'export');
  const canReview = isActionAllowed(gallery.allowedActions, 'review');

  return (
    <div className="flex flex-col h-full min-h-0">
      {runs.length > 0 && (
        <div
          className="lg:hidden flex overflow-x-auto gap-1 px-4 py-2 border-b border-base-300"
          aria-label={t('runs-nav-label')}
        >
          {runs.map((run) => {
            const display = describeRun(
              run,
              origins?.get(run.id),
              productNameFor ? productNameFor(run.product_id) : null,
              t,
              tc
            );
            return (
              <button
                key={run.id}
                type="button"
                className={`badge whitespace-nowrap ${activeRunId === run.id ? 'badge-primary' : 'badge-ghost'}`}
                onClick={() =>
                  onSelectRun(activeRunId === run.id ? undefined : run.id)
                }
                aria-pressed={activeRunId === run.id}
                aria-label={display.ariaLabel}
                title={run.id}
              >
                {display.title} &middot; {display.statusLabel} &middot;{' '}
                {display.done}/{display.total}
              </button>
            );
          })}
        </div>
      )}

      {gallery.facets && (
        <FilterBar
          facets={gallery.facets}
          activeFilters={activeFilters}
          onFilterChange={handleFilterChange}
        />
      )}

      <div className="flex-1 min-h-0 overflow-y-auto">
        <AssetGrid
          items={gallery.items}
          hasMore={gallery.hasMore}
          isLoading={gallery.isLoading}
          isLoadingMore={gallery.isLoadingMore}
          onLoadMore={gallery.loadMore}
          onOpenVersion={handleOpenVersion}
        />
      </div>

      <SelectionBar
        count={selection.count}
        onClear={selection.clear}
        onExport={handleExport}
        onReviewMode={handleReviewMode}
        canExport={canExport}
        canReview={canReview}
      />
    </div>
  );
}
