"""JwksKeyStore: hard-stale, single-flight refresh, ETag handling (spec §3.2)."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.platform.jwks_store import (
    JwksInitialLoadError,
    JwksKeyStore,
    JwksNotYetLoadedError,
    JwksRefreshError,
    JwksStaleError,
    UnknownKidError,
)

VALID_KEY = {
    "kty": "EC",
    "crv": "P-256",
    "kid": "dsk_001",
    "use": "sig",
    "alg": "ES256",
    "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
    "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
}

OTHER_KEY = {
    "kty": "EC",
    "crv": "P-256",
    "kid": "dsk_002",
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


def make_store(http_client, max_stale_seconds=600):
    return JwksKeyStore(
        jwks_url="http://localhost/.well-known/jwks.json",
        max_stale_seconds=max_stale_seconds,
        http_client=http_client,
    )


class JwksKeyStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http_client = AsyncMock()

    async def test_initial_load_success(self) -> None:
        self.http_client.get.return_value = make_jwks_response([VALID_KEY])
        store = make_store(self.http_client)

        await store.initial_load()
        key = await store.get_key("dsk_001")

        self.assertIsNotNone(key)
        self.assertTrue(store.is_fresh())

    async def test_initial_load_empty_keys_fails(self) -> None:
        self.http_client.get.return_value = make_jwks_response([])
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError):
            await store.initial_load()
        self.assertFalse(store.is_fresh())

    async def test_initial_load_does_not_retry_by_default(self) -> None:
        self.http_client.get.side_effect = httpx.ConnectError("[Errno -2] Name or service not known")
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError):
            await store.initial_load()

        self.assertEqual(self.http_client.get.call_count, 1)
        self.assertFalse(store.is_fresh())

    async def test_initial_load_retries_transient_transport_failures(self) -> None:
        # Web and API restart together: the JWKS host does not resolve for
        # the first attempts, then answers normally.
        self.http_client.get.side_effect = [
            httpx.ConnectError("[Errno -2] Name or service not known"),
            httpx.ConnectError("[Errno -2] Name or service not known"),
            make_jwks_response([VALID_KEY]),
        ]
        store = make_store(self.http_client)

        await store.initial_load(attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0)

        self.assertTrue(store.is_fresh())
        self.assertIsNotNone(await store.get_key("dsk_001"))
        self.assertEqual(self.http_client.get.call_count, 3)

    async def test_initial_load_retries_an_empty_key_set(self) -> None:
        # Web may be up before its signer is bootstrapped; an empty JWKS is
        # transient at startup and shares the retry budget.
        self.http_client.get.side_effect = [
            make_jwks_response([]),
            make_jwks_response([VALID_KEY]),
        ]
        store = make_store(self.http_client)

        await store.initial_load(attempts=2, initial_backoff_seconds=0, max_backoff_seconds=0)

        self.assertTrue(store.is_fresh())
        self.assertEqual(self.http_client.get.call_count, 2)

    async def test_initial_load_gives_up_after_the_retry_budget(self) -> None:
        self.http_client.get.side_effect = httpx.ConnectError("[Errno -2] Name or service not known")
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError) as ctx:
            await store.initial_load(attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0)

        self.assertIsInstance(ctx.exception.__cause__, httpx.ConnectError)
        self.assertEqual(self.http_client.get.call_count, 3)
        self.assertFalse(store.is_fresh())

    async def test_initial_load_backoff_doubles_up_to_the_cap(self) -> None:
        self.http_client.get.side_effect = httpx.ConnectError("[Errno -2] Name or service not known")
        store = make_store(self.http_client)

        with patch("app.platform.jwks_store.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(JwksInitialLoadError):
                await store.initial_load(
                    attempts=7, initial_backoff_seconds=2, max_backoff_seconds=20
                )

        delays = [call.args[0] for call in sleep.await_args_list]
        # Six sleeps between seven attempts; never a sleep after the last one.
        self.assertEqual(delays, [2, 4, 8, 16, 20, 20])
        self.assertEqual(self.http_client.get.call_count, 7)

    async def test_initial_load_does_not_retry_unexpected_errors(self) -> None:
        # A programming error (not a transport/response failure) must surface
        # immediately rather than burn the whole retry budget.
        self.http_client.get.side_effect = RuntimeError("bug")
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError) as ctx:
            await store.initial_load(attempts=5, initial_backoff_seconds=0, max_backoff_seconds=0)

        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertEqual(self.http_client.get.call_count, 1)

    async def test_unknown_kid_triggers_refresh(self) -> None:
        self.http_client.get.return_value = make_jwks_response([VALID_KEY])
        store = make_store(self.http_client)
        await store.initial_load()

        with self.assertRaises(UnknownKidError):
            await store.get_key("dsk_nonexistent")

        # Initial load + one refresh attempt from the unknown-kid lookup.
        self.assertEqual(self.http_client.get.call_count, 2)

    async def test_304_updates_freshness_with_populated_cache(self) -> None:
        self.http_client.get.side_effect = [
            make_jwks_response([VALID_KEY], etag='"etag-1"'),
            make_jwks_response([], status_code=304),
        ]
        store = make_store(self.http_client)
        await store.initial_load()

        # Force a second refresh via an unknown kid; it should 304 and keep
        # the existing key installed and fresh.
        with self.assertRaises(UnknownKidError):
            await store.get_key("dsk_missing")
        key = await store.get_key("dsk_001")

        self.assertIsNotNone(key)

    async def test_304_on_an_empty_or_unetagged_cache_is_a_refresh_error(self) -> None:
        self.http_client.get.return_value = make_jwks_response([], status_code=304)
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError):
            await store.initial_load()

    async def test_empty_refresh_preserves_existing_keys(self) -> None:
        self.http_client.get.side_effect = [
            make_jwks_response([VALID_KEY]),
            make_jwks_response([]),
        ]
        store = make_store(self.http_client)
        await store.initial_load()

        # The refresh itself fails closed (an empty key set is never
        # installed), so the unknown-kid lookup surfaces that refresh
        # failure rather than a plain UnknownKidError.
        with self.assertRaises(JwksRefreshError):
            await store.get_key("dsk_new")

        key = await store.get_key("dsk_001")
        self.assertIsNotNone(key)

    async def test_duplicate_kid_in_one_response_fails_the_whole_refresh(self) -> None:
        self.http_client.get.return_value = make_jwks_response([VALID_KEY, VALID_KEY])
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError):
            await store.initial_load()

    async def test_a_key_missing_required_fields_is_skipped_not_fatal(self) -> None:
        malformed = {"kty": "EC", "crv": "P-256", "kid": "dsk_bad", "use": "sig", "alg": "ES256"}
        self.http_client.get.return_value = make_jwks_response([malformed, VALID_KEY])
        store = make_store(self.http_client)

        await store.initial_load()

        key = await store.get_key("dsk_001")
        self.assertIsNotNone(key)
        with self.assertRaises(UnknownKidError):
            await store.get_key("dsk_bad")
        # Both calls are covered by the single initial refresh — the
        # malformed key never triggers its own HTTP round trip.
        self.assertEqual(self.http_client.get.call_count, 2)

    async def test_hard_stale_rejects_verification_even_with_a_cached_kid(self) -> None:
        self.http_client.get.return_value = make_jwks_response([VALID_KEY])
        store = make_store(self.http_client, max_stale_seconds=1)
        await store.initial_load()
        store._last_successful_refresh_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        with self.assertRaises(JwksStaleError):
            await store.get_key("dsk_001")

    async def test_get_key_before_any_load_raises_not_yet_loaded(self) -> None:
        store = make_store(self.http_client)

        with self.assertRaises(JwksNotYetLoadedError):
            await store.get_key("dsk_001")

    async def test_concurrent_unknown_kid_lookups_share_one_http_call(self) -> None:
        self.http_client.get.side_effect = [
            make_jwks_response([VALID_KEY]),
            make_jwks_response([VALID_KEY, OTHER_KEY]),
        ]
        store = make_store(self.http_client)
        await store.initial_load()

        results = await asyncio.gather(
            store.get_key("dsk_002"), store.get_key("dsk_002"), return_exceptions=True
        )

        self.assertTrue(all(not isinstance(r, Exception) for r in results))
        # One call for initial_load, exactly one more shared by both waiters.
        self.assertEqual(self.http_client.get.call_count, 2)

    async def test_background_refresh_backs_off_on_repeated_failure(self) -> None:
        self.http_client.get.side_effect = [
            make_jwks_response([VALID_KEY]),
            make_jwks_response([], status_code=500),
        ]
        store = make_store(self.http_client)
        await store.initial_load()

        store.start_background_refresh(refresh_interval_seconds=0)
        try:
            # Give the loop a couple of ticks to attempt (and fail) a refresh.
            await asyncio.sleep(0.05)
        finally:
            await store.stop_background_refresh()

        # The failed background refresh must not have clobbered the cache.
        key = await store.get_key("dsk_001")
        self.assertIsNotNone(key)

    async def test_start_background_refresh_is_idempotent(self) -> None:
        self.http_client.get.return_value = make_jwks_response([VALID_KEY])
        store = make_store(self.http_client)
        await store.initial_load()

        store.start_background_refresh(refresh_interval_seconds=60)
        first_task = store._background_task
        store.start_background_refresh(refresh_interval_seconds=60)

        self.assertIs(store._background_task, first_task)
        await store.stop_background_refresh()

    async def test_a_5xx_response_raises_refresh_error(self) -> None:
        self.http_client.get.return_value = make_jwks_response([], status_code=500)
        response = self.http_client.get.return_value
        response.raise_for_status.side_effect = Exception("boom")
        store = make_store(self.http_client)

        with self.assertRaises(JwksInitialLoadError):
            await store.initial_load()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
