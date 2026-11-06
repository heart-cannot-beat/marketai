import { useCallback } from 'react';
import { useSWRConfig } from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import type { LibraryResource } from '@/lib/library/command-resources';

type OperationId = string;

const RESOURCE_OPERATIONS: Record<LibraryResource, readonly OperationId[]> = {
  entry: ['get_library_entry'],
  entryList: ['list_library_entries'],
  versions: ['list_library_entry_versions'],
  events: ['list_library_entry_events'],
  impact: [
    'get_library_entry_publish_impact',
    'get_library_version_withdraw_impact',
    'get_library_entry_archive_impact',
  ],
  bindings: ['list_library_version_bindings'],
  entryTypes: ['list_library_entry_types'],
  internalEntries: ['list_library_internal_entries'],
};

export interface InvalidateOptions {
  resources: readonly LibraryResource[];
}

function isDomainKey(
  key: unknown
): key is readonly [string, string, string, ...unknown[]] {
  return Array.isArray(key) && key.length >= 3 && key[0] === 'domain';
}

/** SWR's special prefix for `useSWRInfinite` metadata keys. */
const INFINITE_PREFIX = '$inf$';

/**
 * Whether an `$inf$` cache key belongs to a `useSWRInfinite` list of
 * `operationId` in `workspaceSlug`. The serialized infinite key embeds the
 * first page's array key (`['domain', slug, operationId, params, null]`,
 * `lib/domain-client/query-keys.ts`), each element JSON-quoted, so the two
 * quoted markers identify it without re-deriving SWR's hash format.
 */
function isInfiniteKeyFor(
  key: string,
  workspaceSlug: string,
  operationId: OperationId
): boolean {
  return (
    key.startsWith(INFINITE_PREFIX) &&
    key.includes(`"domain","${workspaceSlug}","${operationId}",`)
  );
}

export interface UseLibraryCacheResult {
  invalidate: (options: InvalidateOptions) => Promise<void>;
}

export function useLibraryCache(): UseLibraryCacheResult {
  const { mutate, cache } = useSWRConfig();
  const { current } = useWorkspace();

  const invalidate = useCallback(
    async (options: InvalidateOptions): Promise<void> => {
      const { resources } = options;
      if (resources.length === 0) return;

      const targetOps = new Set<OperationId>();
      for (const resource of resources) {
        const ops = RESOURCE_OPERATIONS[resource];
        for (const op of ops) {
          targetOps.add(op);
        }
      }

      await mutate(
        (key?: unknown) => {
          if (!isDomainKey(key)) return false;
          if (key[1] !== current.slug) return false;
          return targetOps.has(key[2]);
        },
        undefined,
        { revalidate: true }
      );

      // A key-filter `mutate` deliberately skips SWR's `$inf$` keys, and a
      // `useSWRInfinite` list (`useDomainPaginatedQuery`: the entry list,
      // versions, events, bindings, internal entries) only re-reads when
      // *its* key revalidates -- revalidating the per-page keys above leaves
      // the list showing the pages it already had. Revalidate the matching
      // infinite keys explicitly so a freshly created entry appears in the
      // list without a reload.
      const ops = Array.from(targetOps);
      const infiniteKeys = Array.from(cache.keys()).filter(
        (key) =>
          typeof key === 'string' &&
          ops.some((op) => isInfiniteKeyFor(key, current.slug, op))
      );
      await Promise.all(
        infiniteKeys.map((key) => mutate(key, undefined, { revalidate: true }))
      );
    },
    [mutate, cache, current.slug]
  );

  return { invalidate };
}
