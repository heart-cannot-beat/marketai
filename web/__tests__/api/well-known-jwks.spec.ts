import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

const jwksResponse = jest.fn();

jest.mock('../../lib/delegated-identity/instances', () => ({
  getJwksPublisher: () => ({ jwksResponse }),
}));

import handler from '../../pages/api/.well-known/jwks';

const okResult = {
  body: Buffer.from(JSON.stringify({ keys: [{ kid: 'dsk_1' }] }), 'utf-8'),
  etag: '"sha256:abc123"',
  cacheControl: 'public, max-age=600, must-revalidate',
  status: 200 as const,
};

const notModifiedResult = {
  body: Buffer.alloc(0),
  etag: '"sha256:abc123"',
  cacheControl: 'public, max-age=600, must-revalidate',
  status: 304 as const,
};

describe('GET /.well-known/jwks.json (pages/api/.well-known/jwks)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jwksResponse.mockResolvedValue(okResult);
  });

  it('returns the JWKS body with the publisher headers on a fresh request', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    expect(jwksResponse).toHaveBeenCalledWith(null);
    expect(res.statusCode).toBe(200);
    expect(res.getHeader('Content-Type')).toBe('application/json');
    expect(res.getHeader('Cache-Control')).toBe(okResult.cacheControl);
    expect(res.getHeader('ETag')).toBe(okResult.etag);
    // node-mocks-http's `send()` stores a Buffer body as-is rather than
    // stringifying it, so compare the decoded bytes.
    expect(res._getData().toString()).toBe(okResult.body.toString());
  });

  it('forwards If-None-Match as the request ETag', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      headers: { 'if-none-match': '"sha256:abc123"' },
    });

    await handler(req, res);

    expect(jwksResponse).toHaveBeenCalledWith('"sha256:abc123"');
  });

  it('returns 304 with no body when the publisher reports a match', async () => {
    jwksResponse.mockResolvedValue(notModifiedResult);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      headers: { 'if-none-match': '"sha256:abc123"' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(304);
    expect(res._getData()).toBe('');
    expect(res.getHeader('ETag')).toBe(notModifiedResult.etag);
  });

  it('answers HEAD with headers only, no body', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'HEAD',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(200);
    expect(res._getData()).toBe('');
    expect(res.getHeader('ETag')).toBe(okResult.etag);
  });

  it('rejects non-GET/HEAD methods with 405 and an Allow header', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(405);
    expect(res.getHeader('Allow')).toBe('GET, HEAD');
    expect(jwksResponse).not.toHaveBeenCalled();
  });

  it('does not require any session or authentication input', async () => {
    // No cookie/session header supplied at all; the handler must not reach
    // for one — this is the endpoint FastAPI hits with no BFF session.
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(200);
  });
});
