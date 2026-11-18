import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { ProductFrame } from '@/components/workspace/ProductFrame';
import { ProductProfile } from '@/components/ideas/ProductProfile';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/** `/w/{slug}/p/{productId}/profile` -- a product's product profile (ADR 0018, #202). */
const ProfilePage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  const router = useRouter();
  if (!bootstrap) return null;
  return (
    <ProductFrame productId={router.query.productId as string} view="profile">
      {(product) => <ProductProfile product={product} />}
    </ProductFrame>
  );
};

ProfilePage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'ideas', 'products', 'library'],
  adsFirstOnly: true,
});
export default ProfilePage;
