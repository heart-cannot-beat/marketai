import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import type { LibraryEntryType } from '@/lib/library/navigation';

export interface LibraryEntrySummary {
  id: string;
  entry_type: string;
  display_name: string;
  lifecycle: string;
  /** The entry's published version, null until it has one. */
  current_version_id?: string | null;
  draft_state: string | null;
  current_version_number: number | null;
  current_version_state: string | null;
  eligibility: string | null;
  updated_at: string;
  created_at: string;
}

/**
 * The Workspace's active entries of one type. The list route takes any
 * entry type: the Library lists its top-level types, and the manual fact
 * import lists Offers for its default Offer (#54).
 */
export function useLibraryEntries(
  entryType: LibraryEntryType,
  options?: { enabled?: boolean; pageSize?: number }
): UseDomainPaginatedQueryResult<LibraryEntrySummary> {
  return useDomainPaginatedQuery<'list_library_entries', LibraryEntrySummary>(
    'list_library_entries',
    { entry_type: entryType } as any,
    { enabled: options?.enabled ?? true, pageSize: options?.pageSize }
  );
}
