import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { WorkspaceOverview } from '@/components/workspace/WorkspaceOverview';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/overview` -- what to do next, per product (ADR 0018). */
const OverviewPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  if (!bootstrap) return null;
  return <WorkspaceOverview />;
};

OverviewPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common'],
  adsFirstOnly: true,
});
export default OverviewPage;
