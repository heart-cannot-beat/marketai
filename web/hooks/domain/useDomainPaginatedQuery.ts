import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import useSWRInfinite from 'swr/infinite';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import type { DomainOperationMap } from '@/lib/domain-client/generated/operation-map';
import type { PaginatedOperationId } from '@/lib/domain-client/query-keys';
import { paginatedQueryKey } from '@/lib/domain-client/query-keys';
import type {
  DomainResultFor,
  ProblemDetails,
} from '@/lib/domain-client/result-types';

const DEFAULT_PAGE_SIZE = 50;

export type PaginatedPageData = {
  items: unknown[];
  next_cursor?: string | null;
  [key: string]: unknown;
};

type BaseParams = Record<string, string | number | boolean | undefined>;

type WithoutNeverProperties<T> = {
  [P in keyof T as [T[P]] extends [never] ? never : P]: T[P];
};

type PaginatedQueryParams<K extends PaginatedOperationId> = Omit<
  WithoutNeverProperties<DomainOperationMap[K]['pathParams']> &
    WithoutNeverProperties<DomainOperationMap[K]['queryParams']>,
  'cursor' | 'limit'
>;

export interface UseDomainPaginatedQueryOptions {
  enabled?: boolean;
  pageSize?: number;
}

export interface UseDomainPaginatedQueryResult<T = unknown> {
  items: T[];
  /** The authoritative first page, including operation-specific metadata
   * such as AssetGalleryPage.facets and collection allowed_actions. */
  firstPage: PaginatedPageData | undefined;
  hasMore: boolean;
  isLoading: boolean;
  isLoadingMore: boolean;
  isRefreshing: boolean;
  problem: ProblemDetails | undefined;
  loadMore: () => void;
  refresh: () => Promise<void>;
}

function extractPage<K extends PaginatedOperationId>(
  result: DomainResultFor<K>
): PaginatedPageData | null {
  if (!('data' in result)) return null;
  const data = result.data as PaginatedPageData | null;
  if (!data || !Array.isArray(data.items)) return null;
  return data;
}

