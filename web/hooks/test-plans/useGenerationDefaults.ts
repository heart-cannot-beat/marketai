import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { GenerationDefaultsView } from '@/lib/test-plans/plan-model';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export interface UseGenerationDefaultsResult {
  defaults: GenerationDefaultsView | undefined;
  isLoading: boolean;
  problem: ProblemDetails | undefined;
  refresh: () => Promise<void>;
}

/** The Workspace's effective「生成默认值」(`get_generation_defaults`),
 * shared through the SWR cache by the banner, the three questions and the
 * Settings block. */
export function useGenerationDefaults(options?: {
  enabled?: boolean;
}): UseGenerationDefaultsResult {
  const query = useDomainQuery('get_generation_defaults', undefined, {
    enabled: options?.enabled ?? true,
  });
  return {
    defaults: query.data as GenerationDefaultsView | undefined,
    isLoading: query.isLoading,
    problem: query.problem,
    refresh: query.refresh,
  };
}
