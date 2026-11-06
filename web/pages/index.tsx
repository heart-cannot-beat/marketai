import { type ReactElement } from 'react';
import type { NextPageWithLayout } from 'types';
import { Loading } from '@/components/shared';

/**
 * There is no public landing page.
 *
 * Upstream shipped a marketing page with hero, feature, pricing and FAQ
 * sections plus a "Sign up" call to action. MarketAI's Beta is invitation-only,
 * so the root route always redirects to sign-in and the marketing components
 * were removed from the fork.
 */
const Home: NextPageWithLayout = () => <Loading />;

export const getServerSideProps = async () => {
  return {
    redirect: {
      destination: '/auth/login',
      permanent: false,
    },
  };
};

Home.getLayout = function getLayout(page: ReactElement) {
  return <>{page}</>;
};

export default Home;
