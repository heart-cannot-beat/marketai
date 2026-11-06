import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import type { LibraryEntryType } from '@/lib/library/navigation';

export interface InternalEntrySummary {
  id: string;
  entry_type: string;
  display_name: string;
  lifecycle: string;
  current_version_id?: string | null;
  root_entry_id: string;
  parent_entry_id?: string | null;
}

export function useLibraryInternalEntries(
  rootEntryId: string | null,
  entryType: LibraryEntryType,
  options?: { parentId?: string | null; enabled?: boolean }
): UseDomainPaginatedQueryResult<InternalEntrySummary> {
  const params = rootEntryId
    ? ({
        root_entry_id: rootEntryId,
        entry_type: entryType,
        ...(options?.parentId ? { parent_id: options.parentId } : {}),
      } as any)
    : undefined;

  return useDomainPaginatedQuery<
    'list_library_internal_entries',
    InternalEntrySummary
  >('list_library_internal_entries', params, {
    enabled: (options?.enabled ?? true) && !!rootEntryId,
  });
}
