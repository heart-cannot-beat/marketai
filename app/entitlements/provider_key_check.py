"""Is this AI provider key accepted, asked before it is saved (#192, ads-first S5).

OpenAI only, for now: ``GET {OPENAI_BASE_URL}/models`` lists the models a key can see. It calls no model, uses no
tokens and makes no image, so it is not a billed request. Its answer tells an accepted key from a refused one; it
cannot tell whether the key's organization may generate images (OpenAI checks organization verification on the
image request itself, which is billed), so such a key is accepted here and fails on its first image.

- ``200`` with a model list: ``valid``.
- ``401``: ``rejected`` / ``key_invalid`` (wrong or revoked key, no organization, IP not allowed).
- ``403``: ``rejected`` / ``access_denied`` (for example an unsupported country or region).
- ``429`` with a no-credit or spend-limit ``error.code``: ``rejected`` / ``quota_exhausted``.
- Anything else (timeout, network error, rate limit, 5xx, an unreadable body): ``unavailable``, judged neither way.

The key goes in the request header only. The check, its log line and its audit evidence keep the status and
OpenAI's ``error.code``, never the key or OpenAI's error text (a ``401`` text repeats part of the key).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app import config
from app.platform.audit import append_event

logger = logging.getLogger(__name__)

#: One event per check: on the stored version when the key was saved, on the credential key when it was not.
CHECK_EVENT = "credential.provider_key_checked"
CHECKED_PROVIDERS = frozenset({"openai"})
TIMEOUT_SECONDS = 10
QUOTA_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                         "project_spend_limit_exceeded", "organization_usage_limit_exceeded"})
_SAFE_CODE = re.compile(r"[a-z0-9_]{1,64}")

Outcome = Literal["valid", "rejected", "unavailable"]


@dataclass(frozen=True)
class ProviderKeyCheck:
    provider: str
    outcome: Outcome
    #: ``key_invalid``, ``access_denied`` or ``quota_exhausted`` when rejected; otherwise ``None``.
    reason: str | None
    http_status: int | None
    elapsed_ms: int
    #: English, for logs and evidence; never shown in the interface.
    diagnostic: str


def checks_key(provider: str) -> bool:
    return provider in CHECKED_PROVIDERS


async def check_key(provider: str, plaintext: bytes, *,
                    transport: httpx.AsyncBaseTransport | None = None) -> ProviderKeyCheck:
    """Ask ``provider`` whether it accepts a key that has not been stored yet."""
    if provider != "openai":
        raise ValueError(f"no key check for provider {provider!r}")
    check = await _check_openai(plaintext, transport)
    logger.info("provider key check: provider=%s outcome=%s reason=%s http_status=%s elapsed_ms=%s diagnostic=%s",
                check.provider, check.outcome, check.reason, check.http_status, check.elapsed_ms, check.diagnostic)
    return check


def _result(outcome: Outcome, started: float, *, reason: str | None = None, status: int | None = None,
            diagnostic: str = "") -> ProviderKeyCheck:
    return ProviderKeyCheck("openai", outcome, reason, status, int((time.monotonic() - started) * 1000), diagnostic)


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else None


async def _check_openai(plaintext: bytes, transport: httpx.AsyncBaseTransport | None) -> ProviderKeyCheck:
    started = time.monotonic()
    key = plaintext.decode("utf-8", errors="replace").strip()
    # A header carries visible ASCII only; such a key could never authenticate, so it is not sent at all.
    if not key or any(not 0x21 <= ord(ch) <= 0x7E for ch in key):
        return _result("rejected", started, reason="key_invalid",
                       diagnostic="key has characters a request header cannot carry")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False, follow_redirects=False,
                                     transport=transport) as client:
            response = await client.get(f"{config.OPENAI_BASE_URL}/models",
                                        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except httpx.TimeoutException:
        return _result("unavailable", started, diagnostic="models request timed out")
    except httpx.HTTPError as exc:
        return _result("unavailable", started, diagnostic=f"models request failed ({type(exc).__name__})")

    status = response.status_code
    code = _error_code(response) if status != 200 else None
    diagnostic = f"models answered {status}" + (f" ({code})" if code else "")
    if status == 200:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("object") == "list" and isinstance(body.get("data"), list):
            return _result("valid", started, status=status, diagnostic="models answered a model list")
        return _result("unavailable", started, status=status, diagnostic="models body is not a model list")
    if status == 401:
        return _result("rejected", started, reason="key_invalid", status=status, diagnostic=diagnostic)
    if status == 403:
        return _result("rejected", started, reason="access_denied", status=status, diagnostic=diagnostic)
    if status == 429 and code in QUOTA_CODES:
        return _result("rejected", started, reason="quota_exhausted", status=status, diagnostic=diagnostic)
    return _result("unavailable", started, status=status, diagnostic=diagnostic)


def _details(check: ProviderKeyCheck) -> dict[str, Any]:
    return {"provider": check.provider, "outcome": check.outcome, "reason": check.reason,
            "http_status": check.http_status, "elapsed_ms": check.elapsed_ms, "diagnostic": check.diagnostic}


def record_check(session, check: ProviderKeyCheck, *, credential_version_id: str | None = None,
                 credential_key: str | None = None) -> None:
    """Append the evidence of one key check to the current transaction."""
    if credential_version_id is not None:
        resource_type, resource_id = "credential_version", credential_version_id
    else:
        resource_type, resource_id = "credential_key", credential_key or ""
    append_event(session, event_type=CHECK_EVENT, resource_type=resource_type, resource_id=resource_id,
                 details=_details(check))
