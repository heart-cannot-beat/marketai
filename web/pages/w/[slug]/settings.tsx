import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { TeamSettings } from '@/components/team';
import { MetaConnectionSection } from '@/components/workspace/MetaConnectionSection';
import { ProviderCredentialsSection } from '@/components/workspace/ProviderCredentialsSection';
import { GenerationDefaultsSection } from '@/components/test-plans/GenerationDefaultsSection';
import { PROVIDER_CREDENTIALS_ANCHOR } from '@/components/test-plans/GenerationDefaultsBanner';
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

const SettingsPage: NextPageWithLayout<Props> = () => {
  const { t } = useTranslation(['common', 'settings']);
  const { isLoading, isError, team } = useTeam();
  if (isLoading) return <Loading />;
  if (isError || !team) return <Error message={t('settings:load-error')} />;

  return (
    <>
      <h1 className="text-2xl font-bold outline-none">{t('team-settings')}</h1>
      <TeamSettings team={team} />
      <div className="mt-6 scroll-mt-6" id={PROVIDER_CREDENTIALS_ANCHOR}>
        <ProviderCredentialsSection />
      </div>
      <div className="mt-6">
        <GenerationDefaultsSection />
      </div>
      <div className="mt-6">
        <MetaConnectionSection />
      </div>
    </>
  );
};

SettingsPage.getLayout = function getLayout(page: ReactElement) {
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
  ownerOnly: true,
  namespaces: ['common', 'tests', 'settings'],
});
export default SettingsPage;
