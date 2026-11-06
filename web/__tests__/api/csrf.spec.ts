import crypto from 'node:crypto';
import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

jest.mock('../../lib/session', () => ({ getSession: jest.fn() }));

import handler from '../../pages/api/csrf';
import { getSession } from '../../lib/session';

const SECRET = process.env.NEXTAUTH_SECRET as string;

describe('GET /api/csrf', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('issues a token bound to the caller session, matching the HMAC formula', async () => {
    (getSession as jest.Mock).mockResolvedValue({
      sessionId: 'sess_1',
      user: { id: 'user_1', email: 'owner@example.test' },
    });

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(200);
    const expected = crypto
      .createHmac('sha256', SECRET)
      .update('csrf:sess_1')
      .digest('hex');
    expect(res._getJSONData()).toEqual({ token: expected });
  });

  it('sets Cache-Control: no-store on the response', async () => {
    (getSession as jest.Mock).mockResolvedValue({
      sessionId: 'sess_1',
      user: { id: 'user_1', email: 'owner@example.test' },
    });

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    expect(res.getHeader('Cache-Control')).toBe('no-store');
  });

  it('mints a different token for a different session', async () => {
    (getSession as jest.Mock).mockResolvedValue({
      sessionId: 'sess_2',
      user: { id: 'user_2', email: 'other@example.test' },
    });

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    const bodySess1 = crypto
      .createHmac('sha256', SECRET)
      .update('csrf:sess_1')
      .digest('hex');
    expect(res._getJSONData().token).not.toBe(bodySess1);
  });

  it('answers 401 when there is no live session', async () => {
    (getSession as jest.Mock).mockResolvedValue(null);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(401);
  });

  it('answers 401 for the empty "no session" shape', async () => {
    (getSession as jest.Mock).mockResolvedValue({});

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(401);
  });

  it('rejects non-GET methods with 405 and an Allow header', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(405);
    expect(res.getHeader('Allow')).toBe('GET');
    expect(getSession).not.toHaveBeenCalled();
  });
});
