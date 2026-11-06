import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { TestPlanList } from '@/components/test-plans/TestPlanList';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/tests` -- every test plan, newest first (Issue #165). */
const TestsPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  if (!bootstrap) return null;
  return <TestPlanList />;
};

TestsPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'products', 'tests'],
});
export default TestsPage;
