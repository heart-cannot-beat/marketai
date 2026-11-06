import crypto from 'node:crypto';
import { createVerify } from 'node:crypto';
import { PrismaSignerAdapter } from '../../lib/delegated-identity/signer';
import { AesGcmPrivateKeyProtector } from '../../lib/delegated-identity/key-protector';

function makeSignedKeyRow(
  overrides: {
    kid?: string;
    state?: string;
  } = {}
) {
  const encKey = crypto.randomBytes(32);
  const protector = new AesGcmPrivateKeyProtector(encKey);
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ec', {
    namedCurve: 'P-256',
  });
  const privatePem = privateKey.export({
    type: 'pkcs8',
    format: 'pem',
  }) as string;
  const publicPem = publicKey.export({ type: 'spki', format: 'pem' }) as string;
  const kid = overrides.kid ?? 'dsk_test';
  const binding = { kid, algorithm: 'ES256', publicJwkThumbprint: 'thumb' };
  const protectedKey = protector.protect(Buffer.from(privatePem), binding);

  return {
    protector,
    publicPem,
    row: {
      id: 'dsk_001',
      kid,
      algorithm: 'ES256',
      publicJwkThumbprint: 'thumb',
      protectedPrivateKey: {
        ciphertext: protectedKey.ciphertext.toString('base64'),
        iv: protectedKey.iv.toString('base64'),
        authTag: protectedKey.authTag.toString('base64'),
      },
      state: overrides.state ?? 'ACTIVE',
    },
  };
}

describe('PrismaSignerAdapter', () => {
  it('signs a JWT with ES256 and embeds kid in header', async () => {
    const { protector, row } = makeSignedKeyRow();

    const mockPrisma = {
      delegatedSigningKey: {
        findFirst: jest.fn().mockResolvedValue(row),
      },
    };

    const signer = new PrismaSignerAdapter(mockPrisma as any, protector);
    const token = await signer.sign({ sub: 'user_1', workspace_id: 'ws_1' });

    const parts = token.split('.');
    expect(parts).toHaveLength(3);
    const header = JSON.parse(Buffer.from(parts[0], 'base64url').toString());
    expect(header.alg).toBe('ES256');
    expect(header.typ).toBe('delegated+jwt');
    expect(header.kid).toBe('dsk_test');
  });

  it('selects the ACTIVE key via findFirst with state filter', async () => {
    const { protector, row } = makeSignedKeyRow();
    const findFirst = jest.fn().mockResolvedValue(row);
    const mockPrisma = { delegatedSigningKey: { findFirst } };

    const signer = new PrismaSignerAdapter(mockPrisma as any, protector);
    await signer.sign({ sub: 'user_1' });

    expect(findFirst).toHaveBeenCalledWith(
      expect.objectContaining({ where: { state: 'ACTIVE' } })
    );
  });

  it('embeds the claims verbatim in the payload segment', async () => {
    const { protector, row } = makeSignedKeyRow();
    const mockPrisma = {
      delegatedSigningKey: { findFirst: jest.fn().mockResolvedValue(row) },
    };

    const signer = new PrismaSignerAdapter(mockPrisma as any, protector);
    const claims = { sub: 'user_1', workspace_id: 'ws_1', epoch: 3 };
    const token = await signer.sign(claims);

    const [, payloadSegment] = token.split('.');
    const payload = JSON.parse(
      Buffer.from(payloadSegment, 'base64url').toString()
    );
    expect(payload).toEqual(claims);
  });

  it('produces a signature verifiable against the key pair public key', async () => {
    const { protector, publicPem, row } = makeSignedKeyRow();
    const mockPrisma = {
      delegatedSigningKey: { findFirst: jest.fn().mockResolvedValue(row) },
    };

    const signer = new PrismaSignerAdapter(mockPrisma as any, protector);
    const token = await signer.sign({ sub: 'user_1' });
    const [headerSegment, payloadSegment, signatureSegment] = token.split('.');
    const signingInput = `${headerSegment}.${payloadSegment}`;
    const signature = Buffer.from(signatureSegment, 'base64url');

    const verify = createVerify('SHA256');
    verify.update(signingInput);
    const isValid = verify.verify(
      { key: publicPem, dsaEncoding: 'ieee-p1363' },
      signature
    );
    expect(isValid).toBe(true);
  });

  it('throws when no ACTIVE signing key exists', async () => {
    const encKey = crypto.randomBytes(32);
    const protector = new AesGcmPrivateKeyProtector(encKey);
    const mockPrisma = {
      delegatedSigningKey: { findFirst: jest.fn().mockResolvedValue(null) },
    };

    const signer = new PrismaSignerAdapter(mockPrisma as any, protector);
    await expect(signer.sign({ sub: 'user_1' })).rejects.toThrow(
      'No ACTIVE signing key available'
    );
  });
});
