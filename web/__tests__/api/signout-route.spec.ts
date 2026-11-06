import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('next-auth/jwt', () => ({ getToken: jest.fn() }));
jest.mock('../../models/session', () => ({
  deleteSessions: jest.fn().mockResolvedValue({ count: 1 }),
}));

import nextAuthRoute from '../../pages/api/auth/[...nextauth]';
import { getToken } from 'next-auth/jwt';
import { deleteSessions } from '../../models/session';

const setCookies = (res: any): string[] => {
  const value = res.getHeader('Set-Cookie');
  return Array.isArray(value) ? value : value ? [value] : [];
};

/**
 * NextAuth's own `/api/auth/signout` clears the cookie without touching the
 * server-side session row, so a copy of the token on another device would keep
 * working. The standard route is taken over by the fail-closed custom handler.
 */
describe('The standard NextAuth sign-out route', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
    (deleteSessions as jest.Mock).mockResolvedValue({ count: 1 });
    (getToken as jest.Mock).mockResolvedValue({ sub: 'user_1', sid: 'sess_1' });
  });

  // Derived from the configured APP_URL: the same-origin guard compares
  // against it, so a hard-coded port fails wherever CI sets a different one.
  const appOrigin = new URL(process.env.APP_URL as string);

  const headers = {
    host: appOrigin.host,
    origin: appOrigin.origin,
  };

  it('revokes the server-side session row', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      headers,
      query: { nextauth: ['signout'] },
    });

    await nextAuthRoute(req, res);

    expect(deleteSessions).toHaveBeenCalledWith(['sess_1']);
    expect(res.statusCode).toBe(200);
    expect(setCookies(res)).toHaveLength(2);
  });

  it('fails closed when revocation fails, exactly like the custom route', async () => {
    (deleteSessions as jest.Mock).mockRejectedValue(
      new Error('connection lost')
    );

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      headers,
      query: { nextauth: ['signout'] },
    });

    await nextAuthRoute(req, res);

    expect(res.statusCode).toBe(503);
    expect(setCookies(res)).toHaveLength(0);
  });

  it('does not offer a GET sign-out that skips revocation', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      headers,
      query: { nextauth: ['signout'] },
    });

    await nextAuthRoute(req, res);

    expect(res.statusCode).toBe(405);
    expect(res.getHeader('Allow')).toBe('POST');
    expect(deleteSessions).not.toHaveBeenCalled();
    expect(setCookies(res)).toHaveLength(0);
  });

  it('only matches the exact signout route', async () => {
    // A path that merely contains the word must go to NextAuth, not to the
    // sign-out handler.
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      headers,
      query: { nextauth: ['callback', 'signout'] },
    });

    await nextAuthRoute(req, res);

    expect(deleteSessions).not.toHaveBeenCalled();
  });

  it('refuses a cross-origin sign-out', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      headers: { host: appOrigin.host, origin: 'https://evil.example' },
      query: { nextauth: ['signout'] },
    });

    await nextAuthRoute(req, res);

    expect(res.statusCode).toBe(403);
    expect(deleteSessions).not.toHaveBeenCalled();
  });
});