function callPaginatedOperation<K extends PaginatedOperationId>(
  gateway: DomainGateway,
  operationId: K,
  workspaceSlug: string,
  params: BaseParams,
  cursor: string | undefined,
  limit: number
): Promise<DomainResultFor<K>> {
  switch (operationId) {
    case 'list_experiments': {
      const p = params as DomainOperationMap['list_experiments']['queryParams'];
      return gateway.listExperiments({
        workspaceSlug,
        state: p.state,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_entries': {
      const p =
        params as DomainOperationMap['list_library_entries']['queryParams'];
      return gateway.listLibraryEntries({
        workspaceSlug,
        entryType: p.entry_type,
        includeArchived:
          p.include_archived !== undefined
            ? p.include_archived === 'true'
            : undefined,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_entry_versions': {
      const p =
        params as DomainOperationMap['list_library_entry_versions']['pathParams'];
      return gateway.listLibraryEntryVersions({
        workspaceSlug,
        entryId: p.entry_id,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_entry_events': {
      const p =
        params as DomainOperationMap['list_library_entry_events']['pathParams'];
      return gateway.listLibraryEntryEvents({
        workspaceSlug,
        entryId: p.entry_id,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_version_bindings': {
      const p =
        params as DomainOperationMap['list_library_version_bindings']['pathParams'];
      return gateway.listLibraryVersionBindings({
        workspaceSlug,
        versionId: p.version_id,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_internal_entries': {
      const p =
        params as DomainOperationMap['list_library_internal_entries']['pathParams'] &
          DomainOperationMap['list_library_internal_entries']['queryParams'];
      return gateway.listLibraryInternalEntries({
        workspaceSlug,
        rootEntryId: p.root_entry_id,
        entryType: p.entry_type!,
        parentId: p.parent_id,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_generation_drafts': {
      const p =
        params as DomainOperationMap['list_generation_drafts']['queryParams'];
      return gateway.listGenerationDrafts({
        workspaceSlug,
        productId: p.product_id,
        state: p.state,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_generation_runs': {
      const p =
        params as DomainOperationMap['list_generation_runs']['queryParams'];
      return gateway.listGenerationRuns({
        workspaceSlug,
        productId: p.product_id,
        status: p.status,
        executionMode: p.execution_mode,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_enrollments': {
      const p =
        params as DomainOperationMap['list_scale_enrollments']['pathParams'] &
          DomainOperationMap['list_scale_enrollments']['queryParams'];
      return gateway.listScaleEnrollments({
        workspaceSlug,
        metaMode: p.meta_mode,
        lifecycle: p.lifecycle,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_advice_threads': {
      const p =
        params as DomainOperationMap['list_scale_advice_threads']['pathParams'] &
          DomainOperationMap['list_scale_advice_threads']['queryParams'];
      return gateway.listScaleAdviceThreads({
        workspaceSlug,
        metaMode: p.meta_mode,
        enrollmentId: p.enrollment_id,
        lifecycle: p.lifecycle,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_targets': {
      const p =
        params as DomainOperationMap['list_scale_targets']['pathParams'] &
          DomainOperationMap['list_scale_targets']['queryParams'];
      return gateway.listScaleTargets({
        workspaceSlug,
        metaMode: p.meta_mode,
        lifecycleState: p.lifecycle_state,
        productVersionId: p.product_version_id,
        offerVersionId: p.offer_version_id,
        marketCode: p.market_code,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_generation_assets': {
      const p =
        params as DomainOperationMap['list_generation_assets']['queryParams'];
      return gateway.listGenerationAssets({
        workspaceSlug,
        productId: p.product_id,
        runId: p.run_id,
        provider: p.provider,
        executionMode: p.execution_mode,
        originKind: p.origin_kind,
        reviewValue: p.review_value,
        aspectRatio: p.aspect_ratio,
        qcState: p.qc_state,
        tag: p.tag,
        experimentContext: p.experiment_context,
        conclusion: p.conclusion,
        cursor,
        limit,
      }) as Promise<DomainResultFor<K>>;
    }
    default: {
      const exhaustiveCheck: never = operationId;
      throw new Error(
        `Unsupported paginated operationId: ${String(exhaustiveCheck)}`
      );
    }
  }
}

function deduplicateById<T extends { id?: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  const result: T[] = [];
  for (const item of items) {
    const id = item.id;
    if (id === undefined) {
      result.push(item);
      continue;
    }
    if (!seen.has(id)) {
      seen.add(id);
      result.push(item);
    }
  }
  return result;
}

type PaginatedKey = readonly [string, string, string, string, string | null];

export function useDomainPaginatedQuery<
  K extends PaginatedOperationId,
  T = unknown,
>(
  operationId: K,
  params?: PaginatedQueryParams<K>,
  options: UseDomainPaginatedQueryOptions = {}
): UseDomainPaginatedQueryResult<T> {
  const { enabled = true, pageSize = DEFAULT_PAGE_SIZE } = options;
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isHydratingFirstPage, setIsHydratingFirstPage] = useState(enabled);
  const loadMoreGuardRef = useRef(false);
  const hydratedScopeRef = useRef<string>();

  const stableParams = useMemo(
    () => JSON.stringify(params ?? {}),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [JSON.stringify(params ?? {})]
  );

  const getKey = useCallback(
    (
      pageIndex: number,
      previousPageData: DomainResultFor<K> | null
    ): PaginatedKey | null => {
      if (!enabled) return null;

      let cursor: string | null = null;
      if (pageIndex > 0) {
        if (!previousPageData) return null;
        const page = extractPage(previousPageData);
        if (!page || !page.next_cursor) return null;
        cursor = page.next_cursor;
      }

      const baseKey = paginatedQueryKey(
        current.slug,
        operationId,
        params as BaseParams
      );
      return [...baseKey, cursor] as const;
    },
    [enabled, current.slug, operationId, stableParams] // eslint-disable-line react-hooks/exhaustive-deps
  );

  const fetcher = useCallback(
    async (key: PaginatedKey) => {
      const cursor = key[4] ?? undefined;
      return callPaginatedOperation(
        gateway,
        operationId,
        current.slug,
        JSON.parse(stableParams),
        cursor,
        pageSize
      );
    },
    [gateway, operationId, current.slug, stableParams, pageSize]
  );

  const {
    data: pages,
    size,
    setSize,
    isLoading: swrIsLoading,
    isValidating,
    mutate,
  } = useSWRInfinite<DomainResultFor<K>>(getKey, fetcher, {
    // The effect below owns the first client revalidation. Leaving SWR's
    // automatic mount pass enabled would race that deterministic pass and
    // issue duplicate BFF reads in clients that do not hit the hydration bug.
    revalidateOnMount: false,
    revalidateIfStale: false,
    revalidateFirstPage: true,
    revalidateOnFocus: false,
    parallel: false,
  });

  const { items, firstPage, hasMore, problem } = useMemo(() => {
    if (!pages || pages.length === 0) {
      return {
        items: [] as T[],
        firstPage: undefined,
        hasMore: false,
        problem: undefined,
      };
    }

    const allItems: unknown[] = [];
    let firstPage: PaginatedPageData | undefined;
    let lastProblem: ProblemDetails | undefined;
    let lastHasMore = false;

    for (const result of pages) {
      if (result.kind === 'problem') {
        lastProblem = (result as { kind: 'problem'; problem: ProblemDetails })
          .problem;
        break;
      }
      const page = extractPage(result);
      if (page) {
        firstPage ??= page;
        allItems.push(...page.items);
        lastHasMore = !!page.next_cursor;
      }
    }

    const deduplicated = deduplicateById(allItems as Array<{ id?: string }>);
    return {
      items: deduplicated as T[],
      firstPage,
      hasMore: lastHasMore,
      problem: lastProblem,
    };
  }, [pages]);

  const isLoading = enabled && (swrIsLoading || isHydratingFirstPage);
  const isLoadingMore = enabled && !swrIsLoading && isValidating && size > 1;

  const loadMore = useCallback(() => {
    if (!hasMore || isLoadingMore || loadMoreGuardRef.current) return;
    loadMoreGuardRef.current = true;
    setSize((s) => s + 1).finally(() => {
      loadMoreGuardRef.current = false;
    });
  }, [hasMore, isLoadingMore, setSize]);

  const refresh = useCallback(async () => {
    setIsRefreshing(true);
    try {
      const firstPageKey = getKey(0, null);
      if (!firstPageKey) return;
      const firstPage = await fetcher(firstPageKey);
      void setSize(1);
      await mutate([firstPage], false);
    } finally {
      setIsRefreshing(false);
    }
  }, [fetcher, getKey, mutate, setSize]);

  useEffect(() => {
    if (!enabled) {
      hydratedScopeRef.current = undefined;
      setIsHydratingFirstPage(false);
      return;
    }

    const scope = `${current.slug}:${operationId}:${stableParams}:${pageSize}`;
    if (hydratedScopeRef.current === scope) return;
    hydratedScopeRef.current = scope;
    setIsHydratingFirstPage(true);

    // Pages Router renders the workspace shell on the server. During
    // hydration SWR Infinite can retain a structurally matching empty page
    // whose bound mutate remains inert. Fetch the first key directly, then
    // seed the infinite cache. This guarantees one BFF read and also
    // refreshes the collection when its Workspace/filter scope changes.
    const firstPageKey = getKey(0, null);
    if (!firstPageKey) return;
    void (async () => {
      const firstPage = await fetcher(firstPageKey);
      if (hydratedScopeRef.current === scope) {
        void setSize(1);
        await mutate([firstPage], false);
        setIsHydratingFirstPage(false);
      }
    })();
  }, [
    current.slug,
    enabled,
    fetcher,
    getKey,
    mutate,
    operationId,
    pageSize,
    setSize,
    stableParams,
  ]);

  return {
    items,
    firstPage,
    hasMore,
    isLoading,
    isLoadingMore,
    isRefreshing,
    problem,
    loadMore,
    refresh,
  };
}
