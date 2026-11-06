import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { TestPlanView } from '@/components/test-plans/TestPlanView';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/tests/{planId}` -- direction cards, sample and full runs
 * (Issue #165). */
const TestPlanPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  const router = useRouter();
  const planId =
    typeof router.query.planId === 'string' ? router.query.planId : null;
  if (!bootstrap || !planId) return null;
  return <TestPlanView key={planId} planId={planId} />;
};

TestPlanPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'products', 'tests', 'learnings'],
});
export default TestPlanPage;
