import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

const findUnique = jest.fn();
jest.mock('../../../../../lib/prisma', () => ({
  prisma: {
    team: { findUnique: (...args: unknown[]) => findUnique(...args) },
  },
}));

/**
 * `GET /api/internal/domain/workspaces/{workspaceId}/authorization-snapshot`
 * (spec §3.10) — service-token authenticated, not session-authenticated.
 * `lib/env.ts` computes `projectionSyncServiceToken` from
 * `PROJECTION_SYNC_SERVICE_TOKEN` at module load, so each case reloads the
 * handler (and the env module transitively) after setting the variable it
 * needs.
 */
describe('GET /api/internal/domain/workspaces/{workspaceId}/authorization-snapshot', () => {
  const ORIGINAL_TOKEN = process.env.PROJECTION_SYNC_SERVICE_TOKEN;

  const loadHandler = async (token: string | undefined) => {
    jest.resetModules();
    if (token === undefined) {
      delete process.env.PROJECTION_SYNC_SERVICE_TOKEN;
    } else {
      process.env.PROJECTION_SYNC_SERVICE_TOKEN = token;
    }
    const mod =
      await import('../../../../../pages/api/internal/domain/workspaces/[workspaceId]/authorization-snapshot');
    return mod.default;
  };

  beforeEach(() => {
    findUnique.mockReset();
  });

  afterAll(() => {
    if (ORIGINAL_TOKEN === undefined) {
      delete process.env.PROJECTION_SYNC_SERVICE_TOKEN;
    } else {
      process.env.PROJECTION_SYNC_SERVICE_TOKEN = ORIGINAL_TOKEN;
    }
  });

  it('rejects non-GET with 405 and an Allow header', async () => {
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
    });

    await handler(req, res);

    expect(res.statusCode).toBe(405);
    expect(res.getHeader('Allow')).toBe('GET');
    expect(findUnique).not.toHaveBeenCalled();
  });

  it('answers 500 when the service token is not configured, without touching the database', async () => {
    const handler = await loadHandler(undefined);
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_1' },
      headers: { authorization: 'Bearer anything' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(500);
    expect(findUnique).not.toHaveBeenCalled();
  });

  it('answers 401 when the Authorization header is missing', async () => {
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_1' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(401);
    expect(findUnique).not.toHaveBeenCalled();
  });

  it('answers 401 when the Authorization header is not a Bearer token', async () => {
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_1' },
      headers: { authorization: 'Basic svc-token' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(401);
  });

  it('answers 403 when the bearer token does not match', async () => {
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_1' },
      headers: { authorization: 'Bearer wrong-token' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(403);
    expect(findUnique).not.toHaveBeenCalled();
  });

  it('does not throw a raw RangeError on a token length mismatch', async () => {
    // Regression guard, mirrors HmacCsrfVerifier's coverage:
    // `crypto.timingSafeEqual` throws on differing buffer lengths rather
    // than returning `false`.
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_1' },
      headers: { authorization: 'Bearer short' },
    });

    await expect(handler(req, res)).resolves.toBeUndefined();
    expect(res.statusCode).toBe(403);
  });

  it('answers 400 when workspaceId is not a single path segment', async () => {
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: ['a', 'b'] },
      headers: { authorization: 'Bearer svc-token' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(400);
    expect(findUnique).not.toHaveBeenCalled();
  });

  it('answers 404 when the workspace does not exist', async () => {
    findUnique.mockResolvedValue(null);
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_missing' },
      headers: { authorization: 'Bearer svc-token' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(404);
  });

  it('returns the 6-field snapshot with Cache-Control: no-store on success', async () => {
    findUnique.mockResolvedValue({
      id: 'team_1',
      name: 'Acme',
      authorizationEpoch: 3,
      entitlementVersion: 2,
      suspended: false,
      projectionVersion: 7,
    });
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_1' },
      headers: { authorization: 'Bearer svc-token' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(200);
    expect(res.getHeader('Cache-Control')).toBe('no-store');
    expect(res._getJSONData()).toEqual({
      workspace_id: 'team_1',
      workspace_name: 'Acme',
      authorization_epoch: 3,
      entitlement_version: 2,
      suspended: false,
      projection_version: 7,
    });
    expect(findUnique).toHaveBeenCalledWith({
      where: { id: 'team_1' },
      select: {
        id: true,
        name: true,
        authorizationEpoch: true,
        entitlementVersion: true,
        suspended: true,
        projectionVersion: true,
      },
    });
  });

  it('reflects a suspended workspace verbatim in the snapshot', async () => {
    findUnique.mockResolvedValue({
      id: 'team_2',
      name: 'Suspended Co',
      authorizationEpoch: 9,
      entitlementVersion: 1,
      suspended: true,
      projectionVersion: 12,
    });
    const handler = await loadHandler('svc-token');
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
      query: { workspaceId: 'team_2' },
      headers: { authorization: 'Bearer svc-token' },
    });

    await handler(req, res);

    expect(res._getJSONData().suspended).toBe(true);
  });
});
