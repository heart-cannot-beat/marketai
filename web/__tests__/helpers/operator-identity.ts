import crypto from 'node:crypto';

import { verifyOperatorIdentityToken } from '@/lib/operator-identity';

const keyId = 'operator-test-key-v1';
const { privateKey, publicKey } = crypto.generateKeyPairSync('ec', {
  namedCurve: 'prime256v1',
});
const publicJwk = publicKey.export({ format: 'jwk' });

export const testOperatorJwks = {
  keys: [{ ...publicJwk, kid: keyId, alg: 'ES256', use: 'sig' }],
};

export const signTestOperatorIdentity = (
  subject: string,
  overrides: Record<string, unknown> = {},
  now = Date.now(),
  headerOverrides: Record<string, unknown> = {}
) => {
  const issuedAt = Math.floor(now / 1000);
  const encodedHeader = Buffer.from(
    JSON.stringify({
      alg: 'ES256',
      typ: 'operator+jwt',
      kid: keyId,
      ...headerOverrides,
    })
  ).toString('base64url');
  const encodedClaims = Buffer.from(
    JSON.stringify({
      sub: subject,
      iss: 'marketai-operator-identity',
      aud: 'marketai-control-plane-ops',
      iat: issuedAt,
      nbf: issuedAt - 1,
      exp: issuedAt + 300,
      jti: `assertion-${crypto.randomUUID()}`,
      roles: ['support'],
      ...overrides,
    })
  ).toString('base64url');
  const signingInput = `${encodedHeader}.${encodedClaims}`;
  const signature = crypto.sign('sha256', Buffer.from(signingInput), {
    key: privateKey,
    dsaEncoding: 'ieee-p1363',
  });
  return `${signingInput}.${signature.toString('base64url')}`;
};

export const verifiedTestOperator = (subject: string) =>
  verifyOperatorIdentityToken(
    signTestOperatorIdentity(subject),
    testOperatorJwks
  );
