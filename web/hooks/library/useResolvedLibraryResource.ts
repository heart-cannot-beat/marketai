import { useMemo } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { LibraryEntryType } from '@/lib/library/navigation';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export interface ResolvedResource {
  id: string;
  entry_type: string;
  display_name: string;
  lifecycle: string;
  current_version_id?: string | null;
  root_entry_id: string;
  root_entry_type: string;
  parent_entry_id?: string | null;
  relation_kind: string;
  creation_evidence: string;
}

export interface UseResolvedLibraryResourceResult {
  resource: ResolvedResource | undefined;
  problem: ProblemDetails | undefined;
  isLoading: boolean;
  notFound: boolean;
  refresh: () => Promise<void>;
}

export function useResolvedLibraryResource(
  rootEntryId: string | null,
  resourceType: LibraryEntryType | null,
  resourceId: string | null
): UseResolvedLibraryResourceResult {
  const enabled = !!rootEntryId && !!resourceType && !!resourceId;

  const { data, problem, isLoading, refresh } = useDomainQuery(
    'get_library_internal_entry_context',
    enabled
      ? ({
          root_entry_id: rootEntryId,
          entry_type: resourceType,
          entry_id: resourceId,
        } as any)
      : undefined,
    { enabled }
  );

  const resource = data as unknown as ResolvedResource | undefined;

  const notFound = useMemo(() => {
    if (!problem) return false;
    return problem.status === 404;
  }, [problem]);

  return {
    resource,
    problem: notFound ? undefined : problem,
    isLoading,
    notFound,
    refresh,
  };
}
