import env from '@/lib/env';
import { prisma } from '@/lib/prisma';
import { requireSession } from '@/lib/requireSession';

import { PrismaJwksPublisher } from './jwks-publisher';
import type { DelegatedJwksPublisher } from './jwks-publisher';
import { AesGcmPrivateKeyProtector } from './key-protector';
import { PrismaSignerAdapter } from './signer';
import { HmacCsrfVerifier, type CsrfVerifier } from './csrf-verifier';
import {
  ApiError,
  DefaultDelegatedTokenIssuer,
  type DelegatedTokenIssuer,
} from './token-issuer';

/**
 * Process-wide singletons for the delegated-identity module.
 *
 * API routes are re-invoked per request but the module graph is loaded once
 * per Next.js server process, so a lazily-created singleton here avoids
 * rebuilding `PrismaJwksPublisher` on every request. Reuses the shared
 * `prisma` client from `@/lib/prisma` rather than opening a second
 * connection pool; that module already caches the client on `global` in
 * development so hot reloads don't exhaust connections.
 */
let jwksPublisher: DelegatedJwksPublisher | null = null;

export function getJwksPublisher(): DelegatedJwksPublisher {
  if (!jwksPublisher) {
    const maxAge = Number(process.env.SIGNER_CACHE_MAX_AGE ?? '600');
    jwksPublisher = new PrismaJwksPublisher(prisma, maxAge);
  }
  return jwksPublisher;
}

/**
 * Decodes and length-checks `DELEGATED_SIGNING_KEY_ENCRYPTION_KEY` only
 * when a token actually needs to be minted — not at module load, so a
 * process that never calls `getTokenIssuer()` (e.g. most test suites) never
 * has to set it.
 */
function loadSigningKeyEncryptionKey(): Buffer {
  const base64 = env.delegatedIdentity.signingKeyEncryptionKeyBase64;
  if (!base64) {
    throw new ApiError(
      500,
      'signing_key_encryption_key_not_configured',
      false,
      'DELEGATED_SIGNING_KEY_ENCRYPTION_KEY is not set'
    );
  }
  return Buffer.from(base64, 'base64');
}

let tokenIssuer: DelegatedTokenIssuer | null = null;

/**
 * The BFF's sole source of delegated JWTs (spec §1.3, §2.2). Uses
 * `PrismaSignerAdapter` — a fresh decrypt-and-sign per call — rather than
 * `SignerCache`'s in-memory cache: the request path in this milestone is a
 * single low-traffic identity probe, and wiring the cache's background
 * refresh lifecycle into a serverless-friendly singleton is deferred to
 * whichever future ticket first needs the signing throughput it buys.
 */
export function getTokenIssuer(): DelegatedTokenIssuer {
  if (!tokenIssuer) {
    const protector = new AesGcmPrivateKeyProtector(
      loadSigningKeyEncryptionKey()
    );
    const signer = new PrismaSignerAdapter(prisma, protector);
    tokenIssuer = new DefaultDelegatedTokenIssuer(
      prisma,
      signer,
      requireSession,
      {
        issuer: 'urn:marketai:control-plane',
        audience: 'urn:marketai:domain-api',
        maxTtlSeconds: env.delegatedIdentity.tokenMaxTtlSeconds,
      }
    );
  }
  return tokenIssuer;
}

let csrfVerifier: CsrfVerifier | null = null;

export function getCsrfVerifier(): CsrfVerifier {
  if (!csrfVerifier) {
    const secret = env.nextAuth.secret;
    if (!secret) {
      throw new ApiError(
        500,
        'server_misconfigured',
        false,
        'NEXTAUTH_SECRET is not set'
      );
    }
    csrfVerifier = new HmacCsrfVerifier(secret);
  }
  return csrfVerifier;
}
