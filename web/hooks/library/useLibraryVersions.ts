import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';

export interface LibraryVersionSummary {
  id: string;
  entry_id: string;
  entry_type: string;
  version_no: number;
  state: string;
  eligibility: string;
  content_schema_version: number;
  lifecycle_revision: number;
  content_hash?: string | null;
  origin_command?: string | null;
  source_version_id?: string | null;
  base_version_id?: string | null;
  published_by?: string | null;
  published_at?: string | null;
  withdraw_reason?: string | null;
}

export function useLibraryVersions(
  entryId: string | null,
  options?: { enabled?: boolean }
): UseDomainPaginatedQueryResult<LibraryVersionSummary> {
  return useDomainPaginatedQuery<
    'list_library_entry_versions',
    LibraryVersionSummary
  >(
    'list_library_entry_versions',
    entryId ? ({ entry_id: entryId } as any) : undefined,
    { enabled: (options?.enabled ?? true) && !!entryId }
  );
}
