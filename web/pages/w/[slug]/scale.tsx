import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { EnrollmentList } from '@/components/scale/list/EnrollmentList';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** Atomic cutover (Issue #25 Task 17): replaces the `FeatureEmptyState`
 * placeholder with the real Scale Enrollment list -- no fixture-backed or
 * legacy path remains reachable from this route. */
const ScalePage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  return (
    <EnrollmentList
      preferredDecisionId={
        typeof router.query.source === 'string'
          ? router.query.source
          : undefined
      }
    />
  );
};

ScalePage.getLayout = function getLayout(page: ReactElement) {
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
export default ScalePage;
