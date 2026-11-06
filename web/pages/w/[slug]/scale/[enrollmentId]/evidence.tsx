import type { ReactElement } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { EvidenceTimeline } from '@/components/scale/detail/EvidenceTimeline';
import { useScaleNav } from '@/hooks/scale/useScaleNav';
import { useScaleMetaMode } from '@/lib/scale/meta-mode';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** Deep link to one Enrollment's evidence condition axis (spec §4.4) --
 * the same `EvidenceTimeline` the detail page embeds inside `EpisodePanel`,
 * given a focused, shareable/bookmarkable URL of its own. */
const EnrollmentEvidencePage: NextPageWithLayout<Props> = () => {
  const { t } = useTranslation('scale');
  const router = useRouter();
  const enrollmentId = router.query.enrollmentId as string;
  const { metaMode } = useScaleMetaMode();
  const { buildEnrollmentUrl } = useScaleNav();

  if (!enrollmentId) return null;

  return (
    <>
      <Link
        href={buildEnrollmentUrl(enrollmentId)}
        className="text-sm text-base-content/60 hover:underline"
      >
        {t('detail.back-to-enrollment')}
      </Link>
      <h1 className="mt-1 text-2xl font-bold outline-none">
        {t('evidence.heading')}
      </h1>
      <EvidenceTimeline enrollmentId={enrollmentId} metaMode={metaMode} />
    </>
  );
};

EnrollmentEvidencePage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout
      bootstrap={bootstrap}
      errorCode={errorCode}
      errorContext={errorContext}
    >
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps({
  namespaces: ['common', 'scale'],
});
export default EnrollmentEvidencePage;
