import { useRouter } from 'next/router';
import { useCallback, useMemo } from 'react';

export function useScaleNav() {
  const router = useRouter();
  const slug = router.query.slug as string;
  const base = `/w/${slug}/scale`;

  const buildListUrl = useCallback(() => base, [base]);
  const buildEnrollmentUrl = useCallback(
    (enrollmentId: string) => `${base}/${enrollmentId}`,
    [base]
  );
  const buildEvidenceUrl = useCallback(
    (enrollmentId: string) => `${base}/${enrollmentId}/evidence`,
    [base]
  );
  const buildBreakdownsUrl = useCallback(
    (enrollmentId: string) => `${base}/${enrollmentId}/breakdowns`,
    [base]
  );
  const buildTargetsUrl = useCallback(() => `${base}/targets`, [base]);

  const navigateToEnrollment = useCallback(
    (enrollmentId: string) => router.push(buildEnrollmentUrl(enrollmentId)),
    [router, buildEnrollmentUrl]
  );

  return useMemo(
    () => ({
      buildListUrl,
      buildEnrollmentUrl,
      buildEvidenceUrl,
      buildBreakdownsUrl,
      buildTargetsUrl,
      navigateToEnrollment,
    }),
    [
      buildListUrl,
      buildEnrollmentUrl,
      buildEvidenceUrl,
      buildBreakdownsUrl,
      buildTargetsUrl,
      navigateToEnrollment,
    ]
  );
}
