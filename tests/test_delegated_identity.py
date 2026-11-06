"""DelegatedIdentityMiddleware: strict JWT validation and the epoch/entitlement
gates (spec §3). This is the FastAPI-side security boundary for #17, so the
suite below drives the middleware end to end with real ES256 signatures
(via PyJWT + `cryptography`) over a bare ASGI app, in addition to the
brief's targeted unit tests for claim-set shape and gate direction.
"""
from __future__ import annotations

import asyncio
import logging
import time
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.exc import ProgrammingError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.platform.delegated_identity import (
    EXPECTED_CLAIMS,
    DelegatedIdentityMiddleware,
    IdentityConfig,
    TokenVerificationError,
    VerifiedDelegatedIdentity,
)
from app.platform.jwks_store import (
    JwksNotYetLoadedError,
    JwksStaleError,
    UnknownKidError,
)
from app.platform.projection_sync import (
    AuthorizationSnapshot,
    ControlPlaneAuthorizationSnapshotHttpAdapter,
    ProjectionUpstreamUnavailableError,
    WorkspaceNotFoundUpstreamError,
    WorkspaceProjectionSyncer,
)
from app.platform.workspace_admission import WorkspaceAdmission, WorkspaceNotFoundError

ISSUER = "urn:marketai:control-plane"
AUDIENCE = "urn:marketai:domain-api"


# ---------------------------------------------------------------- fakes ----
class FakeJwksStore:
    """Satisfies the one method the middleware calls: `get_key(kid)`."""

    def __init__(self, keys: dict[str, "jwt.PyJWK"] | None = None, error: Exception | None = None):
        self._keys = keys or {}
        self._error = error

    async def get_key(self, kid: str) -> "jwt.PyJWK":
        if self._error is not None:
            raise self._error
        if kid not in self._keys:
            raise UnknownKidError(kid)
        return self._keys[kid]


class FakeAdmissionReader:
    def __init__(self, admissions: dict[str, WorkspaceAdmission] | None = None):
        self._admissions = dict(admissions or {})

    async def read(self, workspace_id: str) -> WorkspaceAdmission:
        try:
            return self._admissions[workspace_id]
        except KeyError:
            raise WorkspaceNotFoundError(workspace_id)

    def set(self, workspace_id: str, admission: WorkspaceAdmission) -> None:
        self._admissions[workspace_id] = admission


class FakeProjectionSyncer:
    """`outcome` selects what `sync()` does; `on_success` lets a test wire the
    repair into a `FakeAdmissionReader` so the middleware's re-read sees the
    "repaired" projection, the way a real sync would.
    """

    def __init__(self, outcome: str = "success", on_success=None):
        self.outcome = outcome
        self.calls: list[str] = []
        self._on_success = on_success

    async def sync(self, workspace_id: str) -> bool:
        self.calls.append(workspace_id)
        if self.outcome == "not_found":
            raise WorkspaceNotFoundUpstreamError(workspace_id)
        if self.outcome == "unavailable":
            raise ProjectionUpstreamUnavailableError("upstream down")
        if self._on_success is not None:
            self._on_success(workspace_id)
        return True


# ------------------------------------------------------------- app setup ----
async def _echo(request):
    tc = request.state.tenant_context
    return JSONResponse(
        {
            "workspace_id": tc.workspace_id,
            "role": tc.role,
            "initiator_id": tc.initiator_id,
            "authorization_epoch": tc.authorization_epoch,
            "request_id": tc.request_id,
            "trace_id": tc.trace_id,
            "ui_language": tc.ui_language,
        }
    )


async def _legacy(request):
    del request
    return JSONResponse({"ok": True})


def _inner_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/api/v1/echo", _echo, methods=["GET", "POST"]),
            Route("/api/legacy", _legacy),
        ]
    )


DEFAULT_CONFIG = IdentityConfig(
    issuer=ISSUER,
    audience=AUDIENCE,
    max_clock_skew_seconds=5,
    max_token_bytes=4096,
    max_token_ttl_seconds=60,
    projection_repair_interval_seconds=60,
)


