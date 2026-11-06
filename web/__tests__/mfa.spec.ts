import {
  decodeBase32,
  encodeBase32,
  protectMfaSecret,
  revealMfaSecret,
  totpCode,
  verifyTotp,
} from '@/lib/mfa';

jest.mock('@/lib/prisma', () => ({ prisma: {} }));

describe('MFA primitives', () => {
  const originalKey = process.env.MFA_ENCRYPTION_KEY;

  beforeAll(() => {
    process.env.MFA_ENCRYPTION_KEY = Buffer.alloc(32, 7).toString('base64');
  });

  afterAll(() => {
    if (originalKey === undefined) delete process.env.MFA_ENCRYPTION_KEY;
    else process.env.MFA_ENCRYPTION_KEY = originalKey;
  });

  it('round-trips arbitrary bytes through RFC 4648 base32', () => {
    const bytes = Buffer.from('MarketAI MFA\u0000\u00ff', 'latin1');
    expect(decodeBase32(encodeBase32(bytes))).toEqual(bytes);
  });

  it('accepts only a current-window TOTP code', () => {
    const secret = encodeBase32(Buffer.alloc(20, 9));
    const at = Date.parse('2026-09-09T09:00:00Z');
    expect(verifyTotp(secret, totpCode(secret, at), at)).toBe(true);
    expect(verifyTotp(secret, '000000', at)).toBe(false);
  });

  it('persists an authenticated encryption envelope instead of plaintext', () => {
    const secret = encodeBase32(Buffer.alloc(20, 3));
    const protectedValue = protectMfaSecret(secret);
    expect(JSON.stringify(protectedValue)).not.toContain(secret);
    expect(revealMfaSecret(protectedValue)).toBe(secret);
  });
});
