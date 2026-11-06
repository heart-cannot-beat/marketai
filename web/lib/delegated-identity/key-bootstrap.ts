import crypto from 'node:crypto';
import type { Prisma, PrismaClient } from '@prisma/client';

import { AesGcmPrivateKeyProtector } from './key-protector';

export type DelegatedSigningKeyBootstrapResult = {
  readonly created: boolean;
  readonly kid: string;
};

type DelegatedSigningKeyStore = Pick<PrismaClient, 'delegatedSigningKey'>;

const CROCKFORD_BASE32 = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

const generateUlid = (): string => {
  let timestamp = Date.now();
  const timePart = new Array<string>(10);
  for (let index = timePart.length - 1; index >= 0; index--) {
    timePart[index] = CROCKFORD_BASE32[timestamp % 32];
    timestamp = Math.floor(timestamp / 32);
  }

  let randomPart = '';
  let accumulator = 0;
  let bits = 0;
  const randomBytes = crypto.randomBytes(10);
  for (let index = 0; index < randomBytes.length; index++) {
    const byte = randomBytes[index];
    accumulator = (accumulator << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      bits -= 5;
      randomPart += CROCKFORD_BASE32[(accumulator >>> bits) & 31];
    }
  }

  return `${timePart.join('')}${randomPart}`;
};

const requirePublicCoordinate = (
  value: string | undefined,
  name: string
): string => {
  if (!value) {
    throw new Error(`Generated P-256 public key is missing ${name}`);
  }
  return value;
};

/**
 * Ensure a fresh control-plane database has the initial ACTIVE ES256 key.
 *
 * This is intentionally an explicit operator/bootstrap operation rather than
 * an application-startup side effect. Re-running it is safe: once an ACTIVE
 * key exists it leaves the keyring untouched, so normal restarts never rotate
 * signing material unexpectedly.
 */
export async function ensureActiveDelegatedSigningKey(
  prisma: DelegatedSigningKeyStore,
  encryptionKey: Buffer
): Promise<DelegatedSigningKeyBootstrapResult> {
  const existing = await prisma.delegatedSigningKey.findFirst({
    where: { state: 'ACTIVE' },
    select: { kid: true },
  });

  if (existing) {
    return { created: false, kid: existing.kid };
  }

  const id = `dsk_${generateUlid()}`;
  const kid = id;
  const algorithm = 'ES256';
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ec', {
    namedCurve: 'P-256',
  });
  const exportedPublicJwk = publicKey.export({ format: 'jwk' });
  const x = requirePublicCoordinate(exportedPublicJwk.x, 'x');
  const y = requirePublicCoordinate(exportedPublicJwk.y, 'y');

  // RFC 7638 requires this exact member set and lexicographic order for an EC
  // JWK thumbprint. JSON.stringify preserves the insertion order below.
  const publicJwkThumbprint = crypto
    .createHash('sha256')
    .update(JSON.stringify({ crv: 'P-256', kty: 'EC', x, y }))
    .digest('base64url');
  const publicJwk = {
    alg: algorithm,
    crv: 'P-256',
    kid,
    kty: 'EC',
    use: 'sig',
    x,
    y,
  } satisfies Prisma.JsonObject;
  const binding = { kid, algorithm, publicJwkThumbprint };
  const privateKeyPem = privateKey.export({ type: 'pkcs8', format: 'pem' });
  const protectedPrivateKey = new AesGcmPrivateKeyProtector(
    encryptionKey
  ).protect(Buffer.from(privateKeyPem), binding);

  await prisma.delegatedSigningKey.create({
    data: {
      id,
      kid,
      algorithm,
      publicJwk,
      protectedPrivateKey: {
        ciphertext: protectedPrivateKey.ciphertext.toString('base64'),
        iv: protectedPrivateKey.iv.toString('base64'),
        authTag: protectedPrivateKey.authTag.toString('base64'),
      },
      publicJwkThumbprint,
      state: 'ACTIVE',
      activatedAt: new Date(),
    },
  });

  return { created: true, kid };
}