def build_client(
    *,
    jwks_store=None,
    admission_reader=None,
    projection_syncer=None,
    config: IdentityConfig = DEFAULT_CONFIG,
) -> TestClient:
    middleware = DelegatedIdentityMiddleware(
        _inner_app(),
        jwks_store=jwks_store if jwks_store is not None else FakeJwksStore(),
        admission_reader=admission_reader if admission_reader is not None else FakeAdmissionReader(),
        projection_syncer=projection_syncer if projection_syncer is not None else FakeProjectionSyncer(),
        config=config,
    )
    return TestClient(middleware)


def generate_keypair(kid: str = "dsk_test"):
    """A fresh ES256 keypair plus the JWK dict `FakeJwksStore` installs."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    alg = jwt.algorithms.ECAlgorithm(jwt.algorithms.ECAlgorithm.SHA256)
    import json

    public_jwk = json.loads(alg.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return private_key, jwt.PyJWK.from_dict(public_jwk)


def base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user_1",
        "workspace_id": "ws_1",
        "membership_id": "mem_1",
        "role": "owner",
        "session_id": "sess_1",
        "authorization_epoch": 1,
        "entitlement_version": 1,
        "jti": f"jti_{uuid.uuid4()}",
        "iat": now,
        "nbf": now,
        "exp": now + 30,
    }
    claims.update(overrides)
    return claims


def sign(private_key, claims: dict, *, kid: str = "dsk_test", alg: str = "ES256", typ: str | None = "delegated+jwt"):
    headers = {"kid": kid}
    if typ is not None:
        headers["typ"] = typ
    return jwt.encode(claims, private_key, algorithm=alg, headers=headers)


def admitted(**overrides) -> WorkspaceAdmission:
    fields = dict(workspace_id="ws_1", authorization_epoch=1, entitlement_version=1, suspended=False)
    fields.update(overrides)
    return WorkspaceAdmission(**fields)


class ClaimShapeTests(unittest.TestCase):
    """From the task brief: the 13-claim contract, checked as data."""

    def test_expected_claims_has_exactly_13(self) -> None:
        self.assertEqual(len(EXPECTED_CLAIMS), 13)

    def test_expected_claims_contents(self) -> None:
        expected = {
            "iss", "aud", "sub", "workspace_id", "membership_id", "role",
            "session_id", "authorization_epoch", "entitlement_version",
            "jti", "iat", "nbf", "exp",
        }
        self.assertEqual(EXPECTED_CLAIMS, expected)


class EpochGateTests(unittest.TestCase):
    def _identity(self, epoch: int) -> VerifiedDelegatedIdentity:
        return VerifiedDelegatedIdentity(
            sub="u1", workspace_id="ws1", membership_id="m1", role="owner",
            session_id="s1", authorization_epoch=epoch, entitlement_version=0, jti="jti_test",
        )

    def _admission(self, epoch: int) -> WorkspaceAdmission:
        return WorkspaceAdmission(
            workspace_id="ws1", authorization_epoch=epoch, entitlement_version=0, suspended=False,
        )

    def test_epoch_match_returns_none(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        self.assertIsNone(mid._check_authorization_epoch(self._identity(1), self._admission(1)))

    def test_claim_behind_returns_stale(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        result = mid._check_authorization_epoch(self._identity(0), self._admission(1))
        self.assertEqual(result, "authorization_epoch_stale")

    def test_claim_ahead_returns_sync_pending(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        result = mid._check_authorization_epoch(self._identity(2), self._admission(1))
        self.assertEqual(result, "authorization_sync_pending")


class EntitlementGateTests(unittest.TestCase):
    def _identity(self, version: int) -> VerifiedDelegatedIdentity:
        return VerifiedDelegatedIdentity(
            sub="u1", workspace_id="ws1", membership_id="m1", role="owner",
            session_id="s1", authorization_epoch=0, entitlement_version=version, jti="jti_test",
        )

    def _admission(self, version: int) -> WorkspaceAdmission:
        return WorkspaceAdmission(
            workspace_id="ws1", authorization_epoch=0, entitlement_version=version, suspended=False,
        )

    def test_match_returns_none(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        result = mid._check_entitlement_version(self._identity(2), self._admission(2), {"method": "POST"})
        self.assertIsNone(result)

    def test_claim_ahead_blocks_a_write(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                result = mid._check_entitlement_version(self._identity(3), self._admission(2), {"method": method})
                self.assertEqual(result, "entitlement_sync_pending")

    def test_claim_ahead_allows_a_read(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        result = mid._check_entitlement_version(self._identity(3), self._admission(2), {"method": "GET"})
        self.assertIsNone(result)

    def test_claim_behind_uses_the_newer_local_projection(self) -> None:
        mid = DelegatedIdentityMiddleware.__new__(DelegatedIdentityMiddleware)
        result = mid._check_entitlement_version(self._identity(1), self._admission(2), {"method": "POST"})
        self.assertIsNone(result)


class TokenValidationEndToEndTests(unittest.TestCase):
    """Full request path through a real ASGI TestClient, one rejection reason
    at a time. Each test builds its own middleware instance so the
    projection-repair rate limiter never leaks state across tests."""

    def setUp(self) -> None:
        self.private_key, self.jwk = generate_keypair()
        self.jwks_store = FakeJwksStore({"dsk_test": self.jwk})
        self.admission_reader = FakeAdmissionReader({"ws_1": admitted()})

    def _client(self, **kwargs) -> TestClient:
        kwargs.setdefault("jwks_store", self.jwks_store)
        kwargs.setdefault("admission_reader", self.admission_reader)
        return build_client(**kwargs)

    def test_missing_token_is_rejected(self) -> None:
        with self._client() as client:
            response = client.get("/api/v1/echo")
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["code"], "delegated_identity_required")
        self.assertIn("trace_id", body)
        self.assertFalse(body["retryable"])
        self.assertIn("Bearer", response.headers["www-authenticate"])

    def test_malformed_token_is_rejected(self) -> None:
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": "Bearer not-a-jwt"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "malformed_token")

    def test_oversized_token_is_rejected(self) -> None:
        config = replace(DEFAULT_CONFIG, max_token_bytes=10)
        with self._client(config=config) as client:
            token = sign(self.private_key, base_claims())
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "token_too_large")

    def test_non_es256_algorithm_is_rejected(self) -> None:
        token = jwt.encode(
            base_claims(), "some-hs256-secret", algorithm="HS256",
            headers={"kid": "dsk_test", "typ": "delegated+jwt"},
        )
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "algorithm_not_allowed")

    def test_wrong_typ_header_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(), typ="JWT")
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_token_type")

    def test_unknown_kid_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(), kid="dsk_unknown")
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "unknown_signing_key")

    def test_expired_token_is_rejected(self) -> None:
        now = int(time.time())
        token = sign(self.private_key, base_claims(iat=now - 120, nbf=now - 120, exp=now - 60))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "token_expired")

    def test_not_yet_valid_token_is_rejected(self) -> None:
        now = int(time.time())
        token = sign(self.private_key, base_claims(iat=now, nbf=now + 120, exp=now + 150))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "token_not_yet_valid")

    def test_wrong_issuer_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(iss="urn:marketai:someone-else"))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_issuer")

    def test_wrong_audience_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(aud="urn:marketai:someone-else"))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_audience")

    def test_ttl_exceeded_is_rejected(self) -> None:
        now = int(time.time())
        token = sign(self.private_key, base_claims(iat=now, nbf=now, exp=now + 3600))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "token_ttl_exceeded")

    def test_invalid_jti_format_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(jti="not-a-valid-jti"))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_jti_format")

    def test_extra_claim_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(extra_field="not allowed"))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "unexpected_claims")

    def test_missing_claim_is_rejected(self) -> None:
        claims = base_claims()
        del claims["membership_id"]
        token = sign(self.private_key, claims)
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "malformed_token")

    def test_invalid_role_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(role="superadmin"))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_role")

    def test_negative_authorization_epoch_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(authorization_epoch=-1))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_authorization_epoch")

    def test_negative_entitlement_version_is_rejected(self) -> None:
        token = sign(self.private_key, base_claims(entitlement_version=-1))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_entitlement_version")

    def test_signature_from_a_different_key_is_rejected(self) -> None:
        other_private_key, _ = generate_keypair(kid="dsk_test")
        token = sign(other_private_key, base_claims())
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_signature")

    def test_jwks_not_yet_loaded_returns_503(self) -> None:
        token = sign(self.private_key, base_claims())
        client = self._client(jwks_store=FakeJwksStore(error=JwksNotYetLoadedError()))
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "jwks_unavailable")
        self.assertEqual(response.headers["retry-after"], "5")

    def test_jwks_hard_stale_returns_503(self) -> None:
        token = sign(self.private_key, base_claims())
        client = self._client(jwks_store=FakeJwksStore(error=JwksStaleError(999, 600)))
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "jwks_unavailable")

    def test_valid_token_returns_200_and_a_matching_tenant_context(self) -> None:
        token = sign(self.private_key, base_claims())
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workspace_id"], "ws_1")
        self.assertEqual(body["role"], "owner")
        self.assertEqual(body["initiator_id"], "user_1")
        self.assertEqual(body["authorization_epoch"], 1)
        self.assertTrue(body["request_id"])
        self.assertEqual(body["trace_id"], body["request_id"])

    def test_valid_member_token_carries_the_member_role(self) -> None:
        token = sign(self.private_key, base_claims(role="member"))
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "member")

    def test_trace_id_propagates_from_the_incoming_header(self) -> None:
        token = sign(self.private_key, base_claims())
        with self._client() as client:
            response = client.get(
                "/api/v1/echo",
                headers={"Authorization": f"Bearer {token}", "X-Trace-Id": "trace-from-bff-12345"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace_id"], "trace-from-bff-12345")

    def test_an_invalid_trace_id_header_is_not_trusted(self) -> None:
        token = sign(self.private_key, base_claims())
        with self._client() as client:
            response = client.get(
                "/api/v1/echo",
                headers={"Authorization": f"Bearer {token}", "X-Trace-Id": "short"},
            )
        self.assertEqual(response.status_code, 200)
        # Too short to match the correlation-id pattern -> falls back to a
        # generated request_id instead of trusting the raw header value.
        self.assertNotEqual(response.json()["trace_id"], "short")

    def test_ui_language_header_reaches_the_tenant_context(self) -> None:
        token = sign(self.private_key, base_claims())
        with self._client() as client:
            response = client.get(
                "/api/v1/echo",
                headers={"Authorization": f"Bearer {token}", "X-UI-Language": "zh-CN"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ui_language"], "zh-CN")

    def test_a_missing_or_unknown_ui_language_reads_as_english(self) -> None:
        token = sign(self.private_key, base_claims())
        for headers in ({}, {"X-UI-Language": "fr"}, {"X-UI-Language": "zh"}, {"X-UI-Language": "ZH-CN"}):
            with self._client() as client:
                response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}", **headers})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ui_language"], "en", headers)

    def test_ui_language_cannot_change_the_workspace_or_actor(self) -> None:
        # The header is wording only: the tenant, role and user stay the
        # verified token's even when the value looks like an identity.
        token = sign(self.private_key, base_claims(role="member"))
        with self._client() as client:
            response = client.get(
                "/api/v1/echo",
                headers={"Authorization": f"Bearer {token}", "X-UI-Language": "ws_2"},
            )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((body["workspace_id"], body["role"], body["initiator_id"], body["ui_language"]),
                         ("ws_1", "member", "user_1", "en"))

    def test_ui_language_does_not_admit_a_request_without_a_token(self) -> None:
        with self._client() as client:
            response = client.get("/api/v1/echo", headers={"X-UI-Language": "zh-CN"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "delegated_identity_required")

    def test_legacy_path_is_exempt_from_the_middleware(self) -> None:
        with self._client() as client:
            response = client.get("/api/legacy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})


class WorkspaceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key, self.jwk = generate_keypair()
        self.jwks_store = FakeJwksStore({"dsk_test": self.jwk})

    def test_workspace_not_found_then_sync_provisions_it(self) -> None:
        reader = FakeAdmissionReader()  # empty: no ws_1 row yet
        syncer = FakeProjectionSyncer(
            outcome="success",
            on_success=lambda ws_id: reader.set(ws_id, admitted()),
        )
        token = sign(self.private_key, base_claims())
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(syncer.calls, ["ws_1"])

    def test_first_projection_repair_is_allowed_on_a_fresh_process(self) -> None:
        """A VM with less uptime than the limiter interval still gets its first permit."""

        reader = FakeAdmissionReader()
        syncer = FakeProjectionSyncer(
            outcome="success",
            on_success=lambda ws_id: reader.set(ws_id, admitted()),
        )
        token = sign(self.private_key, base_claims())
        client = build_client(
            jwks_store=self.jwks_store,
            admission_reader=reader,
            projection_syncer=syncer,
        )

        with patch("app.platform.delegated_identity.time.monotonic", return_value=1.0):
            with client:
                response = client.get(
                    "/api/v1/echo",
                    headers={"Authorization": f"Bearer {token}"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(syncer.calls, ["ws_1"])

    def test_workspace_not_found_and_sync_confirms_not_found_returns_401(self) -> None:
        reader = FakeAdmissionReader()
        syncer = FakeProjectionSyncer(outcome="not_found")
        token = sign(self.private_key, base_claims())
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "workspace_not_found")

    def test_workspace_not_found_and_sync_unavailable_returns_503(self) -> None:
        reader = FakeAdmissionReader()
        syncer = FakeProjectionSyncer(outcome="unavailable")
        token = sign(self.private_key, base_claims())
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "projection_sync_unavailable")
        self.assertEqual(response.headers["retry-after"], "5")

    def test_epoch_stale_is_rejected_without_a_repair_attempt(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(authorization_epoch=5)})
        syncer = FakeProjectionSyncer(outcome="success")
        token = sign(self.private_key, base_claims(authorization_epoch=2))
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "authorization_epoch_stale")
        self.assertTrue(body["retryable"])
        # No retry_after: the client retries once the BFF reissues a token
        # with the current epoch, not after a fixed delay (spec §3.5).
        self.assertNotIn("retry_after", body)
        self.assertEqual(syncer.calls, [])

    def test_epoch_ahead_triggers_a_repair_and_then_succeeds(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(authorization_epoch=1)})
        syncer = FakeProjectionSyncer(
            outcome="success",
            on_success=lambda ws_id: reader.set(ws_id, admitted(authorization_epoch=3)),
        )
        token = sign(self.private_key, base_claims(authorization_epoch=3))
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["authorization_epoch"], 3)
        self.assertEqual(syncer.calls, ["ws_1"])

    def test_epoch_ahead_repair_still_behind_returns_403_sync_pending(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(authorization_epoch=1)})
        # The repair "succeeds" (returns True) but the local row is still
        # behind the claim -- e.g. a second control-plane change landed
        # after this snapshot. The gate must re-evaluate and still reject.
        syncer = FakeProjectionSyncer(outcome="success")
        token = sign(self.private_key, base_claims(authorization_epoch=3))
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "authorization_sync_pending")
        self.assertEqual(body["retry_after"], 5)

    def test_epoch_ahead_repair_unavailable_returns_503(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(authorization_epoch=1)})
        syncer = FakeProjectionSyncer(outcome="unavailable")
        token = sign(self.private_key, base_claims(authorization_epoch=3))
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "projection_sync_unavailable")

    def test_entitlement_ahead_blocks_a_write_request(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(entitlement_version=1)})
        syncer = FakeProjectionSyncer(outcome="unavailable")
        token = sign(self.private_key, base_claims(entitlement_version=3))
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.post("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 503)  # repair attempted and unavailable
        self.assertEqual(response.json()["code"], "projection_sync_unavailable")

    def test_entitlement_ahead_allows_a_read_request_without_a_repair(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(entitlement_version=1)})
        syncer = FakeProjectionSyncer(outcome="unavailable")
        token = sign(self.private_key, base_claims(entitlement_version=3))
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(syncer.calls, [])

    def test_suspended_workspace_returns_403_after_the_epoch_and_entitlement_gates(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted(suspended=True)})
        token = sign(self.private_key, base_claims())
        client = build_client(jwks_store=self.jwks_store, admission_reader=reader)
        with client:
            response = client.get("/api/v1/echo", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "workspace_suspended")


LOGGER = "app.platform.delegated_identity"
TRACE_ID = "trace_sync_0001"
SENTINEL_TOKEN = "svc_sentinel_token_7f3a"
SENTINEL_BODY = "body_sentinel_91c2"
SENTINEL_HOST = "control-plane-sentinel.internal"


class InsufficientPrivilege(Exception):
    """Stands in for the psycopg driver error SQLAlchemy keeps on `orig`."""


class SnapshotFetcher:
    async def fetch(self, workspace_id: str) -> AuthorizationSnapshot:
        return AuthorizationSnapshot(
            workspace_id=workspace_id,
            workspace_name="Acme",
            authorization_epoch=1,
            entitlement_version=1,
            suspended=False,
            projection_version=1,
        )


class ProjectionStore:
    """`apply()` raises `error` when one is given, else reports an applied row."""

    def __init__(self, error: Exception | None = None):
        self._error = error

    async def apply(self, snapshot: AuthorizationSnapshot) -> bool:
        if self._error is not None:
            raise self._error
        return True


def log_fields(message: str) -> dict[str, str]:
    """The `key=value` pairs after the `projection_sync` event name."""
    event, *pairs = message.split(" ")
    assert event == "projection_sync", message
    return dict(pair.split("=", 1) for pair in pairs)


class ProjectionSyncLoggingTests(unittest.TestCase):
    """Each on-demand projection sync leaves one `projection_sync` warning whose
    fields are in the message itself: Uvicorn's default logging prints only the
    message, so the assertions read the message and never `extra`.
    """

    def setUp(self) -> None:
        self.private_key, self.jwk = generate_keypair()
        self.jwks_store = FakeJwksStore({"dsk_test": self.jwk})
        # Migration tests run Alembic in this process, and its `fileConfig`
        # disables every already-imported logger (see `tests.test_asset_thumbnails`).
        logger = logging.getLogger(LOGGER)
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False

    def _request(self, client: TestClient, method: str = "GET", **claims):
        token = sign(self.private_key, base_claims(**claims))
        return client.request(
            method,
            "/api/v1/echo",
            headers={"Authorization": f"Bearer {token}", "X-Trace-Id": TRACE_ID},
        )

    def _client(self, reader, syncer) -> TestClient:
        return build_client(jwks_store=self.jwks_store, admission_reader=reader, projection_syncer=syncer)

    def _real_syncer(self, handler, store: ProjectionStore | None = None) -> WorkspaceProjectionSyncer:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addCleanup(lambda: asyncio.run(http_client.aclose()))
        adapter = ControlPlaneAuthorizationSnapshotHttpAdapter(
            base_url=f"https://{SENTINEL_HOST}",
            service_token=SENTINEL_TOKEN,
            timeout_ms=3000,
            http_client=http_client,
        )
        return WorkspaceProjectionSyncer(adapter, store or ProjectionStore())

    def _only_message(self, captured) -> str:
        self.assertEqual(len(captured.records), 1, [r.getMessage() for r in captured.records])
        record = captured.records[0]
        # No traceback either: it would print the exception message.
        self.assertIsNone(record.exc_info)
        return record.getMessage()

    def test_missing_row_while_rate_limited_returns_503(self) -> None:
        reader = FakeAdmissionReader()
        syncer = FakeProjectionSyncer(outcome="unavailable")
        with self._client(reader, syncer) as client:
            first = self._request(client)
            second = self._request(client)
        for response in (first, second):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "projection_sync_unavailable")
            self.assertEqual(response.headers["retry-after"], "5")
        self.assertEqual(syncer.calls, ["ws_1"])

    def test_each_real_attempt_logs_one_line_with_its_result(self) -> None:
        cases = (
            ("success", 200, {"result": "synced", "error_class": "-"}),
            ("not_found", 401, {"result": "not_found", "error_class": "WorkspaceNotFoundUpstreamError"}),
            (
                "unavailable",
                503,
                {"result": "upstream_unavailable", "error_class": "ProjectionUpstreamUnavailableError"},
            ),
        )
        for outcome, status, expected in cases:
            with self.subTest(outcome=outcome):
                reader = FakeAdmissionReader()
                syncer = FakeProjectionSyncer(
                    outcome=outcome,
                    on_success=lambda ws_id, reader=reader: reader.set(ws_id, admitted()),
                )
                with self.assertLogs(LOGGER, level="WARNING") as captured, self._client(reader, syncer) as client:
                    response = self._request(client)
                self.assertEqual(response.status_code, status)

                fields = log_fields(self._only_message(captured))
                self.assertEqual(
                    fields,
                    {
                        "trigger": "missing_row",
                        "workspace_id": "ws_1",
                        "trace_id": TRACE_ID,
                        "elapsed_ms": fields["elapsed_ms"],
                        "cause_class": "-",
                        "http_status": "-",
                        "rate_limited_since_last": "0",
                        **expected,
                    },
                )
                self.assertRegex(fields["elapsed_ms"], r"^\d+$")

    def test_a_rate_limited_sync_logs_the_request_fields_only(self) -> None:
        reader = FakeAdmissionReader()
        syncer = FakeProjectionSyncer(outcome="unavailable")
        with self.assertLogs(LOGGER, level="WARNING") as captured, self._client(reader, syncer) as client:
            self._request(client)
            self._request(client)

        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(len(messages), 2, messages)
        self.assertEqual(
            log_fields(messages[1]),
            {
                "trigger": "missing_row",
                "result": "rate_limited",
                "workspace_id": "ws_1",
                "trace_id": TRACE_ID,
            },
        )

    def test_rate_limited_syncs_log_once_per_window_and_the_next_attempt_counts_them(self) -> None:
        reader = FakeAdmissionReader()
        syncer = FakeProjectionSyncer(outcome="unavailable")
        clock = [1000.0]
        with (
            patch("app.platform.delegated_identity.time.monotonic", side_effect=lambda: clock[0]),
            self.assertLogs(LOGGER, level="WARNING") as captured,
            self._client(reader, syncer) as client,
        ):
            for _ in range(5):
                self.assertEqual(self._request(client).status_code, 503)
            clock[0] += DEFAULT_CONFIG.projection_repair_interval_seconds + 1
            self.assertEqual(self._request(client).status_code, 503)

        lines = [log_fields(record.getMessage()) for record in captured.records]
        self.assertEqual(
            [line["result"] for line in lines],
            ["upstream_unavailable", "rate_limited", "upstream_unavailable"],
        )
        self.assertEqual(lines[0]["rate_limited_since_last"], "0")
        self.assertEqual(lines[2]["rate_limited_since_last"], "4")
        self.assertEqual(syncer.calls, ["ws_1", "ws_1"])

    def test_an_upstream_status_is_logged_without_the_token_url_or_body(self) -> None:
        sent: list[str] = []

        def forbidden(request: httpx.Request) -> httpx.Response:
            sent.append(request.headers["authorization"])
            return httpx.Response(403, text=f"forbidden {SENTINEL_BODY}")

        with (
            self.assertLogs(LOGGER, level="WARNING") as captured,
            self._client(FakeAdmissionReader(), self._real_syncer(forbidden)) as client,
        ):
            response = self._request(client)

        self.assertEqual(response.status_code, 503)
        # The sentinel token really went out, so a leak would have shown it.
        self.assertEqual(sent, [f"Bearer {SENTINEL_TOKEN}"])
        message = self._only_message(captured)
        for secret in (SENTINEL_TOKEN, "Bearer", SENTINEL_BODY, SENTINEL_HOST, "https://"):
            self.assertNotIn(secret, message)
        fields = log_fields(message)
        self.assertEqual(fields["result"], "upstream_unavailable")
        self.assertEqual(fields["error_class"], "ProjectionUpstreamUnavailableError")
        self.assertEqual(fields["cause_class"], "-")
        self.assertEqual(fields["http_status"], "403")

    def test_a_transport_error_is_logged_by_class_without_its_message(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                f"cannot reach {request.url} with {request.headers['authorization']} {SENTINEL_BODY}",
                request=request,
            )

        with (
            self.assertLogs(LOGGER, level="WARNING") as captured,
            self._client(FakeAdmissionReader(), self._real_syncer(unreachable)) as client,
        ):
            response = self._request(client)

        self.assertEqual(response.status_code, 503)
        message = self._only_message(captured)
        for secret in (SENTINEL_TOKEN, "Bearer", SENTINEL_BODY, SENTINEL_HOST, "https://"):
            self.assertNotIn(secret, message)
        fields = log_fields(message)
        self.assertEqual(fields["error_class"], "ProjectionUpstreamUnavailableError")
        self.assertEqual(fields["cause_class"], "ConnectError")
        self.assertEqual(fields["http_status"], "-")

    def test_an_empty_base_url_is_logged_as_unsupported_protocol(self) -> None:
        # A real client: the relative snapshot URL fails before any network I/O.
        http_client = httpx.AsyncClient()
        self.addCleanup(lambda: asyncio.run(http_client.aclose()))
        adapter = ControlPlaneAuthorizationSnapshotHttpAdapter(
            base_url="", service_token=SENTINEL_TOKEN, timeout_ms=3000, http_client=http_client
        )
        syncer = WorkspaceProjectionSyncer(adapter, ProjectionStore())

        with self.assertLogs(LOGGER, level="WARNING") as captured, self._client(FakeAdmissionReader(), syncer) as client:
            response = self._request(client)

        self.assertEqual(response.status_code, 503)
        fields = log_fields(self._only_message(captured))
        self.assertEqual(fields["cause_class"], "UnsupportedProtocol")

    def test_a_projection_write_failure_logs_the_driver_error_class(self) -> None:
        driver_error = InsufficientPrivilege("permission denied for function marketai_apply_workspace_projection")
        store = ProjectionStore(ProgrammingError("SELECT marketai_apply_workspace_projection()", {}, driver_error))
        syncer = WorkspaceProjectionSyncer(SnapshotFetcher(), store)

        with self.assertLogs(LOGGER, level="WARNING") as captured, self._client(FakeAdmissionReader(), syncer) as client:
            response = self._request(client)

        self.assertEqual(response.status_code, 503)
        message = self._only_message(captured)
        self.assertNotIn("permission denied", message)
        self.assertNotIn("SELECT", message)
        fields = log_fields(message)
        self.assertEqual(fields["result"], "upstream_unavailable")
        self.assertEqual(fields["error_class"], "ProgrammingError")
        self.assertEqual(fields["cause_class"], "InsufficientPrivilege")

    def test_a_claim_ahead_of_the_projection_names_its_trigger(self) -> None:
        cases = (
            ("GET", {"authorization_epoch": 3}, "epoch_ahead"),
            ("POST", {"entitlement_version": 3}, "entitlement_ahead"),
        )
        for method, claims, trigger in cases:
            with self.subTest(trigger=trigger):
                reader = FakeAdmissionReader({"ws_1": admitted()})
                syncer = FakeProjectionSyncer(outcome="unavailable")
                with self.assertLogs(LOGGER, level="WARNING") as captured, self._client(reader, syncer) as client:
                    response = self._request(client, method, **claims)
                self.assertEqual(response.status_code, 503)
                fields = log_fields(self._only_message(captured))
                self.assertEqual((fields["trigger"], fields["result"]), (trigger, "upstream_unavailable"))

    def test_a_current_projection_logs_nothing(self) -> None:
        reader = FakeAdmissionReader({"ws_1": admitted()})
        syncer = FakeProjectionSyncer(outcome="unavailable")
        with self.assertNoLogs(LOGGER, level="DEBUG"), self._client(reader, syncer) as client:
            for method in ("GET", "POST"):
                self.assertEqual(self._request(client, method).status_code, 200)
        self.assertEqual(syncer.calls, [])


class TokenVerificationErrorTests(unittest.TestCase):
    def test_carries_code_and_description(self) -> None:
        err = TokenVerificationError("some_code", "Some description")
        self.assertEqual(err.code, "some_code")
        self.assertEqual(err.description, "Some description")
        self.assertEqual(str(err), "Some description")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
