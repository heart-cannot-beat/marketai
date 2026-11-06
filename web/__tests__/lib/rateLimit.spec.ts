import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';
import { enforceRateLimit, resetRateLimit } from '@/lib/rateLimit';
import env from '@/lib/env';

const request = () => {
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
    method: 'POST',
    url: '/api/auth/join',
    headers: { 'x-forwarded-for': '203.0.113.10' },
  });

  return { req, res };
};

describe('Lib - rate limit', () => {
  beforeEach(() => resetRateLimit());

  it('is enabled by default', () => {
    expect(env.rateLimit.enabled).toBe(true);
  });

  it('allows requests up to the auth limit and then answers 429', () => {
    const limit = env.rateLimit.authMaxRequests;

    for (let i = 0; i < limit; i++) {
      const { req, res } = request();
      expect(enforceRateLimit(req, res, 'auth')).toBe(true);
    }

    const { req, res } = request();
    expect(enforceRateLimit(req, res, 'auth')).toBe(false);
    expect(res.statusCode).toBe(429);
    expect(res.getHeader('Retry-After')).toBeDefined();
  });

  it('counts each client separately', () => {
    const limit = env.rateLimit.authMaxRequests;

    for (let i = 0; i < limit + 1; i++) {
      const { req, res } = request();
      enforceRateLimit(req, res, 'auth');
    }

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      url: '/api/auth/join',
      headers: { 'x-forwarded-for': '198.51.100.4' },
    });

    expect(enforceRateLimit(req, res, 'auth')).toBe(true);
  });
});
