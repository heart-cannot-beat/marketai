/**
 * Interface languages (ADR 0013: the interface language is a user setting
 * that defaults to the browser; the strategy language follows it; the ad
 * language is a plan field and is not decided here).
 *
 * Kept free of Node and React imports: `middleware.ts` runs it on the Edge
 * runtime, and `next-i18next.config.js` (CommonJS, read by Next itself) cannot
 * import TypeScript, so `__tests__/locales/locale-config.spec.ts` checks that
 * both declare the same list.
 */
export const SUPPORTED_LOCALES = ['en', 'zh-CN'] as const;

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

/**
 * The strategy language AI drafts a new plan in: the interface language
 * (ADR 0013). The ad language is a separate plan field.
 */
export const strategyLanguageFor = (locale: string | null | undefined) =>
  locale?.startsWith('zh') ? ('zh-CN' as const) : ('en-US' as const);

/** Served without a path prefix; every other locale is `/<locale>/...`. */
export const DEFAULT_LOCALE: SupportedLocale = 'en';

/** The cookie Next.js itself reads for locale detection on `/`. */
export const LOCALE_COOKIE = 'NEXT_LOCALE';

const LOCALE_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export const isSupportedLocale = (value: unknown): value is SupportedLocale =>
  typeof value === 'string' &&
  (SUPPORTED_LOCALES as readonly string[]).includes(value);

/**
 * The supported locale a single language tag asks for, if any. Any Chinese
 * tag maps to zh-CN, the only Chinese interface there is; any English tag to
 * en.
 */
const localeForTag = (tag: string): SupportedLocale | null => {
  const primary = tag.trim().toLowerCase().split('-')[0];
  if (primary === 'zh') return 'zh-CN';
  if (primary === 'en') return 'en';
  return null;
};

/**
 * Best supported locale for an `Accept-Language` header, by quality value and
 * then by position. Tags the interface does not offer are skipped; with none
 * left, the default locale.
 */
export const matchAcceptLanguage = (
  header: string | null | undefined
): SupportedLocale => {
  if (!header) return DEFAULT_LOCALE;

  const ranked = header
    .split(',')
    .map((part, index) => {
      const [tag, ...params] = part.trim().split(';');
      const qParam = params
        .map((param) => param.trim())
        .find((param) => param.startsWith('q='));
      const quality = qParam ? Number(qParam.slice(2)) : 1;
      return {
        tag,
        index,
        quality: Number.isFinite(quality) ? quality : 0,
      };
    })
    .filter(({ tag, quality }) => tag && quality > 0)
    .sort((a, b) => b.quality - a.quality || a.index - b.index);

  for (const { tag } of ranked) {
    if (tag === '*') return DEFAULT_LOCALE;
    const locale = localeForTag(tag);
    if (locale) return locale;
  }

  return DEFAULT_LOCALE;
};

/**
 * The interface language for one request: the signed-in user's saved
 * preference, then the language cookie, then the browser. A cleared
 * preference (`null`) means "follow the browser", so the cookie and the
 * header decide.
 */
export const resolveLocale = ({
  userLocale,
  cookieLocale,
  acceptLanguage,
}: {
  userLocale?: string | null;
  cookieLocale?: string | null;
  acceptLanguage?: string | null;
}): SupportedLocale => {
  if (isSupportedLocale(userLocale)) return userLocale;
  if (isSupportedLocale(cookieLocale)) return cookieLocale;
  return matchAcceptLanguage(acceptLanguage);
};

/**
 * `path` (a route without any locale prefix, as `router.asPath` reports it)
 * under `locale`'s prefix.
 */
export const localizedPath = (locale: SupportedLocale, path: string) => {
  const route = path.startsWith('/') ? path : `/${path}`;
  if (locale === DEFAULT_LOCALE) return route;
  if (route === '/') return `/${locale}`;
  if (route.startsWith('/?') || route.startsWith('/#')) {
    return `/${locale}${route.slice(1)}`;
  }
  return `/${locale}${route}`;
};

/** Attributes shared by the browser and the middleware when writing the cookie. */
export const localeCookieOptions = (secure: boolean) => ({
  path: '/',
  maxAge: LOCALE_COOKIE_MAX_AGE_SECONDS,
  sameSite: 'lax' as const,
  secure,
});

/** Reads the language cookie in the browser. */
export const readLocaleCookie = (): string | null => {
  if (typeof document === 'undefined') return null;
  const entry = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${LOCALE_COOKIE}=`));
  return entry
    ? decodeURIComponent(entry.slice(LOCALE_COOKIE.length + 1))
    : null;
};

/**
 * Writes (or, with `null`, clears) the language cookie in the browser.
 * Returns whether the browser kept it, so callers do not reload into a
 * language the next request cannot see.
 */
export const writeLocaleCookie = (locale: SupportedLocale | null): boolean => {
  if (typeof document === 'undefined') return false;
  const { path, maxAge, sameSite } = localeCookieOptions(false);
  const secure = window.location.protocol === 'https:' ? '; Secure' : '';
  document.cookie =
    locale === null
      ? `${LOCALE_COOKIE}=; Path=${path}; Max-Age=0; SameSite=${sameSite}${secure}`
      : `${LOCALE_COOKIE}=${encodeURIComponent(locale)}; Path=${path}; Max-Age=${maxAge}; SameSite=${sameSite}${secure}`;
  return readLocaleCookie() === locale;
};
