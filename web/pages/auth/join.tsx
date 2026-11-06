import Link from 'next/link';
import toast from 'react-hot-toast';
import { useRouter } from 'next/router';
import { useSession } from 'next-auth/react';
import { useTranslation } from 'next-i18next';
import { GetServerSidePropsContext, InferGetServerSidePropsType } from 'next';
import { type ReactElement, useEffect } from 'react';
import { serverSideTranslations } from 'next-i18next/serverSideTranslations';

import type { NextPageWithLayout } from 'types';
import { authProviderEnabled } from '@/lib/auth';
import { AuthLayout } from '@/components/layouts';
import { JoinWithInvitation } from '@/components/auth';
import Head from 'next/head';
import { Loading } from '@/components/shared';
import env from '@/lib/env';

const Signup: NextPageWithLayout<
  InferGetServerSidePropsType<typeof getServerSideProps>
> = ({ authProviders }) => {
  const router = useRouter();
  const { status } = useSession();
  const { t } = useTranslation('common');

  const { error, token } = router.query as {
    error: string;
    token: string;
  };

  useEffect(() => {
    if (error) {
      toast.error(t(error));
    }
  }, [error, t]);

  if (status === 'loading') {
    return <Loading />;
  }

  if (status === 'authenticated') {
    router.push(env.redirectIfAuthenticated);
  }

  const params = token ? `?token=${token}` : '';

  return (
    <>
      <Head>
        <title>{t('sign-up-title')}</title>
      </Head>
      <div className="rounded p-6 border">
        {authProviders.credentials && (
          <JoinWithInvitation inviteToken={token} />
        )}
      </div>
      <p className="text-center text-sm text-gray-600 mt-3">
        {t('already-have-an-account')}
        <Link
          href={`/auth/login${params}`}
          className="font-medium text-primary hover:text-[color-mix(in_oklab,oklch(var(--p)),black_7%)]"
        >
          &nbsp;{t('sign-in')}
        </Link>
      </p>
    </>
  );
};

Signup.getLayout = function getLayout(page: ReactElement) {
  return (
    <AuthLayout heading="get-started" description="create-a-new-account">
      {page}
    </AuthLayout>
  );
};

export const getServerSideProps = async (
  context: GetServerSidePropsContext
) => {
  const { locale } = context;

  // Sign-up exists only as the second half of an invitation. Reaching this
  // page without a token is not a sign-up funnel entry point.
  if (!context.query.token) {
    return {
      redirect: {
        destination: '/auth/login?error=invitation-required',
        permanent: false,
      },
    };
  }

  return {
    props: {
      ...(locale ? await serverSideTranslations(locale, ['common']) : {}),
      authProviders: authProviderEnabled(),
    },
  };
};

export default Signup;
