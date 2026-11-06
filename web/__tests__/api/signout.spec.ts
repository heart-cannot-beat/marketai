import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';
import { Prisma } from '@prisma/client';

import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('next-auth/jwt', () => ({ getToken: jest.fn() }));
jest.mock('../../models/session', () => ({
  deleteSessions: jest.fn().mockResolvedValue({ count: 1 }),
}));

import signOutHandler from '../../pages/api/auth/custom-signout';
import { getToken } from 'next-auth/jwt';
import { deleteSessions } from '../../models/session';

const alreadyGone = () =>
  new Prisma.PrismaClientKnownRequestError('Record to delete does not exist.', {
    code: 'P2025',
    clientVersion: 'test',
  });

/** node-mocks-http normalises header names; read it either way. */
const setCookies = (res: any): string[] => {
  const value =
    res.getHeader('Set-Cookie') ??
    res.getHeaders?.()['set-cookie'] ??
    res._getHeaders?.()['set-cookie'];

  return Array.isArray(value) ? value : value ? [value] : [];
};

/**
 * The same-origin guard compares the request against the configured APP_URL,
 * so derive the trusted origin from it instead of hard-coding a port. CI sets
 * APP_URL for the production build, and a hard-coded value made every
 * same-origin request look cross-origin there.
 */
const appOrigin = new URL(process.env.APP_URL as string);

const request = (headers: Record<string, string> = {}) =>
  createMocks<NextApiRequest, NextApiResponse>({
    method: 'POST',
    headers: {
      host: appOrigin.host,
      origin: appOrigin.origin,
      ...headers,
    },
  });

/**
 * Sign-out must revoke the server-side session before it touches the cookie,
 * and it must not consult anything that mutates the response on failure.
 */
describe('Sign out', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
    (deleteSessions as jest.Mock).mockResolvedValue({ count: 1 });
    (getToken as jest.Mock).mockResolvedValue({ sub: 'user_1', sid: 'sess_1' });
  });

  it('reads the identity from the signed cookie, not from a database session', async () => {
    const { req, res } = request();

    await signOutHandler(req, res);

    // `getServerSession` would hit the database, and NextAuth writes
    // cookie-clearing headers itself when that read fails.
    expect(getToken).toHaveBeenCalled();
    expect(deleteSessions).toHaveBeenCalledWith(['sess_1']);
    expect(res.statusCode).toBe(200);
  });

  it('clears every cookie name NextAuth can use, including the __Secure- one', async () => {
    const { req, res } = request();

    await signOutHandler(req, res);

    const cookies = setCookies(res);

    expect(cookies).toHaveLength(2);

    const secure = cookies.find((c) =>
      c.startsWith('__Secure-next-auth.session-token=')
    );

    expect(
      cookies.find((c) => c.startsWith('next-auth.session-token='))
    ).toBeDefined();
    expect(secure).toBeDefined();
    // The `__Secure-` prefix is ignored unless the cookie is marked Secure.
    expect(secure).toContain('Secure');

    for (const cookie of cookies) {
      expect(cookie).toContain('Max-Age=0');
      expect(cookie).toContain('HttpOnly');
      expect(cookie).toContain('Path=/');
    }
  });

  it('clears cookies when the row was already revoked elsewhere', async () => {
    (deleteSessions as jest.Mock).mockRejectedValue(alreadyGone());

    const { req, res } = request();

    await signOutHandler(req, res);

    expect(res.statusCode).toBe(200);
    expect(setCookies(res)).toHaveLength(2);
  });

  it('fails loudly when revocation fails, and leaves the cookie alone', async () => {
    (deleteSessions as jest.Mock).mockRejectedValue(
      new Error('connection terminated')
    );

    const { req, res } = request();

    await signOutHandler(req, res);

    // Clearing the cookie here would tell the browser it is signed out while
    // every copy of the token kept working.
    expect(res.statusCode).toBe(503);
    expect(setCookies(res)).toHaveLength(0);
    expect(res._getJSONData().error.message).toMatch(/still active/i);
  });

  it('is idempotent when the caller holds no usable token', async () => {
    (getToken as jest.Mock).mockResolvedValue(null);

    const { req, res } = request();

    await signOutHandler(req, res);

    expect(res.statusCode).toBe(200);
    expect(deleteSessions).not.toHaveBeenCalled();
    expect(setCookies(res)).toHaveLength(2);
  });

  it('refuses a cross-origin sign-out', async () => {
    const { req, res } = request({ origin: 'https://evil.example' });

    await signOutHandler(req, res);

    expect(res.statusCode).toBe(403);
    expect(deleteSessions).not.toHaveBeenCalled();
    expect(setCookies(res)).toHaveLength(0);
  });

  it('refuses a cross-site referer when no Origin is present', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      headers: {
        host: appOrigin.host,
        referer: 'https://evil.example/attack',
      },
    });

    await signOutHandler(req, res);

    expect(res.statusCode).toBe(403);
    expect(deleteSessions).not.toHaveBeenCalled();
  });
});
