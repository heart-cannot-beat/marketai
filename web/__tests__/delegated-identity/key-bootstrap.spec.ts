import crypto from 'node:crypto';

import { ensureActiveDelegatedSigningKey } from '../../lib/delegated-identity/key-bootstrap';
import { AesGcmPrivateKeyProtector } from '../../lib/delegated-identity/key-protector';

describe('ensureActiveDelegatedSigningKey', () => {
  it('leaves an existing ACTIVE key untouched', async () => {
    const findFirst = jest.fn().mockResolvedValue({ kid: 'dsk_existing' });
    const create = jest.fn();

    const result = await ensureActiveDelegatedSigningKey(
      { delegatedSigningKey: { findFirst, create } } as any,
      crypto.randomBytes(32)
    );

    expect(result).toEqual({ created: false, kid: 'dsk_existing' });
    expect(create).not.toHaveBeenCalled();
  });

  it('creates a valid ACTIVE ES256 key with encrypted private material', async () => {
    const encryptionKey = crypto.randomBytes(32);
    const create = jest.fn().mockResolvedValue(undefined);

    const result = await ensureActiveDelegatedSigningKey(
      {
        delegatedSigningKey: {
          findFirst: jest.fn().mockResolvedValue(null),
          create,
        },
      } as any,
      encryptionKey
    );

    expect(result.created).toBe(true);
    expect(result.kid).toMatch(/^dsk_[0-9A-HJKMNP-TV-Z]{26}$/);
    const data = create.mock.calls[0][0].data;
    expect(data).toMatchObject({
      id: result.kid,
      kid: result.kid,
      algorithm: 'ES256',
      state: 'ACTIVE',
      publicJwk: {
        alg: 'ES256',
        crv: 'P-256',
        kid: result.kid,
        kty: 'EC',
        use: 'sig',
      },
    });
    expect(data.publicJwk).not.toHaveProperty('d');

    const protectedKey = {
      ciphertext: Buffer.from(data.protectedPrivateKey.ciphertext, 'base64'),
      iv: Buffer.from(data.protectedPrivateKey.iv, 'base64'),
      authTag: Buffer.from(data.protectedPrivateKey.authTag, 'base64'),
    };
    const privatePem = new AesGcmPrivateKeyProtector(encryptionKey).unprotect(
      protectedKey,
      {
        kid: data.kid,
        algorithm: data.algorithm,
        publicJwkThumbprint: data.publicJwkThumbprint,
      }
    );
    const publicFromPrivate = crypto
      .createPublicKey(privatePem)
      .export({ format: 'jwk' });

    expect(publicFromPrivate.x).toBe(data.publicJwk.x);
    expect(publicFromPrivate.y).toBe(data.publicJwk.y);
  });

  it('rejects an encryption key that is not 32 bytes', async () => {
    await expect(
      ensureActiveDelegatedSigningKey(
        {
          delegatedSigningKey: {
            findFirst: jest.fn().mockResolvedValue(null),
            create: jest.fn(),
          },
        } as any,
        Buffer.alloc(31)
      )
    ).rejects.toThrow('Encryption key must be exactly 32 bytes');
  });
});
