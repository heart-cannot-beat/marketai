import crypto from 'node:crypto';
import {
  AesGcmPrivateKeyProtector,
  KeyProtectionError,
} from '../../lib/delegated-identity/key-protector';
import type { KeyBinding } from '../../lib/delegated-identity/key-protector';

const TEST_KEY = crypto.randomBytes(32);
const BINDING: KeyBinding = {
  kid: 'dsk_test_001',
  algorithm: 'ES256',
  publicJwkThumbprint: 'thumb_abc',
};

describe('AesGcmPrivateKeyProtector', () => {
  const protector = new AesGcmPrivateKeyProtector(TEST_KEY);

  it('roundtrips: protect then unprotect returns original PEM', () => {
    const pem = Buffer.from(
      '-----BEGIN EC PRIVATE KEY-----\nfake\n-----END EC PRIVATE KEY-----'
    );
    const protectedKey = protector.protect(pem, BINDING);
    const result = protector.unprotect(protectedKey, BINDING);
    expect(result).toEqual(pem);
  });

  it('uses 12-byte IV', () => {
    const pem = Buffer.from('test-key-material');
    const protectedKey = protector.protect(pem, BINDING);
    expect(protectedKey.iv.length).toBe(12);
  });

  it('uses 16-byte auth tag', () => {
    const pem = Buffer.from('test-key-material');
    const protectedKey = protector.protect(pem, BINDING);
    expect(protectedKey.authTag.length).toBe(16);
  });

  it('fails unprotect with wrong binding (AAD mismatch)', () => {
    const pem = Buffer.from('test-key-material');
    const protectedKey = protector.protect(pem, BINDING);
    const wrongBinding: KeyBinding = {
      ...BINDING,
      kid: 'dsk_different_key',
    };
    expect(() => protector.unprotect(protectedKey, wrongBinding)).toThrow(
      KeyProtectionError
    );
  });

  it('fails unprotect with tampered ciphertext', () => {
    const pem = Buffer.from('test-key-material');
    const protectedKey = protector.protect(pem, BINDING);
    const tampered = {
      ...protectedKey,
      ciphertext: Buffer.concat([protectedKey.ciphertext, Buffer.from([0xff])]),
    };
    expect(() => protector.unprotect(tampered, BINDING)).toThrow(
      KeyProtectionError
    );
  });

  it('fails unprotect with wrong encryption key', () => {
    const pem = Buffer.from('test-key-material');
    const protectedKey = protector.protect(pem, BINDING);
    const otherProtector = new AesGcmPrivateKeyProtector(
      crypto.randomBytes(32)
    );
    expect(() => otherProtector.unprotect(protectedKey, BINDING)).toThrow(
      KeyProtectionError
    );
  });

  it('rejects an encryption key that is not exactly 32 bytes', () => {
    expect(
      () => new AesGcmPrivateKeyProtector(crypto.randomBytes(16))
    ).toThrow();
    expect(
      () => new AesGcmPrivateKeyProtector(crypto.randomBytes(33))
    ).toThrow();
  });

  it('produces different ciphertext and IV on repeated calls (nonce is not reused)', () => {
    const pem = Buffer.from('test-key-material');
    const first = protector.protect(pem, BINDING);
    const second = protector.protect(pem, BINDING);
    expect(first.iv.equals(second.iv)).toBe(false);
    expect(first.ciphertext.equals(second.ciphertext)).toBe(false);
  });
});
