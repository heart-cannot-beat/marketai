import {
  assertSupportedSessionStrategy,
  isSupportedSessionStrategy,
} from '@/lib/sessionStrategy';
import { authProviderEnabled } from '@/lib/auth';
import env from '@/lib/env';

/**
 * Regression guard.
 *
 * Reducing the provider set to credentials-only made `database` sessions
 * unusable: next-auth's assert rejects the sign-in callback, which showed up as
 * a 500 on the login page rather than as a configuration error.
 */
describe('Session strategy', () => {
  it('is a strategy that actually works with the configured providers', () => {
    const providers = Object.entries(authProviderEnabled())
      .filter(([, enabled]) => enabled)
      .map(([name]) => name as 'credentials');

    expect(providers).toEqual(['credentials']);
    expect(
      isSupportedSessionStrategy(env.nextAuth.sessionStrategy, providers)
    ).toBe(true);
    expect(env.nextAuth.sessionStrategy).toBe('jwt');
  });

  it('rejects database sessions with a credentials-only provider set', () => {
    expect(isSupportedSessionStrategy('database', ['credentials'])).toBe(false);
    expect(() =>
      assertSupportedSessionStrategy('database', ['credentials'])
    ).toThrow(/credentials-only/);
  });

  it('allows jwt sessions with a credentials-only provider set', () => {
    expect(isSupportedSessionStrategy('jwt', ['credentials'])).toBe(true);
    expect(() =>
      assertSupportedSessionStrategy('jwt', ['credentials'])
    ).not.toThrow();
  });
});
