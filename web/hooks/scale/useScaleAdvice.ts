import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

export type AdviceThreadSummary = components['schemas']['AdviceThreadResponse'];

/**
 * Paginated Advice-thread list (spec §4.2, §4.4 "Advice state" axis).
 * `enrollmentId` narrows to one Enrollment's threads (the detail page's
 * advice panel); omitted, it's the cross-Enrollment advice inbox.
 * `lifecycle` narrows to one of `active`/`resolved`/`superseded`/`expired`.
 */
export function useScaleAdviceThreads(
  metaMode: ScaleMetaMode,
  options?: { enrollmentId?: string; lifecycle?: string; enabled?: boolean }
): UseDomainPaginatedQueryResult<AdviceThreadSummary> {
  return useDomainPaginatedQuery<
    'list_scale_advice_threads',
    AdviceThreadSummary
  >(
    'list_scale_advice_threads',
    {
      meta_mode: metaMode,
      enrollment_id: options?.enrollmentId,
      lifecycle: options?.lifecycle,
    } as any,
    { enabled: options?.enabled ?? true }
  );
}
