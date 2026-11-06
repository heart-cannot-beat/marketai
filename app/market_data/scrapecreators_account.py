"""ScrapeCreators' free account endpoint: is this key usable right now?

``GET /v1/account/credit-balance`` with the ``x-api-key`` header costs no
credits. It answers ``200`` even for a key ScrapeCreators does not know --
with ``creditCount: 0`` -- so a key counts as usable only when
``creditCount > 0`` (spec §6); zero means "invalid or no credits left" and
the two cannot be told apart. Anything that is not a readable answer
(timeout, network error, 429, 5xx, a body without a numeric
``creditCount``) is ``unavailable``: the key was not judged either way.

The key is sent in the request header only; it never appears in the
returned check, its diagnostic, or a log line.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import httpx

from app import config

CREDIT_BALANCE_PATH = "/v1/account/credit-balance"

CheckOutcome = Literal["valid", "invalid_or_no_balance", "unavailable"]


@dataclass(frozen=True)
class CreditBalanceCheck:
    outcome: CheckOutcome
    credit_count: int | None
    http_status: int | None
    elapsed_ms: int
    #: English, for logs and evidence; never shown in the interface.
    diagnostic: str

    @property
    def valid(self) -> bool:
        return self.outcome == "valid"


def _result(outcome: CheckOutcome, started: float, *, status: int | None = None,
            credits: int | None = None, diagnostic: str = "") -> CreditBalanceCheck:
    return CreditBalanceCheck(
        outcome=outcome,
        credit_count=credits,
        http_status=status,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        diagnostic=diagnostic,
    )


async def check_credit_balance(api_key: bytes) -> CreditBalanceCheck:
    """Ask ScrapeCreators for ``api_key``'s credit balance (free, read-only)."""
    started = time.monotonic()
    header_value = api_key.decode("utf-8", errors="replace").strip()
    # A header carries visible ASCII only; a key with spaces, line breaks or
    # other characters could never authenticate, so it is not sent at all.
    if not header_value or any(not 0x21 <= ord(ch) <= 0x7E for ch in header_value):
        return _result("invalid_or_no_balance", started,
                       diagnostic="key has characters a request header cannot carry")
    try:
        async with httpx.AsyncClient(
            timeout=config.SCRAPECREATORS_CHECK_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.get(
                f"{config.SCRAPECREATORS_BASE_URL}{CREDIT_BALANCE_PATH}",
                headers={"x-api-key": header_value, "accept": "application/json"},
            )
    except httpx.TimeoutException:
        return _result("unavailable", started, diagnostic="credit-balance request timed out")
    except httpx.HTTPError as exc:
        return _result("unavailable", started,
                       diagnostic=f"credit-balance request failed ({type(exc).__name__})")

    status = response.status_code
    if status in (401, 403):
        return _result("invalid_or_no_balance", started, status=status,
                       diagnostic=f"credit-balance answered {status}")
    if status != 200:
        return _result("unavailable", started, status=status,
                       diagnostic=f"credit-balance answered {status}")
    try:
        body = response.json()
    except ValueError:
        return _result("unavailable", started, status=status, diagnostic="credit-balance body is not JSON")
    count = body.get("creditCount") if isinstance(body, dict) else None
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return _result("unavailable", started, status=status,
                       diagnostic="credit-balance body has no numeric creditCount")
    credits = int(count)
    if credits > 0:
        return _result("valid", started, status=status, credits=credits,
                       diagnostic="creditCount is positive")
    return _result("invalid_or_no_balance", started, status=status, credits=max(credits, 0),
                   diagnostic="creditCount is not positive")
