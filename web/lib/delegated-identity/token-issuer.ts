import crypto from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';
import type { PrismaClient } from '@prisma/client';
import type { DelegatedTokenSigner } from './signer';
import type { AuthenticatedSession } from '../requireSession';

/**
 * Error carried across the `DelegatedTokenIssuer` boundary. Distinct from
 * `web/lib/errors.ts`'s `ApiError` (status + message only): this module's
 * callers (the BFF API route, spec §9's Problem Details mapping) need a
 * stable machine-readable `code` independent of the human-readable
 * `message`, plus a `retryable` flag so a deadline abort (§2.2 "Every await
 * in the Module is guarded by `params.signal`") can be told apart from a
 * client error the caller should not retry.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryable = false,
    message = code
  ) {
    super(message);
  }
}

export interface DelegatedTokenIssuer {
  issueForRequest(params: {
    req: NextApiRequest;
    res: NextApiResponse;
    workspaceSlug: string;
    signal: AbortSignal;
  }): Promise<string>;
}

type IssuerConfig = {
  issuer: string;
  audience: string;
  maxTtlSeconds: number;
};

type RequireSessionFn = (
  req: NextApiRequest,
  res: NextApiResponse
) => Promise<AuthenticatedSession>;

/**
 * The only interface the BFF API route calls to mint a delegated JWT for
 * FastAPI (spec §2.2). Encapsulates the full flow — session verification,
 * workspace/membership resolution, fixed 13-claim assembly (§2.3), and
 * signing — behind one `issueForRequest()` call so no caller can construct
 * a partial or malformed claim set.
 *
 * Deliberately does NOT check `Team.suspended` here. Spec §2.9: "The token
 * issuer signs the current epoch even for a suspended Team; it does not
 * treat that token as authorization to execute a domain handler. FastAPI
 * repairs the projection and then returns `workspace_suspended` before the
 * handler runs." Suspension increments `authorizationEpoch`, so a token
 * minted before suspension is still rejected by FastAPI's epoch check —
 * enforcement lives entirely on the FastAPI side, not at issuance.
 */
export class DefaultDelegatedTokenIssuer implements DelegatedTokenIssuer {
  constructor(
    private readonly prisma: PrismaClient,
    private readonly signer: DelegatedTokenSigner,
    private readonly requireSession: RequireSessionFn,
    private readonly config: IssuerConfig
  ) {}

  async issueForRequest(params: {
    req: NextApiRequest;
    res: NextApiResponse;
    workspaceSlug: string;
    signal: AbortSignal;
  }): Promise<string> {
    const { req, res, workspaceSlug, signal } = params;

    if (signal.aborted) throw new ApiError(504, 'gateway_timeout', true);

    // 1. requireSession(req, res) — throws 401 unless the request carries a
    // live session (session is live, user exists). See requireSession.ts.
    const session = await this.requireSession(req, res);

    if (signal.aborted) throw new ApiError(504, 'gateway_timeout', true);

    // 2. Resolve workspaceSlug -> Team.id (UUID).
    const team = await this.prisma.team.findUnique({
      where: { slug: workspaceSlug },
      select: { id: true },
    });
    if (!team) {
      throw new ApiError(404, 'workspace_not_found');
    }

    if (signal.aborted) throw new ApiError(504, 'gateway_timeout', true);

    // 3. Single-query consistent snapshot: TeamMember JOIN Team WHERE
    // teamId = team.id AND userId = session.user.id. Returns membership_id,
    // role, authorization_epoch, and entitlement_version in one read,
    // avoiding a race between separate queries (spec §2.2 step 3).
    const membership = await this.prisma.teamMember.findFirst({
      where: { teamId: team.id, userId: session.user.id },
      include: {
        team: {
          select: { authorizationEpoch: true, entitlementVersion: true },
        },
      },
    });
    if (!membership) {
      // 4. No active membership -> 403. Also the guard against a forged
      // workspace_id/role: claims are assembled server-side from this
      // query, never trusted from browser input (spec §2.10).
      throw new ApiError(403, 'workspace_membership_required');
    }

    if (signal.aborted) throw new ApiError(504, 'gateway_timeout', true);

    // 5. Assemble the fixed 13-claim payload (spec §2.3). Exactly these
    // keys — FastAPI rejects tokens carrying any additional claim.
    const now = Math.floor(Date.now() / 1000);
    const ttl = Math.min(this.config.maxTtlSeconds, 60);
    const claims: Record<string, unknown> = {
      iss: this.config.issuer,
      aud: this.config.audience,
      sub: session.user.id,
      workspace_id: team.id,
      membership_id: membership.id,
      role: membership.role.toLowerCase(),
      session_id: session.sessionId,
      authorization_epoch: membership.team.authorizationEpoch,
      entitlement_version: membership.team.entitlementVersion,
      jti: `jti_${crypto.randomUUID()}`,
      iat: now,
      nbf: now,
      exp: now + ttl,
    };

    if (signal.aborted) throw new ApiError(504, 'gateway_timeout', true);

    // 6. Sign via DelegatedTokenSigner.sign(claims) — key selection and kid
    // header embedding happen atomically inside sign().
    return this.signer.sign(claims);
  }
}
