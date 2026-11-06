import { useMemo } from 'react';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';

export interface PublishedReferenceAsset {
  entryId: string;
  displayName: string;
  /** The current effective ReferenceAssetVersion -- the id `bind` consumes. */
  versionId: string;
  versionNo: number | null;
  updatedAt: string | null;
}

interface ReferenceEntrySummary {
  id: string;
  entry_type: string;
  display_name: string;
  lifecycle: string;
  current_version_id: string | null;
  current_version_no?: number | null;
  current_version_state?: string | null;
  updated_at?: string | null;
}

export interface UsePublishedReferenceAssetsResult extends Pick<
  UseDomainPaginatedQueryResult<ReferenceEntrySummary>,
  'hasMore' | 'isLoading' | 'isLoadingMore' | 'problem' | 'loadMore' | 'refresh'
> {
  items: PublishedReferenceAsset[];
}

/**
 * Published, bindable references in the current Workspace -- the picker
 * behind `BindReferenceDialog`. Projects `list_library_entries`
 * (`entry_type=reference_asset`) down to entries with an effective current
 * version, so nothing the user can pick is a Draft or a withdrawn version.
 */
export function usePublishedReferenceAssets(options?: {
  enabled?: boolean;
}): UsePublishedReferenceAssetsResult {
  const query = useDomainPaginatedQuery<
    'list_library_entries',
    ReferenceEntrySummary
  >('list_library_entries', { entry_type: 'reference_asset' } as any, {
    enabled: options?.enabled ?? true,
  });

  const items = useMemo<PublishedReferenceAsset[]>(
    () =>
      query.items
        .filter(
          (entry) =>
            entry.lifecycle === 'active' &&
            !!entry.current_version_id &&
            (entry.current_version_state ?? 'effective') === 'effective'
        )
        .map((entry) => ({
          entryId: entry.id,
          displayName: entry.display_name,
          versionId: entry.current_version_id as string,
          versionNo: entry.current_version_no ?? null,
          updatedAt: entry.updated_at ?? null,
        })),
    [query.items]
  );

  return {
    items,
    hasMore: query.hasMore,
    isLoading: query.isLoading,
    isLoadingMore: query.isLoadingMore,
    problem: query.problem,
    loadMore: query.loadMore,
    refresh: query.refresh,
  };
}
