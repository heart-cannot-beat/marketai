"""Async JWKS cache for verifying delegated BFF identity tokens (spec §3.2).

`JwksKeyStore` owns the cache, the HTTP client, and the ETag-based conditional
refresh logic. It deliberately does not wrap ``jwt.PyJWKClient`` -- that class
performs its own blocking HTTP and has cache/refresh semantics that do not
match the hard-stale and single-flight requirements here.

Three properties keep verification correct under load:

* **Hard-stale.** A cache older than ``max_stale_seconds`` is never trusted,
  even if it still has a matching ``kid``. Rather than serve a possibly
  revoked key indefinitely on a control-plane outage, every request fails
  closed once the cache is stale enough.
* **Single-flighted refresh.** Concurrent verification of tokens with an
  unknown ``kid`` shares one HTTP request instead of stampeding the JWKS
  endpoint. Every waiter uses ``asyncio.shield`` so a cancelled request
  cannot cancel the refresh other requests are waiting on.
* **Empty JWKS is always a failure.** A parsed response with zero valid
  signing keys never replaces the installed cache -- a control-plane bug
  that publishes an empty key set must not lock every request out.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import jwt

logger = logging.getLogger(__name__)


class JwksRefreshError(Exception):
    """The remote response cannot safely refresh the installed key set."""


class JwksNotYetLoadedError(Exception):
    """No successful refresh has ever completed."""


class JwksStaleError(Exception):
    """The installed cache is older than ``max_stale_seconds``."""

    def __init__(self, age_seconds: float, max_stale_seconds: int) -> None:
        self.age_seconds = age_seconds
        self.max_stale_seconds = max_stale_seconds
        super().__init__(
            f"JWKS cache is {age_seconds:.1f}s old, exceeding the "
            f"{max_stale_seconds}s hard-stale limit"
        )


class JwksInitialLoadError(Exception):
    """The very first JWKS load at process startup failed."""


class RevokedKidError(Exception):
    """The token's ``kid`` has been explicitly revoked."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        super().__init__(f"Key {kid!r} has been revoked")


class UnknownKidError(Exception):
    """The token's ``kid`` is not present in the (freshly refreshed) cache."""


@dataclass
class CachedKey:
    kid: str
    jwk: "jwt.PyJWK"
    algorithm: str


