import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { StartPage as Start } from '@/components/ideas/StartPage';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/start` -- paste a product link and start (ADR 0018, #195). */
const StartPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  if (!bootstrap) return null;
  return <Start />;
};

StartPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'ideas', 'products'],
  adsFirstOnly: true,
});
export default StartPage;
