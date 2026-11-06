import type { SessionStrategy } from 'next-auth';
import type { AUTH_PROVIDER } from 'types';

/**
 * next-auth 4.x rejects a credentials sign-in when the session strategy is
 * `database` and every configured provider is `credentials`:
 *
 *   "Signin in with credentials only supported if JWT strategy is enabled"
 *   (next-auth/core/lib/assert.ts)
 *
 * Upstream never met that guard because it also shipped GitHub, Google, SAML
 * and email providers. This fork is credentials-only by design, so the
 * combination is unusable and must fail as a test rather than as a 500 on the
 * login page.
 */
export const isSupportedSessionStrategy = (
  strategy: SessionStrategy,
  providers: readonly AUTH_PROVIDER[]
): boolean => {
  const onlyCredentials =
    providers.length > 0 && providers.every((p) => p === 'credentials');

  return !(strategy === 'database' && onlyCredentials);
};

export const assertSupportedSessionStrategy = (
  strategy: SessionStrategy,
  providers: readonly AUTH_PROVIDER[]
): void => {
  if (isSupportedSessionStrategy(strategy, providers)) {
    return;
  }

  throw new Error(
    'NEXTAUTH_SESSION_STRATEGY=database cannot be used with a credentials-only ' +
      'provider set: next-auth refuses the sign-in callback. Use "jwt". Server-side ' +
      'session revocation lands with the delegated-identity seam.'
  );
};
