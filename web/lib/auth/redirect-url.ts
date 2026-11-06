/**
 * Keep browser auth redirects on the configured public app origin.
 *
 * Reverse proxies may make the request's apparent origin an internal host such
 * as localhost. Only the requested path and query are trusted as a destination.
 */
export const canonicalAuthRedirectUrl = (
  callbackUrl: string | string[] | undefined,
  token: string | undefined,
  fallbackPath: string,
  appUrl: string
): string => {
  const requestedRedirectUrl =
    (typeof callbackUrl === 'string' && callbackUrl) ||
    (token ? `/invitations/${token}` : fallbackPath);
  const appOrigin = new URL(appUrl).origin;
  const parsedRedirectUrl = new URL(requestedRedirectUrl, appOrigin);
  const canonicalRedirect = new URL(appOrigin);
  canonicalRedirect.pathname = parsedRedirectUrl.pathname;
  canonicalRedirect.search = parsedRedirectUrl.search;
  canonicalRedirect.hash = parsedRedirectUrl.hash;

  return canonicalRedirect.toString();
};

/** Build a login URL using only the incoming request's local route. */
export const authLoginRedirectUrl = (
  appUrl: string,
  pathname: string,
  search: string
): URL => {
  const loginUrl = new URL('/auth/login', appUrl);
  loginUrl.searchParams.set('callbackUrl', `${pathname}${search}`);
  return loginUrl;
};
