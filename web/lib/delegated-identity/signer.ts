import crypto from 'node:crypto';
import type { PrismaClient } from '@prisma/client';
import type { AesGcmPrivateKeyProtector, KeyBinding } from './key-protector';

/**
 * Mints short-lived delegated BFF-to-FastAPI JWTs. Implementations own key
 * selection: there is no separate `current_kid()` accessor, because
 * selecting the ACTIVE key and embedding its `kid` in the JWT header must
 * happen atomically (spec §1.3) — otherwise the ACTIVE key could rotate
 * between selection and signing, producing a header `kid` that does not
 * match the key that actually signed the token.
 */
export interface DelegatedTokenSigner {
  sign(claims: Record<string, unknown>, tokenType?: string): Promise<string>;
}

/**
 * The base64-encoded shape `protectedPrivateKey` (a Prisma `Json` column)
 * is stored as. `AesGcmPrivateKeyProtector.ProtectedKey` uses raw Buffers;
 * this is the serialization boundary between the two (see Task 4 brief).
 */
type StoredProtectedPrivateKey = {
  ciphertext: string;
  iv: string;
  authTag: string;
};

function base64url(buf: Buffer): string {
  return buf.toString('base64url');
}

/**
 * Signs `signingInput` with an ES256 (P-256 + SHA-256) private key,
 * returning the raw IEEE P1363 (r || s) signature JWS requires — not the
 * DER encoding `crypto.createSign` produces by default.
 */
function signEs256(signingInput: string, privateKeyPem: Buffer): Buffer {
  const sign = crypto.createSign('SHA256');
  sign.update(signingInput);
  return sign.sign({ key: privateKeyPem, dsaEncoding: 'ieee-p1363' });
}

/**
 * Decodes the base64 fields Prisma returns for `protectedPrivateKey` back
 * into the `Buffer`-based `ProtectedKey` shape `AesGcmPrivateKeyProtector`
 * expects.
 */
function toProtectedKey(stored: StoredProtectedPrivateKey) {
  return {
    ciphertext: Buffer.from(stored.ciphertext, 'base64'),
    iv: Buffer.from(stored.iv, 'base64'),
    authTag: Buffer.from(stored.authTag, 'base64'),
  };
}

/**
 * `DelegatedTokenSigner` backed directly by Prisma: reads the ACTIVE
 * `DelegatedSigningKey` row, decrypts its private key, and signs on every
 * call. Suitable for low-volume or non-request-path signing (e.g. tests,
 * scripts, admin tooling); the request path uses `SignerCache` instead so
 * every request does not pay for a database round-trip and a decrypt.
 */
export class PrismaSignerAdapter implements DelegatedTokenSigner {
  constructor(
    private readonly prisma: PrismaClient,
    private readonly protector: AesGcmPrivateKeyProtector
  ) {}

  async sign(
    claims: Record<string, unknown>,
    tokenType?: string
  ): Promise<string> {
    const activeKey = await this.prisma.delegatedSigningKey.findFirst({
      where: { state: 'ACTIVE' },
    });
    if (!activeKey) {
      throw new Error('No ACTIVE signing key available');
    }

    const binding: KeyBinding = {
      kid: activeKey.kid,
      algorithm: activeKey.algorithm,
      publicJwkThumbprint: activeKey.publicJwkThumbprint,
    };

    const stored =
      activeKey.protectedPrivateKey as unknown as StoredProtectedPrivateKey;
    const privateKeyPem = this.protector.unprotect(
      toProtectedKey(stored),
      binding
    );

    const header = base64url(
      Buffer.from(
        JSON.stringify({
          alg: 'ES256',
          typ: tokenType ?? 'delegated+jwt',
          kid: activeKey.kid,
        })
      )
    );
    const body = base64url(Buffer.from(JSON.stringify(claims)));
    const signingInput = `${header}.${body}`;
    const signature = signEs256(signingInput, privateKeyPem);

    return `${signingInput}.${base64url(signature)}`;
  }
}
