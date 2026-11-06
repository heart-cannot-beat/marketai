import { useRouter } from 'next/router';
import { useCallback, useMemo } from 'react';

export function useGenerationNav() {
  const router = useRouter();
  const slug = router.query.slug as string;
  const base = `/w/${slug}/generate`;

  const buildDraftUrl = useCallback(
    (draftId: string) => `${base}/drafts/${draftId}`,
    [base]
  );
  const buildRunUrl = useCallback(
    (runId: string) => `${base}/runs/${runId}`,
    [base]
  );
  const buildListUrl = useCallback(() => base, [base]);

  const navigateToDraft = useCallback(
    (draftId: string) => router.push(buildDraftUrl(draftId)),
    [router, buildDraftUrl]
  );
  const navigateToRun = useCallback(
    (runId: string) => router.push(buildRunUrl(runId)),
    [router, buildRunUrl]
  );

  return useMemo(
    () => ({
      buildDraftUrl,
      buildRunUrl,
      buildListUrl,
      navigateToDraft,
      navigateToRun,
    }),
    [buildDraftUrl, buildRunUrl, buildListUrl, navigateToDraft, navigateToRun]
  );
}
