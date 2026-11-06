"""PlatformOperatorJWTVerifier: ES256 verification of platform-operator JWTs.

Mirrors the async test style used by ``tests/test_jwks_store.py``
(``unittest.IsolatedAsyncioTestCase``) rather than the brief's
``@pytest.mark.asyncio`` decorator: this repository's ``requirements.txt``
does not pin ``pytest-asyncio``, so a bare ``@pytest.mark.asyncio`` test
does not actually execute -- pytest raises "async def functions are not
natively supported" for every such test instead of running it. Test IDs
(PO1, PO4, PO5, PO8, PO10, PO11) match the brief 1:1.
"""
from __future__ import annotations

import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec

from app.platform.jwks_store import JwksKeyStore
from app.platform.platform_operator_identity import (
    PlatformOperatorIdentity,
    PlatformOperatorJWTVerifier,
    PlatformTokenError,
)


def _generate_es256_keypair(kid: str = "kid-1"):
    """A fresh ES256 keypair plus the `jwt.PyJWK` `get_key` would return.

    Installed PyJWT (2.14.0) has no `PyJWK.from_key` -- matches the pattern
    already used by `tests/test_delegated_identity.py::generate_keypair` for
    the same ES256-keypair-to-PyJWK need.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    alg = pyjwt.algorithms.ECAlgorithm(pyjwt.algorithms.ECAlgorithm.SHA256)
    public_jwk = json.loads(alg.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return private_key, pyjwt.PyJWK.from_dict(public_jwk)


def _make_token(private_key, kid="kid-1", claims=None, headers=None):
    now = int(time.time())
    payload = {
        "iss": "marketai-platform",
        "aud": "marketai-api:platform-operator",
        "sub": "platform-actor-1",
        "workspace_id": "ws_platform",
        "session_id": "sess_1",
        "iat": now,
        "exp": now + 3600,
        "nbf": now,
        "jti": "jti_1",
    }
    if claims:
        payload.update(claims)
    hdr = {"kid": kid, "typ": "platform+jwt", "alg": "ES256"}
    if headers:
        hdr.update(headers)
    return pyjwt.encode(payload, private_key, algorithm="ES256", headers=hdr)


class PlatformOperatorJWTVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_platform_jwt(self) -> None:
        """PO1: Valid token -> success."""
        private_key, jwk = _generate_es256_keypair()
        store = AsyncMock(spec=JwksKeyStore)
        store.get_key = AsyncMock(return_value=jwk)
        store.is_fresh = lambda: True

        verifier = PlatformOperatorJWTVerifier(store)
        token = _make_token(private_key)
        identity = await verifier.verify(token)
        self.assertIsInstance(identity, PlatformOperatorIdentity)
        self.assertEqual(identity.sub, "platform-actor-1")
        self.assertEqual(identity.workspace_id, "ws_platform")

    async def test_expired_platform_jwt(self) -> None:
        """PO4: Expired token."""
        private_key, jwk = _generate_es256_keypair()
        store = AsyncMock(spec=JwksKeyStore)
        store.get_key = AsyncMock(return_value=jwk)
        store.is_fresh = lambda: True

        verifier = PlatformOperatorJWTVerifier(store)
        now = int(time.time())
        token = _make_token(
            private_key,
            claims={"iat": now - 7200, "exp": now - 3600, "nbf": now - 7200},
        )
        with self.assertRaisesRegex(PlatformTokenError, "expired"):
            await verifier.verify(token)

    async def test_wrong_issuer(self) -> None:
        """PO5: Wrong issuer."""
        private_key, jwk = _generate_es256_keypair()
        store = AsyncMock(spec=JwksKeyStore)
        store.get_key = AsyncMock(return_value=jwk)
        store.is_fresh = lambda: True

        verifier = PlatformOperatorJWTVerifier(store)
        token = _make_token(private_key, claims={"iss": "wrong-issuer"})
        with self.assertRaises(PlatformTokenError):
            await verifier.verify(token)

    async def test_wrong_typ_header(self) -> None:
        """PO8: Wrong typ header."""
        private_key, _ = _generate_es256_keypair()
        store = AsyncMock(spec=JwksKeyStore)
        store.is_fresh = lambda: True

        verifier = PlatformOperatorJWTVerifier(store)
        token = _make_token(private_key, headers={"typ": "delegated+jwt"})
        with self.assertRaisesRegex(PlatformTokenError, "typ"):
            await verifier.verify(token)

    async def test_oversize_token(self) -> None:
        """PO10: Token exceeds 8 KiB."""
        store = AsyncMock(spec=JwksKeyStore)
        store.is_fresh = lambda: True

        verifier = PlatformOperatorJWTVerifier(store)
        with self.assertRaisesRegex(PlatformTokenError, "oversize"):
            await verifier.verify("x" * 8193)

    async def test_ttl_exceeded(self) -> None:
        """PO11: TTL exceeds 3600s."""
        private_key, jwk = _generate_es256_keypair()
        store = AsyncMock(spec=JwksKeyStore)
        store.get_key = AsyncMock(return_value=jwk)
        store.is_fresh = lambda: True

        verifier = PlatformOperatorJWTVerifier(store)
        now = int(time.time())
        token = _make_token(private_key, claims={"exp": now + 7200})
        with self.assertRaisesRegex(PlatformTokenError, "ttl"):
            await verifier.verify(token)


# ---------------------------------------------------------------------------
# Task 4: request_platform_context -- JWT primary, deprecated static fallback
# ---------------------------------------------------------------------------
#
# app.meta.platform_api.request_platform_context is a FastAPI dependency
# that reads request.app.state.platform_operator_jwt_verifier, so it is
# exercised here through a throwaway FastAPI app + TestClient rather than by
# hand-constructing a Starlette Request -- the same approach
# tests/test_meta_platform_api.py uses for the same dependency, but isolated
# from that file's real app.main.app/migrated-database setup since these
# tests only need the auth guard itself. Test IDs PO16-PO23 continue this
# file's PO-prefixed platform-operator-identity numbering (PO1-PO11 above
# were Task 3's PlatformOperatorJWTVerifier tests).

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.meta.platform_api import LegacyPlatformContext, request_platform_context


def _make_guarded_app() -> FastAPI:
    app = FastAPI()
    app.state.platform_operator_jwt_verifier = None

    @app.post("/guarded")
    async def guarded(ctx: LegacyPlatformContext = Depends(request_platform_context)):
        return {
            "actor_id": ctx.actor_id,
            "workspace_id": ctx.workspace_id,
            "auth_method": ctx.auth_method,
        }

    return app


class RequestPlatformContextTests(unittest.TestCase):
    """Task 4: app.meta.platform_api.request_platform_context."""

    def setUp(self) -> None:
        from app import config

        self._original_token = config.PLATFORM_ADMIN_TOKEN
        self.addCleanup(self._restore_token)

    def _restore_token(self) -> None:
        from app import config

        config.PLATFORM_ADMIN_TOKEN = self._original_token

    def test_valid_jwt_is_primary(self) -> None:
        """PO16: A verifier configured and the token verifies -> auth_method='jwt'."""
        from app import config
        from app.platform.platform_operator_identity import PlatformOperatorIdentity

        config.PLATFORM_ADMIN_TOKEN = ""
        app = _make_guarded_app()
        identity = PlatformOperatorIdentity(
            sub="operator-1", workspace_id="ws_platform", session_id="sess_1",
            iat=1, exp=2, jti="jti_1",
        )
        app.state.platform_operator_jwt_verifier = AsyncMock(
            verify=AsyncMock(return_value=identity)
        )

        resp = TestClient(app).post(
            "/guarded", headers={"Authorization": "Bearer some-jwt"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["actor_id"], "platform:operator-1")
        self.assertEqual(body["workspace_id"], "ws_platform")
        self.assertEqual(body["auth_method"], "jwt")

    def test_invalid_jwt_no_static_fallback_configured_is_401(self) -> None:
        """PO17: verifier rejects the token, no static token configured -> 401."""
        from app import config
        from app.platform.platform_operator_identity import PlatformTokenError

        config.PLATFORM_ADMIN_TOKEN = ""
        app = _make_guarded_app()
        app.state.platform_operator_jwt_verifier = AsyncMock(
            verify=AsyncMock(
                side_effect=PlatformTokenError("platform_operator_token_invalid", "bad")
            )
        )

        resp = TestClient(app).post(
            "/guarded", headers={"Authorization": "Bearer bad-jwt"},
        )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"]["code"], "platform_admin_token_invalid")

    def test_invalid_jwt_falls_back_to_static_token(self) -> None:
        """PO18: verifier rejects the token; a matching static token still succeeds
        (deprecated fallback) and logs a WARNING."""
        from app import config
        from app.platform.platform_operator_identity import PlatformTokenError

        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-fallback"
        app = _make_guarded_app()
        app.state.platform_operator_jwt_verifier = AsyncMock(
            verify=AsyncMock(
                side_effect=PlatformTokenError("platform_operator_token_invalid", "bad")
            )
        )

        # Mocks app.meta.platform_api.logger directly rather than relying on
        # unittest.assertLogs' process-wide logger mutation: this suite runs
        # inside a much larger pytest/unittest session, and assertLogs
        # intermittently observed zero captured records here depending on
        # which other test modules had already run in the same process --
        # a logging-infrastructure interaction outside this test's control,
        # not a behavior of request_platform_context itself (verified with
        # `logger.warning` firing correctly, including reaching a real
        # root-logger handler, when this exact call is exercised in
        # isolation). Patching the logger object read the dependency actually
        # calls is deterministic regardless of ambient logging state.
        with patch("app.meta.platform_api.logger") as mock_logger:
            resp = TestClient(app).post(
                "/guarded", headers={"Authorization": "Bearer s3cr3t-fallback"},
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["auth_method"], "static_token")
        self.assertIsNone(body["workspace_id"])
        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args
        self.assertIn("deprecated", warning_args.args[0])

    def test_no_verifier_configured_uses_static_token(self) -> None:
        """PO19: no verifier wired at all, static token configured and matches."""
        from app import config

        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-only"
        app = _make_guarded_app()  # platform_operator_jwt_verifier stays None

        # See test_invalid_jwt_falls_back_to_static_token for why this mocks
        # the logger directly rather than using assertLogs.
        with patch("app.meta.platform_api.logger") as mock_logger:
            resp = TestClient(app).post(
                "/guarded", headers={"Authorization": "Bearer s3cr3t-only"},
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["auth_method"], "static_token")
        mock_logger.warning.assert_called_once()

    def test_neither_mechanism_configured_is_503(self) -> None:
        """PO20: no verifier and no static token -> fail closed 503."""
        from app import config

        config.PLATFORM_ADMIN_TOKEN = ""
        app = _make_guarded_app()

        resp = TestClient(app).post(
            "/guarded", headers={"Authorization": "Bearer anything"},
        )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"]["code"], "platform_admin_not_configured")

    def test_missing_bearer_header_is_401(self) -> None:
        """PO21: at least one mechanism configured, but no Authorization header."""
        from app import config

        config.PLATFORM_ADMIN_TOKEN = "s3cr3t"
        app = _make_guarded_app()

        resp = TestClient(app).post("/guarded")

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"]["code"], "platform_admin_token_required")

    def test_token_matches_neither_mechanism_is_401(self) -> None:
        """PO22: verifier and static token both configured; token satisfies neither."""
        from app import config
        from app.platform.platform_operator_identity import PlatformTokenError

        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-real"
        app = _make_guarded_app()
        app.state.platform_operator_jwt_verifier = AsyncMock(
            verify=AsyncMock(
                side_effect=PlatformTokenError("platform_operator_token_invalid", "bad")
            )
        )

        resp = TestClient(app).post(
            "/guarded", headers={"Authorization": "Bearer wrong-token"},
        )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"]["code"], "platform_admin_token_invalid")

    def test_static_fallback_warning_names_the_request_path(self) -> None:
        """PO23: the deprecated-use WARNING identifies which path used it."""
        from app import config

        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-path"
        app = _make_guarded_app()

        # See test_invalid_jwt_falls_back_to_static_token for why this mocks
        # the logger directly rather than using assertLogs.
        with patch("app.meta.platform_api.logger") as mock_logger:
            resp = TestClient(app).post(
                "/guarded", headers={"Authorization": "Bearer s3cr3t-path"},
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args
        self.assertIn("/guarded", warning_args.args[1:])


if __name__ == "__main__":
    unittest.main()
