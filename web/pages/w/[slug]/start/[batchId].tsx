import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { ReadingPage as Reading } from '@/components/ideas/ReadingPage';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/**
 * `/w/{slug}/start/{batchId}` -- reading the product and making the first ads
 * (ADR 0018, #195). The batch keeps going with the page closed; coming back
 * here shows how far it got.
 */
const ReadingPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  const router = useRouter();
  if (!bootstrap) return null;
  const batchId = router.query.batchId as string;
  return <Reading key={batchId} batchId={batchId} />;
};

ReadingPage.getLayout = function getLayout(page: ReactElement) {
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
export default ReadingPage;
