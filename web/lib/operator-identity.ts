import crypto from 'node:crypto';

import { ApiError } from './errors';

const ISSUER = 'marketai-operator-identity';
const AUDIENCE = 'marketai-control-plane-ops';
const TOKEN_TYPE = 'operator+jwt';
const MAX_TOKEN_TTL_SECONDS = 5 * 60;
const CLOCK_SKEW_SECONDS = 30;
const SUBJECT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,127}$/;
const ASSERTION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{7,191}$/;
const operatorIdentityBrand: unique symbol = Symbol('VerifiedOperatorIdentity');

export type VerifiedOperatorIdentity = {
  readonly subject: string;
  readonly issuer: typeof ISSUER;
  readonly assertionId: string;
  readonly keyId: string;
  readonly [operatorIdentityBrand]: true;
};

type OperatorHeader = {
  alg?: unknown;
  typ?: unknown;
  kid?: unknown;
};

type OperatorClaims = {
  sub?: unknown;
  iss?: unknown;
  aud?: unknown;
  exp?: unknown;
  iat?: unknown;
  nbf?: unknown;
  jti?: unknown;
  roles?: unknown;
};

type PublicJwk = JsonWebKey & {
  kid?: string;
  alg?: string;
  use?: string;
};

type PublicJwks = { keys: PublicJwk[] };

const unauthorized = () =>
  new ApiError(403, 'A verified support operator identity is required.');

const decodeJson = <T>(encoded: string): T =>
  JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8')) as T;

const isJsonObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const parseJwks = (value: string | PublicJwks): PublicJwks => {
  try {
    const parsed: unknown =
      typeof value === 'string' ? JSON.parse(value) : value;
    if (
      !isJsonObject(parsed) ||
      !Array.isArray(parsed.keys) ||
      parsed.keys.some((key) => !isJsonObject(key))
    ) {
      throw new Error('bad JWKS');
    }
    return parsed as PublicJwks;
  } catch {
    throw new ApiError(500, 'OPERATOR_IDENTITY_JWKS must be a public JWKS.');
  }
};

/**
 * Verify an ES256 assertion issued by the upstream operator access boundary.
 *
 * Only public JWK material reaches this process. Assertions carry a stable
 * ``jti`` and are valid for at most five minutes. Replaying one therefore
 * resolves to the same subject/assertion id; downstream case uniqueness by
 * verified subject cannot turn a replay into a second approval.
 */
export function verifyOperatorIdentityToken(
  token: string,
  publicJwks: string | PublicJwks,
  now = Date.now()
): VerifiedOperatorIdentity {
  const [encodedHeader, encodedClaims, encodedSignature, extra] =
    token.split('.');
  if (!encodedHeader || !encodedClaims || !encodedSignature || extra) {
    throw unauthorized();
  }

  let decodedHeader: unknown;
  let decodedClaims: unknown;
  let signature: Buffer;
  try {
    decodedHeader = decodeJson<unknown>(encodedHeader);
    decodedClaims = decodeJson<unknown>(encodedClaims);
    signature = Buffer.from(encodedSignature, 'base64url');
  } catch {
    throw unauthorized();
  }
  if (!isJsonObject(decodedHeader) || !isJsonObject(decodedClaims)) {
    throw unauthorized();
  }
  const header = decodedHeader as OperatorHeader;
  const claims = decodedClaims as OperatorClaims;

  if (
    Object.keys(header).some((name) => !['alg', 'typ', 'kid'].includes(name)) ||
    header.alg !== 'ES256' ||
    header.typ !== TOKEN_TYPE ||
    typeof header.kid !== 'string' ||
    !header.kid
  ) {
    throw unauthorized();
  }

  const matchingKeys = parseJwks(publicJwks).keys.filter(
    (key) => key.kid === header.kid
  );
  if (matchingKeys.length !== 1) throw unauthorized();
  const jwk = matchingKeys[0];
  if (
    jwk.kty !== 'EC' ||
    jwk.crv !== 'P-256' ||
    jwk.d ||
    (jwk.use !== undefined && jwk.use !== 'sig') ||
    (jwk.alg !== undefined && jwk.alg !== 'ES256') ||
    (jwk.key_ops !== undefined &&
      (!Array.isArray(jwk.key_ops) || !jwk.key_ops.includes('verify')))
  ) {
    throw unauthorized();
  }

  let validSignature = false;
  try {
    const key = crypto.createPublicKey({ key: jwk, format: 'jwk' });
    validSignature = crypto.verify(
      'sha256',
      Buffer.from(`${encodedHeader}.${encodedClaims}`, 'utf8'),
      { key, dsaEncoding: 'ieee-p1363' },
      signature
    );
  } catch {
    throw unauthorized();
  }

  const nowSeconds = Math.floor(now / 1000);
  if (
    !validSignature ||
    claims.iss !== ISSUER ||
    claims.aud !== AUDIENCE ||
    typeof claims.sub !== 'string' ||
    !SUBJECT_PATTERN.test(claims.sub) ||
    typeof claims.jti !== 'string' ||
    !ASSERTION_ID_PATTERN.test(claims.jti) ||
    typeof claims.iat !== 'number' ||
    !Number.isSafeInteger(claims.iat) ||
    typeof claims.nbf !== 'number' ||
    !Number.isSafeInteger(claims.nbf) ||
    typeof claims.exp !== 'number' ||
    !Number.isSafeInteger(claims.exp) ||
    claims.iat > nowSeconds + CLOCK_SKEW_SECONDS ||
    claims.nbf > nowSeconds + CLOCK_SKEW_SECONDS ||
    claims.exp <= nowSeconds ||
    claims.exp <= claims.iat ||
    claims.exp - claims.iat > MAX_TOKEN_TTL_SECONDS ||
    !Array.isArray(claims.roles) ||
    !claims.roles.includes('support')
  ) {
    throw unauthorized();
  }
  return {
    subject: claims.sub,
    issuer: ISSUER,
    assertionId: claims.jti,
    keyId: header.kid,
    [operatorIdentityBrand]: true,
  };
}

/** Read identity only at the process boundary; domain functions take the verified type. */
export function requireOperatorIdentityFromEnv(): VerifiedOperatorIdentity {
  const token = process.env.OPERATOR_IDENTITY_TOKEN;
  const publicJwks = process.env.OPERATOR_IDENTITY_JWKS;
  if (!token) throw unauthorized();
  if (!publicJwks) {
    throw new ApiError(
      500,
      'OPERATOR_IDENTITY_JWKS is required and must contain public keys only.'
    );
  }
  return verifyOperatorIdentityToken(token, publicJwks);
}
