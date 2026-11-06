import crypto from 'node:crypto';
import type { PrismaClient } from '@prisma/client';

export type JwksHttpResponse = {
  readonly body: Buffer;
  readonly etag: string;
  readonly cacheControl: string;
  readonly status: 200 | 304;
};

/**
 * Serves the public half of ACTIVE and RETIRING signing keys at
 * `/.well-known/jwks.json` (spec §1.4). PREPUBLISHED, RETIRED and REVOKED
 * keys are never published: PREPUBLISHED is pre-rotation and not yet
 * trusted, RETIRED has passed its verification window, and REVOKED is an
 * emergency invalidation that must disappear immediately.
 */
export interface DelegatedJwksPublisher {
  jwksResponse(requestEtag: string | null): Promise<JwksHttpResponse>;
}

/**
 * `DelegatedJwksPublisher` backed by Prisma. The ETag is the SHA-256 hash
 * of the canonical response bytes, not derived from key state — so a state
 * transition that does not change which keys are published (e.g. ACTIVE
 * rotating to RETIRING while the new ACTIVE key is already published)
 * produces identical bytes and does not spuriously invalidate caches
 * (spec §1.3).
 */
export class PrismaJwksPublisher implements DelegatedJwksPublisher {
  constructor(
    private readonly prisma: PrismaClient,
    private readonly maxAge: number
  ) {}

  async jwksResponse(requestEtag: string | null): Promise<JwksHttpResponse> {
    const keys = await this.prisma.delegatedSigningKey.findMany({
      where: { state: { in: ['ACTIVE', 'RETIRING'] } },
      select: { kid: true, publicJwk: true },
      orderBy: { createdAt: 'asc' },
    });

    const jwks = { keys: keys.map((k) => k.publicJwk) };
    const body = Buffer.from(JSON.stringify(jwks), 'utf-8');
    const hash = crypto.createHash('sha256').update(body).digest('hex');
    const etag = `"sha256:${hash}"`;
    const cacheControl = `public, max-age=${this.maxAge}, must-revalidate`;

    if (requestEtag !== null && requestEtag === etag) {
      return { body: Buffer.alloc(0), etag, cacheControl, status: 304 };
    }

    return { body, etag, cacheControl, status: 200 };
  }
}
