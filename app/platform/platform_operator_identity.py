"""ES256 verification of Platform-Operator JWTs for ``/api/platform/v1/*``.

Separate identity surface from ``DelegatedIdentityMiddleware`` (spec §3):
that middleware verifies 13-claim BFF-issued tokens scoped to
``role in {owner, member}`` and cannot express a platform-operator actor.
``PlatformOperatorJWTVerifier`` verifies a distinct, narrower token shape
(``typ=platform+jwt``, ``iss=marketai-platform``,
``aud=marketai-api:platform-operator``) signed by a separate key set, using
its own :class:`~app.platform.jwks_store.JwksKeyStore` instance so a
platform-operator key rotation or outage can never affect delegated-user
verification (and vice versa).

``require_platform_operator_jwt`` is the FastAPI dependency that routes
under ``/api/platform/v1/*`` use to obtain a verified
:class:`PlatformOperatorIdentity`. It reads the configured
:class:`PlatformOperatorJWTVerifier` off ``request.app.state`` (wired in
``app.main`` only when ``PLATFORM_OPERATOR_JWKS_URL`` is configured) rather
than importing a module-level singleton, so tests can substitute a verifier
via ``app.state`` without monkeypatching this module.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import jwt as pyjwt
from fastapi import HTTPException, Request

from .jwks_store import JwksKeyStore, JwksNotYetLoadedError, JwksStaleError, UnknownKidError


class PlatformTokenError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


EXPECTED_ISSUER = "marketai-platform"
EXPECTED_AUDIENCE = "marketai-api:platform-operator"
EXPECTED_TYP = "platform+jwt"
MAX_TOKEN_BYTES = 8192
MAX_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 30

#: Error codes that mean "the JWKS side of verification is not usable right
#: now" (not yet loaded / hard-stale) rather than "the token is invalid" --
#: these map to 503, everything else in `PlatformTokenError` maps to 401.
_NOT_CONFIGURED_CODES = frozenset(
    {"platform_operator_not_configured"}
)


@dataclass(frozen=True)
class PlatformOperatorIdentity:
    sub: str
    workspace_id: str
    session_id: str
    iat: int
    exp: int
    jti: str


class PlatformOperatorJWTVerifier:
    def __init__(self, key_store: JwksKeyStore) -> None:
        self._key_store = key_store

    async def verify(self, token: str) -> PlatformOperatorIdentity:
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise PlatformTokenError(
                "platform_operator_token_oversize",
                f"Token is oversize: exceeds {MAX_TOKEN_BYTES} byte limit",
            )

        try:
            unverified_header = pyjwt.get_unverified_header(token)
        except pyjwt.exceptions.DecodeError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid", "Malformed token header",
            ) from exc

        typ = unverified_header.get("typ", "")
        if typ != EXPECTED_TYP:
            raise PlatformTokenError(
                "platform_operator_token_invalid",
                f"Expected typ={EXPECTED_TYP!r}, got {typ!r}",
            )

        alg = unverified_header.get("alg", "")
        if alg != "ES256":
            raise PlatformTokenError(
                "platform_operator_token_invalid",
                f"Expected alg=ES256, got {alg!r}",
            )

        kid = unverified_header.get("kid")
        if not kid:
            raise PlatformTokenError(
                "platform_operator_token_invalid", "Missing kid header",
            )

        try:
            signing_key = await self._key_store.get_key(kid)
        except JwksNotYetLoadedError as exc:
            raise PlatformTokenError(
                "platform_operator_not_configured", "JWKS not yet loaded",
            ) from exc
        except JwksStaleError as exc:
            raise PlatformTokenError(
                "platform_operator_not_configured",
                f"JWKS cache stale: {exc}",
            ) from exc
        except UnknownKidError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid",
                f"Unknown kid after refresh: {kid}",
            ) from exc

        try:
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256"],
                issuer=EXPECTED_ISSUER,
                audience=EXPECTED_AUDIENCE,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "sub", "iat", "exp", "nbf", "jti"]},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid", "Token expired",
            ) from exc
        except pyjwt.InvalidIssuerError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid", f"Invalid issuer: {exc}",
            ) from exc
        except pyjwt.InvalidAudienceError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid", f"Invalid audience: {exc}",
            ) from exc
        except pyjwt.InvalidSignatureError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid", "Invalid signature",
            ) from exc
        except pyjwt.PyJWTError as exc:
            raise PlatformTokenError(
                "platform_operator_token_invalid", str(exc),
            ) from exc

        iat = payload.get("iat", 0)
        exp = payload.get("exp", 0)
        nbf = payload.get("nbf", 0)

        if exp - iat > MAX_TTL_SECONDS:
            raise PlatformTokenError(
                "platform_operator_ttl_exceeded",
                f"Token ttl {exp - iat}s exceeds {MAX_TTL_SECONDS}s limit",
            )

        now = int(time.time())
        if iat > now + CLOCK_SKEW_SECONDS:
            raise PlatformTokenError(
                "platform_operator_token_invalid",
                "iat is in the future beyond clock skew tolerance",
            )

        if nbf > iat:
            raise PlatformTokenError(
                "platform_operator_token_invalid",
                "nbf must not be after iat",
            )

        workspace_id = payload.get("workspace_id", "")
        session_id = payload.get("session_id", "")
        sub = payload.get("sub", "")
        jti = payload.get("jti", "")

        for name, value, max_len in [
            ("sub", sub, 200), ("workspace_id", workspace_id, 255),
            ("session_id", session_id, 200), ("jti", jti, 200),
        ]:
            if not value or len(value) > max_len:
                raise PlatformTokenError(
                    "platform_operator_token_invalid",
                    f"Claim {name!r} missing or exceeds {max_len} chars",
                )

        return PlatformOperatorIdentity(
            sub=sub, workspace_id=workspace_id,
            session_id=session_id, iat=iat, exp=exp, jti=jti,
        )


async def require_platform_operator_jwt(request: Request) -> PlatformOperatorIdentity:
    """FastAPI dependency: verify the platform-operator bearer token.

    Reads the process-wide :class:`PlatformOperatorJWTVerifier` from
    ``request.app.state.platform_operator_jwt_verifier`` (wired in
    ``app.main`` only when ``PLATFORM_OPERATOR_JWKS_URL`` is configured).
    Fails closed with 503 when no verifier is wired, 401 when the
    ``Authorization`` header is missing/malformed or the token itself is
    invalid, and 503 when the token is well-formed but the JWKS cache backing
    verification is not yet loaded or is hard-stale (a control-plane/JWKS
    availability problem, not a client error).

    This is deliberately a plain ``Depends`` function rather than ASGI
    middleware like ``DelegatedIdentityMiddleware``: platform-operator routes
    are a small, explicitly-listed surface under ``/api/platform/v1/*``, so a
    per-route dependency is simpler to reason about and test than a second
    path-prefix-matching middleware.
    """
    verifier = getattr(request.app.state, "platform_operator_jwt_verifier", None)
    if verifier is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "platform_operator_not_configured",
                "message": "Platform-operator identity is not configured.",
            },
        )

    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "platform_operator_token_required",
                "message": "A platform-operator bearer token is required.",
            },
        )
    token = authorization[len("Bearer "):]

    try:
        identity = await verifier.verify(token)
    except PlatformTokenError as exc:
        status_code = 503 if exc.code in _NOT_CONFIGURED_CODES else 401
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.detail},
        ) from exc

    request.state.platform_operator_identity = identity
    return identity
