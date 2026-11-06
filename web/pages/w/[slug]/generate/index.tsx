import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { GenerationListView } from '@/components/generation/list/GenerationListView';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import { FirstValueChecklist } from '@/components/onboarding/FirstValueChecklist';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const GenerateListPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  if (!bootstrap) return null;
  return (
    <>
      <FirstValueChecklist slug={bootstrap.slug} />
      <GenerationListView />
    </>
  );
};

GenerateListPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'products'],
});
export default GenerateListPage;
