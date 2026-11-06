import type { NextPageWithLayout } from 'types';
import type {
  GetServerSidePropsContext,
  InferGetServerSidePropsType,
} from 'next';
import { serverSideTranslations } from 'next-i18next/serverSideTranslations';

import { getAuthenticatedSession } from '@/lib/requireSession';
import { getUserBySession } from 'models/user';
import { UpdateAccount } from '@/components/account';
import env from '@/lib/env';

type AccountProps = InferGetServerSidePropsType<typeof getServerSideProps>;

const Account: NextPageWithLayout<AccountProps> = ({
  user,
  allowEmailChange,
}) => {
  return <UpdateAccount user={user} allowEmailChange={allowEmailChange} />;
};

export const getServerSideProps = async (
  context: GetServerSidePropsContext
) => {
  const session = await getAuthenticatedSession(context.req, context.res);
  const user = await getUserBySession(session);
  const { locale } = context;

  // The session can lapse between the middleware check and this render.
  // A signed-out visitor belongs at sign-in, not on a 404.
  if (!user) {
    return {
      redirect: { destination: '/auth/login', permanent: false },
    };
  }

  return {
    props: {
      ...(locale ? await serverSideTranslations(locale, ['common']) : {}),
      user: {
        id: user.id,
        email: user.email,
        name: user.name,
        image: user.image,
        locale: user.locale,
      },
      allowEmailChange: env.confirmEmail === false,
    },
  };
};

export default Account;
