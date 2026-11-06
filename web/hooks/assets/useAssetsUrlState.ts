import { useCallback, useMemo } from 'react';
import { useRouter } from 'next/router';

export interface AssetFilters {
  productId: string | undefined;
  runId: string | undefined;
  provider: string | undefined;
  originKind: string | undefined;
  executionMode: string | undefined;
  reviewValue: string | undefined;
  aspectRatio: string | undefined;
  qcState: string | undefined;
  tag: string | undefined;
  experimentContext: string | undefined;
  conclusion: string | undefined;
}

type FilterKey = keyof AssetFilters;

const URL_TO_FILTER: Record<string, FilterKey> = {
  product: 'productId',
  run: 'runId',
  provider: 'provider',
  origin: 'originKind',
  mode: 'executionMode',
  review: 'reviewValue',
  aspect: 'aspectRatio',
  qc: 'qcState',
  tag: 'tag',
  context: 'experimentContext',
  conclusion: 'conclusion',
};

const FILTER_TO_URL: Record<FilterKey, string> = Object.fromEntries(
  Object.entries(URL_TO_FILTER).map(([k, v]) => [v, k])
) as Record<FilterKey, string>;

function asString(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

/** The filters of `/assets?run=<runId>` and nothing else -- what another
 * page uses to key a review queue it seeds before opening that URL. */
export function runOnlyAssetFilters(runId: string): AssetFilters {
  const filters = Object.fromEntries(
    Object.values(URL_TO_FILTER).map((key) => [key, undefined])
  ) as unknown as AssetFilters;
  return { ...filters, runId };
}

/** `?view=directions` groups a test-plan Run's assets by direction (Issue
 * #170); anything else is the ungrouped gallery. Not a gallery filter: it
 * changes neither the query nor the review-queue key. */
export type AssetsView = 'directions' | 'all' | undefined;

export interface UseAssetsUrlStateResult {
  filters: AssetFilters;
  overlay: string | undefined;
  exportId: string | undefined;
  reviewMode: boolean;
  view: AssetsView;
  setFilter: (key: FilterKey, value: string | undefined) => void;
  setOverlay: (versionId: string | undefined) => void;
  setExportId: (packageId: string | undefined) => void;
  setReviewMode: (active: boolean) => void;
  setView: (view: AssetsView) => void;
}

export function useAssetsUrlState(): UseAssetsUrlStateResult {
  const router = useRouter();
  const { query } = router;

  const filters = useMemo<AssetFilters>(() => {
    const f: Record<string, string | undefined> = {};
    for (const [urlKey, filterKey] of Object.entries(URL_TO_FILTER)) {
      f[filterKey] = asString(query[urlKey]);
    }
    return f as unknown as AssetFilters;
  }, [query]);

  const overlay = asString(query.overlay);
  const exportId = asString(query.export);
  const reviewMode = asString(query.reviewMode) === '1';
  const rawView = asString(query.view);
  const view: AssetsView =
    rawView === 'directions' || rawView === 'all' ? rawView : undefined;

  const buildQuery = useCallback(
    (overrides: Record<string, string | undefined>) => {
      const next: Record<string, string> = {};
      if (query.slug) next.slug = asString(query.slug)!;

      for (const [urlKey] of Object.entries(URL_TO_FILTER)) {
        const current = asString(query[urlKey]);
        if (current) next[urlKey] = current;
      }
      if (query.overlay) next.overlay = asString(query.overlay)!;
      if (query.export) next.export = asString(query.export)!;
      if (query.reviewMode) next.reviewMode = asString(query.reviewMode)!;
      if (query.view) next.view = asString(query.view)!;

      for (const [key, value] of Object.entries(overrides)) {
        if (value === undefined) {
          delete next[key];
        } else {
          next[key] = value;
        }
      }
      return next;
    },
    [query]
  );

  const setFilter = useCallback(
    (key: FilterKey, value: string | undefined) => {
      const urlKey = FILTER_TO_URL[key];
      router.push(
        { pathname: router.pathname, query: buildQuery({ [urlKey]: value }) },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setOverlay = useCallback(
    (versionId: string | undefined) => {
      router.replace(
        {
          pathname: router.pathname,
          query: buildQuery({ overlay: versionId }),
        },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setExportId = useCallback(
    (packageId: string | undefined) => {
      router.replace(
        { pathname: router.pathname, query: buildQuery({ export: packageId }) },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setReviewMode = useCallback(
    (active: boolean) => {
      router.replace(
        {
          pathname: router.pathname,
          query: buildQuery({ reviewMode: active ? '1' : undefined }),
        },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  const setView = useCallback(
    (nextView: AssetsView) => {
      router.replace(
        { pathname: router.pathname, query: buildQuery({ view: nextView }) },
        undefined,
        { shallow: true }
      );
    },
    [router, buildQuery]
  );

  return {
    filters,
    overlay,
    exportId,
    reviewMode,
    view,
    setFilter,
    setOverlay,
    setExportId,
    setReviewMode,
    setView,
  };
}
