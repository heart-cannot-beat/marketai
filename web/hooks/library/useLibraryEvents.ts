import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';

export interface LibraryEventSummary {
  id: string;
  entry_id: string;
  event_type: string;
  actor_id: string;
  executor_id?: string | null;
  occurred_at: string;
  version_id?: string | null;
  version_no?: number | null;
  detail?: Record<string, unknown> | null;
}

export function useLibraryEvents(
  entryId: string | null,
  options?: { enabled?: boolean }
): UseDomainPaginatedQueryResult<LibraryEventSummary> {
  return useDomainPaginatedQuery<
    'list_library_entry_events',
    LibraryEventSummary
  >(
    'list_library_entry_events',
    entryId ? ({ entry_id: entryId } as any) : undefined,
    { enabled: (options?.enabled ?? true) && !!entryId }
  );
}
