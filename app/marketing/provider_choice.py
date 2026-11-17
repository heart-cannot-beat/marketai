"""Which provider, model and per-call ceiling a paid planning or vision-check call uses (#192, ads-first S5).

The deployment names one provider (``{prefix}_PROVIDER``, Gemini by default). When this Workspace has no key bound
for it but has an OpenAI key, the call uses OpenAI instead, so a single OpenAI key runs the whole flow. Workers need
no change: they read the provider frozen on the call and resolve that provider's key.

Settings, read at call time:

- ``{prefix}_MODELS_JSON``: ``{"openai": {"model": "...", "max_cost_usd": "0.10"}, "gemini": {...}}``.
- ``{prefix}_MODEL`` and ``{prefix}_MAX_COST_USD``: the deployment provider's model and ceiling, as before. When
  either is set they describe that provider and its table entry is not read, so a deployment that only sets them
  answers exactly as it did before the table existed.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.generation.selection import credential_binding

logger = logging.getLogger(__name__)

SUPPORTED = ("gemini", "openai")
FALLBACK = "openai"
MAX_CEILING = Decimal("10")


@dataclass(frozen=True)
class ProviderChoice:
    provider: str
    model: str
    #: The ceiling as configured; reported as-is, like the old setting.
    maximum: str
    #: The parsed ceiling; ``None`` when this provider's settings are unusable.
    amount: Decimal | None
    #: ``None``, ``"credential_required"`` or ``"not_configured"``.
    reason: str | None


def _ceiling(raw) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        amount = Decimal(raw.strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and Decimal("0") < amount <= MAX_CEILING else None


def _table(prefix: str) -> dict:
    raw = os.getenv(f"{prefix}_MODELS_JSON", "").strip()
    if not raw:
        return {}
    try:
        table = json.loads(raw)
    except ValueError:
        table = None
    if not isinstance(table, dict):
        logger.warning("%s_MODELS_JSON is not a JSON object; no provider is configured from it", prefix)
        return {}
    return table


def _entry(provider: str, raw) -> ProviderChoice:
    """One provider's settings; ``reason`` is ``not_configured`` unless they are usable."""
    model, maximum = (raw.get("model"), raw.get("max_cost_usd")) if isinstance(raw, dict) else ("", "")
    model = model.strip() if isinstance(model, str) else ""
    maximum = maximum.strip() if isinstance(maximum, str) else ""
    amount = _ceiling(maximum) if provider in SUPPORTED and model else None
    return ProviderChoice(provider, model, maximum, amount, None if amount is not None else "not_configured")


def configured(prefix: str) -> tuple[str, dict[str, ProviderChoice]]:
    """The deployment provider and the settings of every provider this call could use, deployment provider first."""
    provider = os.getenv(f"{prefix}_PROVIDER", "gemini").strip()
    table = _table(prefix)
    legacy_model = os.getenv(f"{prefix}_MODEL", "").strip()
    legacy_maximum = os.getenv(f"{prefix}_MAX_COST_USD", "").strip()
    if legacy_model or legacy_maximum:
        entries = {provider: _entry(provider, {"model": legacy_model, "max_cost_usd": legacy_maximum})}
    else:
        entries = {provider: _entry(provider, table.get(provider))}
    if provider != FALLBACK:
        entries[FALLBACK] = _entry(FALLBACK, table.get(FALLBACK))
    return provider, entries


def choose(session, ctx, prefix: str, *, assuming: tuple[str, ...] = ()) -> ProviderChoice:
    """The deployment provider when this Workspace has its key, else OpenAI when it has that key.

    Unavailable, it names the first provider with usable settings (``credential_required``), or the deployment
    provider when neither has any (``not_configured``). Entitlement and budget checks stay with the caller.
    ``assuming`` names providers taken to have a key already: a quote prices the call a Workspace could make
    once it saves that key (#216). Nothing is admitted on an assumed key.
    """
    provider, entries = configured(prefix)
    usable = [entry for entry in entries.values() if entry.reason is None]
    for entry in usable:
        if entry.provider in assuming or credential_binding(session, ctx, entry.provider):
            return entry
    if usable:
        first = usable[0]
        return ProviderChoice(first.provider, first.model, first.maximum, first.amount, "credential_required")
    return entries[provider]
