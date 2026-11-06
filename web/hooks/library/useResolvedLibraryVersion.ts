import { useMemo } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export interface ResolvedVersion {
  id: string;
  entry_id: string;
  entry_type: string;
  version_no: number;
  state: string;
  eligibility: string;
  content_schema_version: number;
  lifecycle_revision: number;
  content?: Record<string, unknown> | null;
  content_hash?: string | null;
  origin_command?: string | null;
  source_version_id?: string | null;
  base_version_id?: string | null;
  published_by?: string | null;
  published_at?: string | null;
  withdraw_reason?: string | null;
  allowed_actions?: Array<{
    action: string;
    eligible: boolean;
    reason_code?: string;
  }>;
}

export interface UseResolvedLibraryVersionResult {
  version: ResolvedVersion | undefined;
  problem: ProblemDetails | undefined;
  mismatch: boolean;
  isLoading: boolean;
  refresh: () => Promise<void>;
}

export function useResolvedLibraryVersion(
  versionId: string | null,
  expectedEntryId: string | null
): UseResolvedLibraryVersionResult {
  const { data, problem, isLoading, refresh } = useDomainQuery(
    'get_library_version',
    versionId ? ({ version_id: versionId } as any) : undefined,
    { enabled: !!versionId }
  );

  const version = data as unknown as ResolvedVersion | undefined;

  const mismatch = useMemo(() => {
    if (!version || !expectedEntryId) return false;
    return version.entry_id !== expectedEntryId;
  }, [version, expectedEntryId]);

  return {
    version: mismatch ? undefined : version,
    problem,
    mismatch,
    isLoading,
    refresh,
  };
}
