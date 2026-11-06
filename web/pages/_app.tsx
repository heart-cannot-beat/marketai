import app from '@/lib/app';
import { SessionProvider } from 'next-auth/react';
import { appWithTranslation } from 'next-i18next';
import Head from 'next/head';
import { Toaster } from 'react-hot-toast';
import type { AppPropsWithLayout } from 'types';

import '../styles/globals.css';
import { useEffect } from 'react';
import env from '@/lib/env';
import { Theme, applyTheme } from '@/lib/theme';
import { AccountLayout } from '@/components/layouts';
import { LocalePreference } from '@/components/shared/LocalePreference';

function MyApp({ Component, pageProps }: AppPropsWithLayout) {
  const { session, ...props } = pageProps;

  // Upstream initialised Mixpanel here. Third-party browser analytics are not
  // part of the control plane: they would receive Workspace routing identity
  // and are an unnecessary egress surface.
  useEffect(() => {
    if (env.darkModeEnabled) {
      applyTheme(localStorage.getItem('theme') as Theme);
    }
  }, []);

  const getLayout =
    Component.getLayout || ((page) => <AccountLayout>{page}</AccountLayout>);

  return (
    <>
      <Head>
        <title>{app.name}</title>
        <link rel="icon" href="/favicon.ico" />
      </Head>
      <SessionProvider session={session}>
        {/* The page's language comes from its URL (`router.locale`), which
            `middleware.ts` sets from the saved preference, the NEXT_LOCALE
            cookie or the browser; this keeps an open tab on the preference. */}
        <LocalePreference />
        <Toaster toastOptions={{ duration: 4000 }} />
        {getLayout(<Component {...props} />)}
      </SessionProvider>
    </>
  );
}

export default appWithTranslation<never>(MyApp);
