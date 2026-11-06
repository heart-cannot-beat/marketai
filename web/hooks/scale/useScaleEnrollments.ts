import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

export type EnrollmentSummary = components['schemas']['EnrollmentResponse'];

/**
 * Paginated Enrollment list (spec §4.1) -- `lifecycle` narrows to one of
 * `draft`/`active`/`paused`/`closed`; omitted, the server returns every
 * lifecycle for the Workspace + `metaMode`.
 */
export function useScaleEnrollments(
  metaMode: ScaleMetaMode,
  options?: { lifecycle?: string; enabled?: boolean }
): UseDomainPaginatedQueryResult<EnrollmentSummary> {
  return useDomainPaginatedQuery<'list_scale_enrollments', EnrollmentSummary>(
    'list_scale_enrollments',
    { meta_mode: metaMode, lifecycle: options?.lifecycle } as any,
    { enabled: options?.enabled ?? true }
  );
}

/** Single Enrollment read, used by the detail page and lifecycle actions --
 * `data.revision` is the `If-Match` precondition a lifecycle command threads
 * back (spec §4.3), and `data.allowed_actions` is the sole source of truth
 * for which lifecycle buttons render enabled (never inferred client-side). */
export function useScaleEnrollment(
  enrollmentId: string | undefined,
  metaMode: ScaleMetaMode
) {
  return useDomainQuery(
    'get_scale_enrollment',
    enrollmentId
      ? ({ enrollment_id: enrollmentId, meta_mode: metaMode } as any)
      : undefined,
    { enabled: !!enrollmentId }
  );
}
