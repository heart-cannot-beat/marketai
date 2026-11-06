import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';

export interface LibraryBindingSummary {
  id: string;
  owner_version_id: string;
  reference_version_id: string;
  purpose: string;
  viewpoint: string;
  priority: number;
  note: string;
  conditions: Record<string, unknown>;
  reference_display_name: string;
  gate_status: string;
}

export function useLibraryBindings(
  versionId: string | null,
  options?: { enabled?: boolean }
): UseDomainPaginatedQueryResult<LibraryBindingSummary> {
  return useDomainPaginatedQuery<
    'list_library_version_bindings',
    LibraryBindingSummary
  >(
    'list_library_version_bindings',
    versionId ? ({ version_id: versionId } as any) : undefined,
    { enabled: (options?.enabled ?? true) && !!versionId }
  );
}
