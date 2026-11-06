import { useEffect, useRef } from 'react';
import { useRouter } from 'next/router';
import { useSession } from 'next-auth/react';
import { replaceLocation } from '@/lib/browser-navigation';
import {
  isSupportedLocale,
  localizedPath,
  writeLocaleCookie,
} from '@/lib/i18n/locales';

/**
 * Keeps an open tab on the signed-in account's saved interface language.
 *
 * Full page loads are already moved there by `middleware.ts`. This covers what
 * the middleware cannot see: client-side transitions, pages it serves without
 * a session lookup (invitations), and a preference changed in another tab or
 * device that a session refresh brings in. It reloads at most once per mount,
 * and only when the browser kept the cookie, so a blocked cookie cannot turn
 * into a reload loop.
 */
export function LocalePreference() {
  const router = useRouter();
  const { data } = useSession();
  const preferred = data?.user?.locale;
  const reloaded = useRef(false);

  useEffect(() => {
    if (reloaded.current || !router.isReady) return;
    if (!isSupportedLocale(preferred) || preferred === router.locale) return;
    if (!writeLocaleCookie(preferred)) return;
    reloaded.current = true;
    replaceLocation(localizedPath(preferred, router.asPath));
  }, [preferred, router.isReady, router.locale, router.asPath]);

  return null;
}

export default LocalePreference;
