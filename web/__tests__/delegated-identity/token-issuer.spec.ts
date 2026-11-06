import type { NextApiRequest, NextApiResponse } from 'next';
import {
  ApiError,
  DefaultDelegatedTokenIssuer,
} from '../../lib/delegated-identity/token-issuer';

const BASE_CONFIG = {
  issuer: 'urn:marketai:control-plane',
  audience: 'urn:marketai:domain-api',
  maxTtlSeconds: 60,
};

function makeSession(
  overrides: Partial<{ sessionId: string; userId: string }> = {}
) {
  return {
    sessionId: overrides.sessionId ?? 'sess_001',
    user: { id: overrides.userId ?? 'user_001', email: 'test@test.com' },
  };
}

describe('DefaultDelegatedTokenIssuer', () => {
  it('issues a JWT with exactly the 13 required claims', async () => {
    const now = Math.floor(Date.now() / 1000);
    let signedClaims: Record<string, unknown> = {};
    const mockSigner = {
      sign: jest.fn(async (claims: Record<string, unknown>) => {
        signedClaims = claims;
        return 'mock.jwt.token';
      }),
    };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest.fn().mockResolvedValue({
          id: 'team_uuid_001',
          slug: 'my-workspace',
        }),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue({
          id: 'tm_001',
          role: 'OWNER',
          team: {
            authorizationEpoch: 2,
            entitlementVersion: 1,
          },
        }),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    const req = {} as NextApiRequest;
    const res = {} as NextApiResponse;
    const controller = new AbortController();
    const token = await issuer.issueForRequest({
      req,
      res,
      workspaceSlug: 'my-workspace',
      signal: controller.signal,
    });

    expect(token).toBe('mock.jwt.token');
    expect(signedClaims.iss).toBe('urn:marketai:control-plane');
    expect(signedClaims.aud).toBe('urn:marketai:domain-api');
    expect(signedClaims.sub).toBe('user_001');
    expect(signedClaims.workspace_id).toBe('team_uuid_001');
    expect(signedClaims.membership_id).toBe('tm_001');
    expect(signedClaims.role).toBe('owner');
    expect(signedClaims.session_id).toBe('sess_001');
    expect(signedClaims.authorization_epoch).toBe(2);
    expect(signedClaims.entitlement_version).toBe(1);
    expect(signedClaims.jti).toMatch(/^jti_[0-9a-f-]{36}$/);
    expect(signedClaims.iat).toBeGreaterThanOrEqual(now);
    expect(signedClaims.nbf).toBe(signedClaims.iat);
    expect(
      (signedClaims.exp as number) - (signedClaims.iat as number)
    ).toBeLessThanOrEqual(60);
    expect(Object.keys(signedClaims).sort()).toEqual(
      [
        'aud',
        'authorization_epoch',
        'entitlement_version',
        'exp',
        'iat',
        'iss',
        'jti',
        'membership_id',
        'nbf',
        'role',
        'session_id',
        'sub',
        'workspace_id',
      ].sort()
    );
    expect(Object.keys(signedClaims)).toHaveLength(13);
  });

  it('lowercases MEMBER role', async () => {
    let signedClaims: Record<string, unknown> = {};
    const mockSigner = {
      sign: jest.fn(async (claims: Record<string, unknown>) => {
        signedClaims = claims;
        return 'mock.jwt.token';
      }),
    };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest.fn().mockResolvedValue({ id: 'team_1', slug: 'ws' }),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue({
          id: 'tm_002',
          role: 'MEMBER',
          team: { authorizationEpoch: 0, entitlementVersion: 0 },
        }),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    await issuer.issueForRequest({
      req: {} as NextApiRequest,
      res: {} as NextApiResponse,
      workspaceSlug: 'ws',
      signal: new AbortController().signal,
    });

    expect(signedClaims.role).toBe('member');
  });

  it('caps exp - iat to maxTtlSeconds even when config attempts a longer TTL', async () => {
    let signedClaims: Record<string, unknown> = {};
    const mockSigner = {
      sign: jest.fn(async (claims: Record<string, unknown>) => {
        signedClaims = claims;
        return 'mock.jwt.token';
      }),
    };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest.fn().mockResolvedValue({ id: 'team_1', slug: 'ws' }),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue({
          id: 'tm_002',
          role: 'MEMBER',
          team: { authorizationEpoch: 0, entitlementVersion: 0 },
        }),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      { ...BASE_CONFIG, maxTtlSeconds: 3600 }
    );

    await issuer.issueForRequest({
      req: {} as NextApiRequest,
      res: {} as NextApiResponse,
      workspaceSlug: 'ws',
      signal: new AbortController().signal,
    });

    expect(
      (signedClaims.exp as number) - (signedClaims.iat as number)
    ).toBeLessThanOrEqual(60);
  });

  it('issues distinct jti values per call', async () => {
    const seen: string[] = [];
    const mockSigner = {
      sign: jest.fn(async (claims: Record<string, unknown>) => {
        seen.push(claims.jti as string);
        return 'mock.jwt.token';
      }),
    };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest.fn().mockResolvedValue({ id: 'team_1', slug: 'ws' }),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue({
          id: 'tm_002',
          role: 'OWNER',
          team: { authorizationEpoch: 0, entitlementVersion: 0 },
        }),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    for (let i = 0; i < 3; i += 1) {
      await issuer.issueForRequest({
        req: {} as NextApiRequest,
        res: {} as NextApiResponse,
        workspaceSlug: 'ws',
        signal: new AbortController().signal,
      });
    }

    expect(new Set(seen).size).toBe(3);
  });

  it('propagates requireSession rejection (e.g. unauthenticated session)', async () => {
    const mockSigner = { sign: jest.fn() };
    const mockRequireSession = jest
      .fn()
      .mockRejectedValue(new ApiError(401, 'unauthenticated'));
    const mockPrisma = {
      team: { findUnique: jest.fn() },
      teamMember: { findFirst: jest.fn() },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    await expect(
      issuer.issueForRequest({
        req: {} as NextApiRequest,
        res: {} as NextApiResponse,
        workspaceSlug: 'ws',
        signal: new AbortController().signal,
      })
    ).rejects.toThrow('unauthenticated');
    expect(mockPrisma.team.findUnique).not.toHaveBeenCalled();
    expect(mockSigner.sign).not.toHaveBeenCalled();
  });

  it('throws 404 workspace_not_found when the slug does not resolve to a Team', async () => {
    const mockSigner = { sign: jest.fn() };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: { findUnique: jest.fn().mockResolvedValue(null) },
      teamMember: { findFirst: jest.fn() },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    await expect(
      issuer.issueForRequest({
        req: {} as NextApiRequest,
        res: {} as NextApiResponse,
        workspaceSlug: 'ghost',
        signal: new AbortController().signal,
      })
    ).rejects.toThrow('workspace_not_found');
    expect(mockPrisma.teamMember.findFirst).not.toHaveBeenCalled();
  });

  it('throws 403 workspace_membership_required when user has no membership', async () => {
    const mockSigner = { sign: jest.fn() };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest
          .fn()
          .mockResolvedValue({ id: 'team_uuid_001', slug: 'ws' }),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue(null),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    const err = await issuer
      .issueForRequest({
        req: {} as NextApiRequest,
        res: {} as NextApiResponse,
        workspaceSlug: 'ws',
        signal: new AbortController().signal,
      })
      .catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(403);
    expect(err.code).toBe('workspace_membership_required');
    expect(mockSigner.sign).not.toHaveBeenCalled();
  });

  it('signs the current epoch even when the Team is suspended (spec §2.9 — enforcement is FastAPI-side, not issuance-side)', async () => {
    let signedClaims: Record<string, unknown> = {};
    const mockSigner = {
      sign: jest.fn(async (claims: Record<string, unknown>) => {
        signedClaims = claims;
        return 'mock.jwt.token';
      }),
    };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest
          .fn()
          .mockResolvedValue({ id: 'team_1', slug: 'ws', suspended: true }),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue({
          id: 'tm_001',
          role: 'OWNER',
          team: {
            authorizationEpoch: 5,
            entitlementVersion: 2,
            suspended: true,
          },
        }),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    const token = await issuer.issueForRequest({
      req: {} as NextApiRequest,
      res: {} as NextApiResponse,
      workspaceSlug: 'ws',
      signal: new AbortController().signal,
    });

    expect(token).toBe('mock.jwt.token');
    expect(signedClaims.authorization_epoch).toBe(5);
  });

  it('throws 504 gateway_timeout when the signal is already aborted before requireSession', async () => {
    const mockSigner = { sign: jest.fn() };
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: { findUnique: jest.fn() },
      teamMember: { findFirst: jest.fn() },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    const controller = new AbortController();
    controller.abort();

    const err = await issuer
      .issueForRequest({
        req: {} as NextApiRequest,
        res: {} as NextApiResponse,
        workspaceSlug: 'ws',
        signal: controller.signal,
      })
      .catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(504);
    expect(err.code).toBe('gateway_timeout');
    expect(err.retryable).toBe(true);
    expect(mockRequireSession).not.toHaveBeenCalled();
    expect(mockSigner.sign).not.toHaveBeenCalled();
  });

  it('throws 504 gateway_timeout when the signal aborts after membership lookup, before signing', async () => {
    const mockSigner = { sign: jest.fn() };
    const controller = new AbortController();
    const mockRequireSession = jest.fn().mockResolvedValue(makeSession());
    const mockPrisma = {
      team: {
        findUnique: jest.fn().mockResolvedValue({ id: 'team_1', slug: 'ws' }),
      },
      teamMember: {
        findFirst: jest.fn().mockImplementation(async () => {
          controller.abort();
          return {
            id: 'tm_001',
            role: 'OWNER',
            team: { authorizationEpoch: 0, entitlementVersion: 0 },
          };
        }),
      },
    };

    const issuer = new DefaultDelegatedTokenIssuer(
      mockPrisma as any,
      mockSigner,
      mockRequireSession,
      BASE_CONFIG
    );

    const err = await issuer
      .issueForRequest({
        req: {} as NextApiRequest,
        res: {} as NextApiResponse,
        workspaceSlug: 'ws',
        signal: controller.signal,
      })
      .catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(504);
    expect(mockSigner.sign).not.toHaveBeenCalled();
  });
});
