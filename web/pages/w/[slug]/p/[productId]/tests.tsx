import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { ProductFrame } from '@/components/workspace/ProductFrame';
import { ProductTestsPlaceholder } from '@/components/workspace/ProductViews';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/p/{productId}/tests` -- a product's tests (ADR 0018). */
const TestsPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  const router = useRouter();
  if (!bootstrap) return null;
  return (
    <ProductFrame productId={router.query.productId as string} view="tests">
      {() => <ProductTestsPlaceholder />}
    </ProductFrame>
  );
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
  namespaces: ['common'],
  adsFirstOnly: true,
});
export default TestsPage;
