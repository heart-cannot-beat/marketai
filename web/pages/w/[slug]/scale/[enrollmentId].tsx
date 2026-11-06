import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { EnrollmentDetail } from '@/components/scale/detail/EnrollmentDetail';
import { useScaleMetaMode } from '@/lib/scale/meta-mode';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const EnrollmentDetailPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const enrollmentId = router.query.enrollmentId as string;
  const { metaMode } = useScaleMetaMode();

  if (!enrollmentId) return null;

  return <EnrollmentDetail enrollmentId={enrollmentId} metaMode={metaMode} />;
};

EnrollmentDetailPage.getLayout = function getLayout(page: ReactElement) {
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
export default EnrollmentDetailPage;
