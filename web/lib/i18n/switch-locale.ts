import { defaultHeaders } from '@/lib/common';
import { assignLocation } from '@/lib/browser-navigation';
import type { ApiResponse } from 'types';
import {
  DEFAULT_LOCALE,
  localizedPath,
  writeLocaleCookie,
  type SupportedLocale,
} from './locales';

/**
 * Saves the signed-in account's interface language (`null`: follow the
 * browser). The account is the session's own; `PUT /api/users` takes no user
 * id.
 */
export const saveLocalePreference = async (
  locale: SupportedLocale | null
): Promise<void> => {
  const response = await fetch('/api/users', {
    method: 'PUT',
    headers: defaultHeaders,
    body: JSON.stringify({ locale }),
  });

  if (!response.ok) {
    const json = (await response
      .json()
      .catch(() => null)) as ApiResponse | null;
    throw new Error(json?.error?.message || `HTTP ${response.status}`);
  }
};

/**
 * Writes the language cookie and reloads the current page in `locale`. A full
 * load, not a client-side transition: every namespace of the page has to come
 * back from the server in the new language. `null` clears the cookie and lets
 * the middleware pick the browser's language.
 */
export const reloadInLocale = (
  locale: SupportedLocale | null,
  asPath: string
) => {
  writeLocaleCookie(locale);
  assignLocation(localizedPath(locale ?? DEFAULT_LOCALE, asPath));
};
