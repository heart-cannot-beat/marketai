import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('../../lib/session', () => ({ getSession: jest.fn() }));
jest.mock('../../models/session', () => ({
  findManySessions: jest.fn().mockResolvedValue([]),
  findFirstSessionOrThrown: jest.fn().mockResolvedValue({}),
  deleteSession: jest.fn().mockResolvedValue({}),
}));

import listSessions from '../../pages/api/sessions/index';
import { getSession } from '../../lib/session';
import { findManySessions } from '../../models/session';

/**
 * The session can be revoked between the middleware check and the handler. When
 * that happens `getSession` returns null, and the old code read
 * `session?.user.id` off it — `undefined`. Prisma drops an `undefined` filter,
 * so `where: { userId: undefined }` was an unfiltered query over every user's
 * sessions.
 */
describe('User-scoped session listing', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
    (findManySessions as jest.Mock).mockResolvedValue([]);
  });

  it('answers 401 when the session was revoked mid-request', async () => {
    (getSession as jest.Mock).mockResolvedValue(null);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await listSessions(req, res);

    expect(res.statusCode).toBe(401);
    // The query must never have been attempted.
    expect(findManySessions).not.toHaveBeenCalled();
  });

  it('answers 401 for the empty "no session" shape', async () => {
    // What `/api/auth/session` returns once a session row is gone.
    (getSession as jest.Mock).mockResolvedValue({});

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await listSessions(req, res);

    expect(res.statusCode).toBe(401);
    expect(findManySessions).not.toHaveBeenCalled();
  });

  it('scopes the query to the signed-in user and to live rows', async () => {
    (getSession as jest.Mock).mockResolvedValue({
      sessionId: 'sess_1',
      user: { id: 'user_1', email: 'owner@example.test' },
    });

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await listSessions(req, res);

    expect(res.statusCode).toBe(200);

    const where = (findManySessions as jest.Mock).mock.calls[0][0].where;

    expect(where.userId).toBe('user_1');
    expect(where.expires.gt).toBeInstanceOf(Date);
  });

  it('never hands the stored session token to the browser', async () => {
    (getSession as jest.Mock).mockResolvedValue({
      sessionId: 'sess_1',
      user: { id: 'user_1', email: 'owner@example.test' },
    });
    (findManySessions as jest.Mock).mockResolvedValue([
      {
        id: 'sess_1',
        userId: 'user_1',
        sessionToken: 'secret-token-value',
        expires: new Date(Date.now() + 1000),
      },
    ]);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await listSessions(req, res);

    const body = res._getData();

    expect(body).not.toContain('secret-token-value');
    expect(res._getJSONData().data[0].isCurrent).toBe(true);
  });
});
