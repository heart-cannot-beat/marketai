import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import { MarketingWorkspace } from '@/components/workspace/MarketingWorkspace';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const ExperimentsPage: NextPageWithLayout<Props> = () => {
  return <MarketingWorkspace />;
};

ExperimentsPage.getLayout = function getLayout(page: ReactElement) {
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
  // `assets`: the reuse picker says why an ad of the ad ideas can't be chosen (#225).
  namespaces: ['common', 'library', 'products', 'competitors', 'assets'],
});
export default ExperimentsPage;
