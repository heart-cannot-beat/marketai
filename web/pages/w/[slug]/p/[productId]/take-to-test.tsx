import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { ProductFrame } from '@/components/workspace/ProductFrame';
import { TakeToTest } from '@/components/tests/TakeToTest';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/p/{productId}/take-to-test` -- the kept ads become a test (ADR 0019, #256). */
const TakeToTestPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  const router = useRouter();
  if (!bootstrap) return null;
  return (
    <ProductFrame productId={router.query.productId as string} view="tests">
      {(product) => <TakeToTest product={product} />}
    </ProductFrame>
  );
};

TakeToTestPage.getLayout = function getLayout(page: ReactElement) {
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
export default TakeToTestPage;
