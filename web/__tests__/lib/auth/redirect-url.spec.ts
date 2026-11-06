import {
  authLoginRedirectUrl,
  canonicalAuthRedirectUrl,
} from '@/lib/auth/redirect-url';

const APP_URL = 'https://marketai-web-production.up.railway.app';

describe('authentication redirect URLs', () => {
  it('keeps the requested route but replaces an internal callback origin', () => {
    expect(
      canonicalAuthRedirectUrl(
        'https://localhost:4002/dashboard?tab=security#keys',
        undefined,
        '/dashboard',
        APP_URL
      )
    ).toBe(`${APP_URL}/dashboard?tab=security#keys`);
  });

  it('builds the login callback from only the local path and query', () => {
    const loginUrl = authLoginRedirectUrl(
      APP_URL,
      '/dashboard',
      '?tab=security'
    );

    expect(loginUrl.origin).toBe(APP_URL);
    expect(loginUrl.pathname).toBe('/auth/login');
    expect(loginUrl.searchParams.get('callbackUrl')).toBe(
      '/dashboard?tab=security'
    );
    expect(loginUrl.toString()).not.toContain('localhost');
  });

  it('keeps invitation and fallback redirects on the configured app origin', () => {
    expect(
      canonicalAuthRedirectUrl(undefined, 'invite-token', '/dashboard', APP_URL)
    ).toBe(`${APP_URL}/invitations/invite-token`);
    expect(
      canonicalAuthRedirectUrl(undefined, undefined, '/dashboard', APP_URL)
    ).toBe(`${APP_URL}/dashboard`);
  });
});
