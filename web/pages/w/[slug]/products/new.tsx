import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { NewProductFlow } from '@/components/products/NewProductFlow';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/products/new` -- paste → product card → confirm (Issue #162). */
const NewProductPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  if (!bootstrap) return null;
  return <NewProductFlow />;
};

NewProductPage.getLayout = function getLayout(page: ReactElement) {
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
export default NewProductPage;
