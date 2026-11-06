import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { Members } from '@/components/team';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import useTeam from 'hooks/useTeam';
import { Loading, Error } from '@/components/shared';
import { useTranslation } from 'next-i18next';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

const MembersPage: NextPageWithLayout<Props> = () => {
  const { t } = useTranslation('common');
  const { isLoading, isError, team } = useTeam();
  if (isLoading) return <Loading />;
  if (isError || !team) return <Error message="Could not load members" />;

  return (
    <>
      <h1 className="text-2xl font-bold outline-none">{t('members')}</h1>
      <Members team={team} />
    </>
  );
};

MembersPage.getLayout = function getLayout(page: ReactElement) {
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

export const getServerSideProps = getWorkspacePageProps({ ownerOnly: true });
export default MembersPage;
