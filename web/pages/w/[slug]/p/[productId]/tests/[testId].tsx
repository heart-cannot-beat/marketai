import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { ProductFrame } from '@/components/workspace/ProductFrame';
import { TestInProgress } from '@/components/tests/TestInProgress';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/p/{productId}/tests/{testId}` -- a test in progress (ADR 0019, #257). */
const TestPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  const router = useRouter();
  if (!bootstrap) return null;
  const testId = router.query.testId as string;
  return (
    <ProductFrame productId={router.query.productId as string} view="tests">
      {(product) => (
        <TestInProgress
          key={testId}
          productId={product.productId}
          testId={testId}
        />
      )}
    </ProductFrame>
  );
};

TestPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'ideas', 'tests'],
  adsFirstOnly: true,
});
export default TestPage;
