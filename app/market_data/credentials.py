"""Market-data credentials: the ``market_data`` binding scope and its key checks.

A Workspace's data-vendor key lives in the same CredentialVersion lineage,
encryption and Owner/MFA rules as its AI keys; what differs is the binding
scope (``market_data``, never ``generation``) and that a key is checked
before it is stored. The check runs on the plaintext the Owner just sent:
the API role cannot read a stored key back (``deploy/postgres/
domain-tenant-roles.sql``), so "save" is the one moment a check is possible
without a Worker. A key that is invalid or has no credits is refused and
nothing is stored; a check that could not be made stores nothing either.

Each check that let a key through is append-only evidence on that version
(an audit event), and the Settings page reads the latest one back. A key is
"connected" only while it is bound to ``market_data`` *and* its latest
check found credits.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import AuditEventRecord
from app.entitlements.credentials import CredentialBindingRecord, CredentialVersionRecord
from app.market_data.scrapecreators_account import CreditBalanceCheck, check_credit_balance
from app.platform.audit import append_event
from app.platform.tenancy import TenantContext

logger = logging.getLogger(__name__)

MARKET_DATA_BINDING_SCOPE = "market_data"
SCRAPECREATORS = "scrapecreators"
MARKET_DATA_PROVIDERS = frozenset({SCRAPECREATORS})

#: One event per key check that let a key through (``resource_id`` is the
#: credential version) or refused it (``resource_id`` is the credential key,
#: since nothing was stored).
CHECK_EVENT = "market_data.credential_checked"


class MarketDataCredentialRefused(Exception):
    """A market-data key was not stored; ``code`` is what the interface renders."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def is_market_data_provider(provider: str) -> bool:
    return provider in MARKET_DATA_PROVIDERS


def binding_scope_mismatch(binding_scope: str, provider: str) -> str | None:
    """Why ``provider``'s key cannot serve ``binding_scope``, if it cannot.

    A data-vendor key pays for scans, never for generation, and the
    ``market_data`` scope takes nothing else; other scopes keep their own
    rules.
    """
    if binding_scope == MARKET_DATA_BINDING_SCOPE and provider not in MARKET_DATA_PROVIDERS:
        return f"binding scope {binding_scope!r} accepts market-data providers only, not {provider!r}"
    if provider in MARKET_DATA_PROVIDERS and binding_scope != MARKET_DATA_BINDING_SCOPE:
        return f"{provider!r} keys can only be bound to {MARKET_DATA_BINDING_SCOPE!r}, not {binding_scope!r}"
    return None


async def check_key(provider: str, plaintext: bytes) -> CreditBalanceCheck:
    """Run ``provider``'s free key check on a key that has not been stored yet."""
    if provider == SCRAPECREATORS:
        check = await check_credit_balance(plaintext)
    else:  # pragma: no cover - MARKET_DATA_PROVIDERS has one member
        raise ValueError(f"no key check for market-data provider {provider!r}")
    logger.info(
        "market-data key check: provider=%s outcome=%s http_status=%s elapsed_ms=%s diagnostic=%s",
        provider, check.outcome, check.http_status, check.elapsed_ms, check.diagnostic,
    )
    return check


def refusal(check: CreditBalanceCheck) -> MarketDataCredentialRefused | None:
    """The refusal a failed check answers with, or ``None`` when the key may be stored."""
    if check.outcome == "valid":
        return None
    if check.outcome == "invalid_or_no_balance":
        return MarketDataCredentialRefused(
            422, "market_data_credential_invalid",
            f"the key is invalid or has no credits left ({check.diagnostic}); nothing was stored",
        )
    return MarketDataCredentialRefused(
        503, "market_data_credential_check_unavailable",
        f"the key could not be checked ({check.diagnostic}); nothing was stored",
    )


def _details(provider: str, check: CreditBalanceCheck) -> dict[str, Any]:
    return {
        "provider": provider,
        "binding_scope": MARKET_DATA_BINDING_SCOPE,
        "outcome": check.outcome,
        "credit_count": check.credit_count,
        "http_status": check.http_status,
        "elapsed_ms": check.elapsed_ms,
        "diagnostic": check.diagnostic,
    }


def record_check(
    session: Session,
    *,
    provider: str,
    check: CreditBalanceCheck,
    credential_version_id: str | None = None,
    credential_key: str | None = None,
) -> None:
    """Append the evidence of one key check to the current transaction.

    A stored key's check names its version; a refused key's check names the
    lineage it was meant for, since no version exists.
    """
    if credential_version_id is not None:
        resource_type, resource_id = "credential_version", credential_version_id
    else:
        resource_type, resource_id = "credential_key", credential_key or ""
    append_event(
        session,
        event_type=CHECK_EVENT,
        resource_type=resource_type,
        resource_id=resource_id,
        details=_details(provider, check),
    )


def latest_checks(
    session: Session, ctx: TenantContext, credential_version_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """The newest check of each version: ``{outcome, credit_count, checked_at}``."""
    ids = sorted(set(credential_version_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(
            AuditEventRecord.resource_id,
            AuditEventRecord.details_json,
            AuditEventRecord.created_at,
        )
        .where(
            AuditEventRecord.workspace_id == ctx.workspace_id,
            AuditEventRecord.event_type == CHECK_EVENT,
            AuditEventRecord.resource_type == "credential_version",
            AuditEventRecord.resource_id.in_(ids),
        )
        .order_by(AuditEventRecord.created_at.desc(), AuditEventRecord.id.desc())
    ).all()
    latest: dict[str, dict[str, Any]] = {}
    for version_id, details, created_at in rows:
        if version_id in latest:
            continue
        details = details or {}
        latest[version_id] = {
            "outcome": details.get("outcome"),
            "credit_count": details.get("credit_count"),
            "checked_at": created_at.isoformat(),
        }
    return latest


def resolve_market_data_credential(
    session: Session, ctx: TenantContext, provider: str,
) -> dict:
    """``{"active": True, "provider": ..., "version_id": ...}`` when an active
    key is bound to ``market_data`` for ``provider``; ``{}`` otherwise.

    The same shape :func:`app.generation.credential_binding.resolve_generation_credential`
    answers for generation, read the same column-level way.
    """
    row = session.execute(
        select(
            CredentialBindingRecord.credential_version_id,
            CredentialVersionRecord.state,
        )
        .join(
            CredentialVersionRecord,
            (CredentialBindingRecord.credential_version_id == CredentialVersionRecord.id)
            & (CredentialBindingRecord.workspace_id == CredentialVersionRecord.workspace_id),
        )
        .where(
            CredentialBindingRecord.workspace_id == ctx.workspace_id,
            CredentialBindingRecord.binding_scope == MARKET_DATA_BINDING_SCOPE,
            CredentialBindingRecord.provider == provider,
        )
    ).first()
    if row is None or row[1] != "active":
        return {}
    return {"active": True, "provider": provider, "version_id": row[0]}
