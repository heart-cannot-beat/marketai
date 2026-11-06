import crypto from 'node:crypto';
import { createMocks } from 'node-mocks-http';
import type { NextApiRequest } from 'next';

import { HmacCsrfVerifier } from '../../lib/delegated-identity/csrf-verifier';
import { ApiError } from '../../lib/delegated-identity/token-issuer';
import type { AuthenticatedSession } from '../../lib/requireSession';

const SECRET = 'test-nextauth-secret';

const session = (sessionId: string): AuthenticatedSession => ({
  sessionId,
  user: { id: 'user_1', email: 'user@example.com' },
});

const validTokenFor = (sessionId: string) =>
  crypto.createHmac('sha256', SECRET).update(`csrf:${sessionId}`).digest('hex');

describe('HmacCsrfVerifier', () => {
  const verifier = new HmacCsrfVerifier(SECRET);

  it('returns without checking on GET', async () => {
    const { req } = createMocks<NextApiRequest>({ method: 'GET' });
    await expect(
      verifier.verify(req, session('sess_1'))
    ).resolves.toBeUndefined();
  });

  it('returns without checking on HEAD', async () => {
    const { req } = createMocks<NextApiRequest>({ method: 'HEAD' });
    await expect(
      verifier.verify(req, session('sess_1'))
    ).resolves.toBeUndefined();
  });

  it('accepts a valid token on POST', async () => {
    const sessionId = 'sess_1';
    const { req } = createMocks<NextApiRequest>({
      method: 'POST',
      headers: { 'x-csrf-token': validTokenFor(sessionId) },
    });
    await expect(
      verifier.verify(req, session(sessionId))
    ).resolves.toBeUndefined();
  });

  it('accepts a valid token on PUT, PATCH, and DELETE', async () => {
    const sessionId = 'sess_1';
    for (const method of ['PUT', 'PATCH', 'DELETE'] as const) {
      const { req } = createMocks<NextApiRequest>({
        method,
        headers: { 'x-csrf-token': validTokenFor(sessionId) },
      });
      await expect(
        verifier.verify(req, session(sessionId))
      ).resolves.toBeUndefined();
    }
  });

  it('throws csrf_validation_failed when the header is missing', async () => {
    const { req } = createMocks<NextApiRequest>({ method: 'POST' });

    const result = verifier.verify(req, session('sess_1'));
    await expect(result).rejects.toBeInstanceOf(ApiError);
    await expect(result).rejects.toMatchObject({
      status: 403,
      code: 'csrf_validation_failed',
    });
  });

  it('throws csrf_validation_failed when the header is an empty string', async () => {
    const { req } = createMocks<NextApiRequest>({
      method: 'POST',
      headers: { 'x-csrf-token': '' },
    });

    await expect(verifier.verify(req, session('sess_1'))).rejects.toMatchObject(
      { status: 403, code: 'csrf_validation_failed' }
    );
  });

  it('throws csrf_validation_failed when the token does not match', async () => {
    const { req } = createMocks<NextApiRequest>({
      method: 'POST',
      headers: { 'x-csrf-token': 'not-the-right-token' },
    });

    await expect(verifier.verify(req, session('sess_1'))).rejects.toMatchObject(
      { status: 403, code: 'csrf_validation_failed' }
    );
  });

  it('rejects a token minted for a different session', async () => {
    const tokenForOtherSession = validTokenFor('sess_other');
    const { req } = createMocks<NextApiRequest>({
      method: 'POST',
      headers: { 'x-csrf-token': tokenForOtherSession },
    });

    await expect(verifier.verify(req, session('sess_1'))).rejects.toMatchObject(
      { status: 403, code: 'csrf_validation_failed' }
    );
  });

  it('rejects a token minted with a different secret', async () => {
    const sessionId = 'sess_1';
    const tokenFromOtherSecret = crypto
      .createHmac('sha256', 'a-different-secret')
      .update(`csrf:${sessionId}`)
      .digest('hex');
    const { req } = createMocks<NextApiRequest>({
      method: 'POST',
      headers: { 'x-csrf-token': tokenFromOtherSecret },
    });

    await expect(
      verifier.verify(req, session(sessionId))
    ).rejects.toMatchObject({ status: 403, code: 'csrf_validation_failed' });
  });

  it('does not throw a raw RangeError on a length mismatch', async () => {
    // Regression guard: `crypto.timingSafeEqual` throws on differing buffer
    // lengths rather than returning `false`. A header shorter or longer than
    // the expected 64 hex chars must still fail as `ApiError(403, ...)`, not
    // crash the handler with an uncaught RangeError.
    const { req } = createMocks<NextApiRequest>({
      method: 'POST',
      headers: { 'x-csrf-token': 'short' },
    });

    await expect(verifier.verify(req, session('sess_1'))).rejects.toMatchObject(
      { status: 403, code: 'csrf_validation_failed' }
    );
  });
});
