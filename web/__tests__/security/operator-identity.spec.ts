import crypto from 'node:crypto';

import { verifyOperatorIdentityToken } from '@/lib/operator-identity';
import {
  signTestOperatorIdentity,
  testOperatorJwks,
} from '../helpers/operator-identity';

const now = Date.UTC(2026, 8, 10, 12, 0, 0);

describe('operator identity boundary', () => {
  it('verifies a short-lived ES256 support assertion with public JWKS only', () => {
    const assertion = signTestOperatorIdentity('support.alice', {}, now);
    expect(
      verifyOperatorIdentityToken(assertion, testOperatorJwks, now)
    ).toMatchObject({
      subject: 'support.alice',
      issuer: 'marketai-operator-identity',
      keyId: 'operator-test-key-v1',
      assertionId: expect.stringMatching(/^assertion-/),
    });
    expect(JSON.stringify(testOperatorJwks)).not.toContain('"d"');
  });

  it('rejects changing the subject while replaying the original signature', () => {
    const original = signTestOperatorIdentity('support.alice', {}, now);
    const [header, encodedClaims, signature] = original.split('.');
    const claims = JSON.parse(
      Buffer.from(encodedClaims, 'base64url').toString('utf8')
    );
    const forgedClaims = Buffer.from(
      JSON.stringify({ ...claims, sub: 'support.mallory' })
    ).toString('base64url');

    expect(() =>
      verifyOperatorIdentityToken(
        `${header}.${forgedClaims}.${signature}`,
        testOperatorJwks,
        now
      )
    ).toThrow('verified support operator identity');
  });

  it('rejects an unknown kid even when a trusted key made the signature', () => {
    const original = signTestOperatorIdentity('support.alice', {}, now);
    const [, claims, signature] = original.split('.');
    const wrongHeader = Buffer.from(
      JSON.stringify({
        alg: 'ES256',
        typ: 'operator+jwt',
        kid: 'operator-unknown',
      })
    ).toString('base64url');

    expect(() =>
      verifyOperatorIdentityToken(
        `${wrongHeader}.${claims}.${signature}`,
        testOperatorJwks,
        now
      )
    ).toThrow('verified support operator identity');
  });

  it.each(['jku', 'x5u', 'jwk'])(
    'rejects a signed header carrying remote key injection via %s',
    (name) => {
      const assertion = signTestOperatorIdentity('support.alice', {}, now, {
        [name]: 'https://attacker.invalid/keys.json',
      });
      expect(() =>
        verifyOperatorIdentityToken(assertion, testOperatorJwks, now)
      ).toThrow('verified support operator identity');
    }
  );

  it('rejects non-object JOSE headers and malformed static key entries', () => {
    const assertion = signTestOperatorIdentity('support.alice', {}, now);
    const [, claims, signature] = assertion.split('.');
    expect(() =>
      verifyOperatorIdentityToken(
        `${Buffer.from('null').toString('base64url')}.${claims}.${signature}`,
        testOperatorJwks,
        now
      )
    ).toThrow('verified support operator identity');
    expect(() =>
      verifyOperatorIdentityToken(assertion, { keys: [null] } as never, now)
    ).toThrow('OPERATOR_IDENTITY_JWKS');
  });

  it('accepts both public keys during a bounded rotation overlap', () => {
    const rotated = crypto.generateKeyPairSync('ec', {
      namedCurve: 'prime256v1',
    });
    const rotatedJwk = rotated.publicKey.export({ format: 'jwk' });
    const overlap = {
      keys: [
        ...testOperatorJwks.keys,
        {
          ...rotatedJwk,
          kid: 'operator-test-key-v2',
          alg: 'ES256',
          use: 'sig',
        },
      ],
    };
    const issuedAt = Math.floor(now / 1000);
    const header = Buffer.from(
      JSON.stringify({
        alg: 'ES256',
        typ: 'operator+jwt',
        kid: 'operator-test-key-v2',
      })
    ).toString('base64url');
    const claims = Buffer.from(
      JSON.stringify({
        sub: 'support.bob',
        iss: 'marketai-operator-identity',
        aud: 'marketai-control-plane-ops',
        iat: issuedAt,
        nbf: issuedAt,
        exp: issuedAt + 300,
        jti: 'assertion-rotation-bob',
        roles: ['support'],
      })
    ).toString('base64url');
    const input = `${header}.${claims}`;
    const signature = crypto.sign('sha256', Buffer.from(input), {
      key: rotated.privateKey,
      dsaEncoding: 'ieee-p1363',
    });

    expect(
      verifyOperatorIdentityToken(
        `${input}.${signature.toString('base64url')}`,
        overlap,
        now
      )
    ).toMatchObject({ subject: 'support.bob', keyId: 'operator-test-key-v2' });
    expect(
      verifyOperatorIdentityToken(
        signTestOperatorIdentity('support.alice', {}, now),
        overlap,
        now
      )
    ).toMatchObject({
      subject: 'support.alice',
      keyId: 'operator-test-key-v1',
    });
  });

  it.each([
    ['expired assertion', { exp: Math.floor(now / 1000) - 1 }],
    ['wrong audience', { aud: 'another-service' }],
    ['missing support role', { roles: ['viewer'] }],
    ['excessive replay window', { exp: Math.floor(now / 1000) + 301 }],
    ['missing assertion id', { jti: null }],
  ])('rejects %s', (_label, overrides) => {
    const assertion = signTestOperatorIdentity('support.alice', overrides, now);
    expect(() =>
      verifyOperatorIdentityToken(assertion, testOperatorJwks, now)
    ).toThrow('verified support operator identity');
  });

  it('maps a replay to the same subject and assertion instead of a second identity', () => {
    const assertion = signTestOperatorIdentity('support.alice', {}, now);
    const first = verifyOperatorIdentityToken(assertion, testOperatorJwks, now);
    const replay = verifyOperatorIdentityToken(
      assertion,
      testOperatorJwks,
      now + 1_000
    );

    expect(replay).toMatchObject({
      subject: first.subject,
      assertionId: first.assertionId,
    });
  });
});
