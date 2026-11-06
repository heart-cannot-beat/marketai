import type { ReactElement } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { BreakdownTable } from '@/components/scale/detail/BreakdownTable';
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

/** Deep link to one Enrollment's breakdown view (spec §4.2). Backed by the
 * `get_scale_enrollment_breakdowns` `501 not_implemented` stub today --
 * see `BreakdownTable`'s doc comment. */
const EnrollmentBreakdownsPage: NextPageWithLayout<Props> = () => {
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
        {t('breakdowns.heading')}
      </h1>
      <div className="mt-4">
        <BreakdownTable enrollmentId={enrollmentId} metaMode={metaMode} />
      </div>
    </>
  );
};

EnrollmentBreakdownsPage.getLayout = function getLayout(page: ReactElement) {
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
export default EnrollmentBreakdownsPage;
