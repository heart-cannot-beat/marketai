import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { ProductCompetitors } from '@/components/competitors/ProductCompetitors';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/**
 * `/w/{slug}/competitors` -- the sidebar's Competitors (ADR 0018, design
 * §5.6): pick a product, then its competitors, scans and summary, apart from
 * the ad flow. Exists only while the ads-first switch is on.
 */
const CompetitorsPage: NextPageWithLayout<Props> = ({ bootstrap }) => {
  if (!bootstrap) return null;
  return <ProductCompetitors />;
};

CompetitorsPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'library', 'products', 'competitors'],
  adsFirstOnly: true,
});
export default CompetitorsPage;
