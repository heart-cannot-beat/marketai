import Head from 'next/head';
import { ReactElement } from 'react';
import { NextPageWithLayout } from 'types';
import { useSession } from 'next-auth/react';
import { useTranslation } from 'next-i18next';
import { GetServerSidePropsContext } from 'next';
import { serverSideTranslations } from 'next-i18next/serverSideTranslations';

import useInvitation from 'hooks/useInvitation';
import { AuthLayout } from '@/components/layouts';
import { Error, Loading } from '@/components/shared';
import EmailMismatch from '@/components/invitation/EmailMismatch';
import AcceptInvitation from '@/components/invitation/AcceptInvitation';
import NotAuthenticated from '@/components/invitation/NotAuthenticated';

const AcceptTeamInvitation: NextPageWithLayout = () => {
  const { status, data } = useSession();
  const { t } = useTranslation('common');
  const { isLoading, error, invitation } = useInvitation();

  if (isLoading) {
    return <Loading />;
  }

  if (error || !invitation) {
    return <Error message={error.message} />;
  }

  const authUser = data?.user;

  // The only acceptance condition: the signed-in address is the invited one.
  const emailMatch =
    authUser?.email?.trim().toLowerCase() ===
    invitation.email.trim().toLowerCase();

  return (
    <>
      <Head>
        <title>{`${t('invitation-title')} ${invitation.team.name}`}</title>
      </Head>
      <div className="rounded p-6 border">
        <div className="flex flex-col items-center space-y-6">
          <h2 className="font-bold">
            {`${invitation.team.name} ${t('team-invite')}`}
          </h2>

          {/* User not authenticated */}
          {status === 'unauthenticated' && (
            <NotAuthenticated invitation={invitation} />
          )}

          {/* User authenticated and email matches */}
          {status === 'authenticated' && emailMatch && (
            <AcceptInvitation invitation={invitation} />
          )}

          {/* User authenticated and email does not match */}
          {status === 'authenticated' && authUser?.email && !emailMatch && (
            <EmailMismatch email={authUser.email} />
          )}
        </div>
      </div>
    </>
  );
};

AcceptTeamInvitation.getLayout = function getLayout(page: ReactElement) {
  return <AuthLayout>{page}</AuthLayout>;
};

export const getServerSideProps = async (
  context: GetServerSidePropsContext
) => {
  const { locale } = context;

  return {
    props: {
      ...(locale ? await serverSideTranslations(locale, ['common']) : {}),
    },
  };
};

export default AcceptTeamInvitation;
