import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import type { TopLevelEntryType } from '@/lib/library/navigation';

export interface LibraryEntrySummary {
  id: string;
  entry_type: string;
  display_name: string;
  lifecycle: string;
  draft_state: string | null;
  current_version_number: number | null;
  current_version_state: string | null;
  eligibility: string | null;
  updated_at: string;
  created_at: string;
}

export function useLibraryEntries(
  entryType: TopLevelEntryType,
  options?: { enabled?: boolean }
): UseDomainPaginatedQueryResult<LibraryEntrySummary> {
  return useDomainPaginatedQuery<'list_library_entries', LibraryEntrySummary>(
    'list_library_entries',
    { entry_type: entryType } as any,
    { enabled: options?.enabled ?? true }
  );
}