class JwksKeyStore:
    def __init__(
        self,
        *,
        jwks_url: str,
        max_stale_seconds: int,
        http_client: httpx.AsyncClient,
        revoked_kids: frozenset[str] | None = None,
    ) -> None:
        self._jwks_url = jwks_url
        self._max_stale_seconds = max_stale_seconds
        self._http_client = http_client
        self._revoked_kids: frozenset[str] = revoked_kids or frozenset()
        self._keys: dict[str, CachedKey] = {}
        self._etag: str | None = None
        self._last_successful_refresh_at: datetime | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._background_task: asyncio.Task[None] | None = None

    @property
    def last_successful_refresh_at(self) -> datetime | None:
        return self._last_successful_refresh_at

    def is_fresh(self) -> bool:
        """Non-raising freshness check for readiness probes (spec §3.8)."""
        if self._last_successful_refresh_at is None:
            return False
        age = (datetime.now(timezone.utc) - self._last_successful_refresh_at).total_seconds()
        return age <= self._max_stale_seconds

    async def get_key(self, kid: str) -> "jwt.PyJWK":
        if kid in self._revoked_kids:
            raise RevokedKidError(kid)

        self._check_hard_stale()

        cached = self._keys.get(kid)
        if cached is not None:
            return cached.jwk

        await self._single_flighted_refresh()

        cached = self._keys.get(kid)
        if cached is not None:
            return cached.jwk

        raise UnknownKidError(kid)

    def revoke_kid(self, kid: str) -> None:
        """Add a key ID to the runtime denylist (emergency revocation)."""
        self._revoked_kids = self._revoked_kids | frozenset({kid})

    def _check_hard_stale(self) -> None:
        if self._last_successful_refresh_at is None:
            raise JwksNotYetLoadedError()
        age = (datetime.now(timezone.utc) - self._last_successful_refresh_at).total_seconds()
        if age > self._max_stale_seconds:
            raise JwksStaleError(age, self._max_stale_seconds)

    async def _single_flighted_refresh(self) -> None:
        existing = self._refresh_task
        if existing is not None:
            await asyncio.shield(existing)
            return

        task = asyncio.get_running_loop().create_task(self._do_refresh())
        self._refresh_task = task
        task.add_done_callback(self._clear_refresh_task)
        # A cancelled request waiter must not cancel the process-wide refresh
        # or clear its shared reference while it is still running.
        await asyncio.shield(task)

    def _clear_refresh_task(self, task: asyncio.Task[None]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None

    async def _do_refresh(self) -> None:
        headers: dict[str, str] = {}
        if self._etag is not None:
            headers["If-None-Match"] = self._etag

        response = await self._http_client.get(self._jwks_url, headers=headers)

        now = datetime.now(timezone.utc)

        if response.status_code == 304:
            # A 304 is meaningful only after a successful non-empty load for
            # the ETag sent in this request. A stray first-start 304 must not
            # turn an empty cache into a healthy cache.
            if self._etag is None or len(self._keys) == 0:
                raise JwksRefreshError("304 received without a populated ETag cache")
            self._last_successful_refresh_at = now
            return

        response.raise_for_status()

        data = response.json()
        new_keys: dict[str, CachedKey] = {}
        for key_data in data.get("keys", []):
            kid = key_data.get("kid")
            if (
                not isinstance(kid, str)
                or not kid
                or key_data.get("kty") != "EC"
                or key_data.get("crv") != "P-256"
                or key_data.get("alg") != "ES256"
                or key_data.get("use") != "sig"
            ):
                continue
            if kid in new_keys:
                raise JwksRefreshError("JWKS response contains duplicate kid")
            try:
                jwk = jwt.PyJWK.from_dict(key_data)
            except Exception:
                continue  # skip unparseable individual keys
            new_keys[kid] = CachedKey(kid=kid, jwk=jwk, algorithm="ES256")

        # SAFETY: an empty valid-key set is ALWAYS a refresh failure. Existing
        # keys remain installed because assignment happens only after this
        # check. Raising makes the background loop enter exponential backoff.
        if len(new_keys) == 0:
            raise JwksRefreshError("JWKS response contained no valid signing keys")

        self._keys = new_keys
        self._etag = response.headers.get("ETag")
        self._last_successful_refresh_at = now

    async def initial_load(
        self,
        *,
        attempts: int = 1,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 20.0,
    ) -> None:
        """Populate the cache once at process startup.

        The control plane and this process routinely restart together (a
        redeploy of every service after a billing lapse, a platform
        failover), and for the first minute or two the JWKS host may not
        resolve, may refuse connections, or may answer before its signer is
        bootstrapped. ``attempts`` > 1 retries those transient failures with
        exponential backoff (``initial_backoff_seconds`` doubling up to
        ``max_backoff_seconds``) instead of failing the process on the first
        miss. The budget is bounded so a permanently wrong ``jwks_url`` still
        fails startup, loudly, with the last error chained as the cause.
        """
        if attempts < 1:
            raise ValueError("attempts must be >= 1")

        backoff = max(float(initial_backoff_seconds), 0.0)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self._do_refresh()
            except (httpx.HTTPError, JwksRefreshError, ValueError) as exc:
                # Transport failures, non-2xx responses, an unparseable body
                # and an empty key set are all transient at startup.
                last_error = exc
                if attempt >= attempts:
                    break
                logger.warning(
                    "JWKS initial load attempt %d/%d failed (%s: %s); retrying in %.1fs",
                    attempt,
                    attempts,
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(
                    max(backoff * 2, float(initial_backoff_seconds)),
                    float(max_backoff_seconds),
                )
                continue
            except Exception as exc:
                raise JwksInitialLoadError("JWKS initial load failed") from exc
            last_error = None
            if attempt > 1:
                logger.info("JWKS initial load succeeded on attempt %d/%d", attempt, attempts)
            break

        if last_error is not None:
            raise JwksInitialLoadError(
                f"JWKS initial load failed after {attempts} attempt(s)"
            ) from last_error
        if self._last_successful_refresh_at is None or len(self._keys) == 0:
            raise JwksInitialLoadError("JWKS initial load produced no usable keys")

    def start_background_refresh(self, refresh_interval_seconds: int) -> None:
        """Keep the cache fresh on a timer, independent of request traffic.

        Idempotent: calling this twice does not start a second loop. A
        failing refresh backs off exponentially (capped at 8x the interval)
        rather than hammering a struggling JWKS endpoint; hard-stale still
        kicks in for verification if recovery takes too long, but this loop
        keeps trying regardless.
        """
        if self._background_task is not None:
            return
        self._background_task = asyncio.get_running_loop().create_task(
            self._background_refresh_loop(refresh_interval_seconds)
        )

    async def stop_background_refresh(self) -> None:
        task = self._background_task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._background_task = None

    async def _background_refresh_loop(self, interval_seconds: int) -> None:
        backoff = max(interval_seconds, 1)
        max_backoff = backoff * 8
        while True:
            await asyncio.sleep(backoff)
            try:
                # Shares the same single-flight path unknown-kid lookups use,
                # so a concurrent request-triggered refresh and the timer
                # never issue two overlapping HTTP calls.
                await self._single_flighted_refresh()
                backoff = max(interval_seconds, 1)
            except asyncio.CancelledError:
                raise
            except Exception:
                backoff = min(backoff * 2, max_backoff)
