import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { LocalizationWorkspace } from '@/components/localization/LocalizationWorkspace';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const LocalizationPage: NextPageWithLayout<Props> = () => {
  return <LocalizationWorkspace />;
};

LocalizationPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'localization'],
});
export default LocalizationPage;
