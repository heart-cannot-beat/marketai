"""Tests for JWKS revoked-key denylist (spec §110 key rotation).

Covers:
  - A revoked kid is rejected even if present in the JWKS cache.
  - Revocation check runs before the hard-stale check.
  - Runtime revoke_kid adds to the denylist.
  - The overlap window still works for non-revoked keys.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.platform.jwks_store import (
    JwksKeyStore,
    RevokedKidError,
    UnknownKidError,
)


VALID_KEY = {
    "kty": "EC",
    "crv": "P-256",
    "kid": "dsk_active",
    "use": "sig",
    "alg": "ES256",
    "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
    "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
}

ROTATED_KEY = {
    "kty": "EC",
    "crv": "P-256",
    "kid": "dsk_rotated",
    "use": "sig",
    "alg": "ES256",
    "x": "MKBCTNIcKUSDii11ySs3526iDZ8AiTo7Tu6KPAqv7D4",
    "y": "4Etl6SRW2YiLUrN5vfvVHuhp7x8PxltmWWlbbM4IFyM",
}


def make_jwks_response(keys, status_code=200, etag='"test-etag"'):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"keys": keys}
    response.headers = {"ETag": etag}
    response.raise_for_status = MagicMock()
    return response


class TestJwksRevocation(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http_client = AsyncMock()

    async def test_revoked_kid_rejected_even_when_cached(self):
        self.http_client.get.return_value = make_jwks_response([VALID_KEY])
        store = JwksKeyStore(
            jwks_url="http://localhost/.well-known/jwks.json",
            max_stale_seconds=600,
            http_client=self.http_client,
            revoked_kids=frozenset({"dsk_active"}),
        )
        await store.initial_load()

        with self.assertRaises(RevokedKidError) as cm:
            await store.get_key("dsk_active")
        self.assertEqual(cm.exception.kid, "dsk_active")

    async def test_revocation_check_before_stale_check(self):
        """A revoked kid raises RevokedKidError, not JwksNotYetLoadedError."""
        store = JwksKeyStore(
            jwks_url="http://localhost/.well-known/jwks.json",
            max_stale_seconds=600,
            http_client=self.http_client,
            revoked_kids=frozenset({"dsk_revoked"}),
        )
        # No initial load — normally would raise JwksNotYetLoadedError.
        with self.assertRaises(RevokedKidError):
            await store.get_key("dsk_revoked")

    async def test_runtime_revoke_kid(self):
        self.http_client.get.return_value = make_jwks_response([VALID_KEY])
        store = JwksKeyStore(
            jwks_url="http://localhost/.well-known/jwks.json",
            max_stale_seconds=600,
            http_client=self.http_client,
        )
        await store.initial_load()

        key = await store.get_key("dsk_active")
        self.assertIsNotNone(key)

        store.revoke_kid("dsk_active")

        with self.assertRaises(RevokedKidError):
            await store.get_key("dsk_active")

    async def test_overlap_window_works_for_non_revoked_keys(self):
        """Both old and new keys are accepted during the overlap window."""
        self.http_client.get.return_value = make_jwks_response(
            [VALID_KEY, ROTATED_KEY]
        )
        store = JwksKeyStore(
            jwks_url="http://localhost/.well-known/jwks.json",
            max_stale_seconds=600,
            http_client=self.http_client,
            revoked_kids=frozenset(),
        )
        await store.initial_load()

        active = await store.get_key("dsk_active")
        rotated = await store.get_key("dsk_rotated")
        self.assertIsNotNone(active)
        self.assertIsNotNone(rotated)

    async def test_revoked_kid_blocked_during_overlap(self):
        """Revoking the old key during overlap blocks it while keeping the new."""
        self.http_client.get.return_value = make_jwks_response(
            [VALID_KEY, ROTATED_KEY]
        )
        store = JwksKeyStore(
            jwks_url="http://localhost/.well-known/jwks.json",
            max_stale_seconds=600,
            http_client=self.http_client,
            revoked_kids=frozenset({"dsk_active"}),
        )
        await store.initial_load()

        with self.assertRaises(RevokedKidError):
            await store.get_key("dsk_active")

        rotated = await store.get_key("dsk_rotated")
        self.assertIsNotNone(rotated)


if __name__ == "__main__":
    unittest.main()
