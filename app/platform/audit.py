"""Tenant-scoped audit events and one redaction contract for storage and logs."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .tenancy import TenantContext, current_context

if TYPE_CHECKING:
    from ..adapters.persistence.models import AuditEventRecord


REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "appsecret",
    "authorization",
    "card_number",
    "cookie",
    "credential",
    "cvv",
    "jwt",
    "password",
    "payment_method",
    "private_key",
    "secret",
    "signature",
    "signed_url",
    "token",
)
_SENSITIVE_KEYS = {"sig"}
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def _sensitive_key(key: object) -> bool:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip())
    normalized = snake_case.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    )


def _redact_string(value: str) -> str:
    safe = _BEARER.sub(f"Bearer {REDACTED}", value)
    safe = _JWT.sub(REDACTED, safe)
    looks_like_url = "://" in safe or safe.startswith(("/", "?", "./", "../"))
    if not looks_like_url:
        return safe
    try:
        parts = urlsplit(safe)
        netloc = parts.netloc
        if parts.netloc:
            hostname = parts.hostname
            if not hostname:
                return safe
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            netloc = hostname
            if parts.port is not None:
                netloc = f"{netloc}:{parts.port}"
        query = [
            (key, REDACTED if _sensitive_key(key) else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (
                parts.scheme,
                netloc,
                parts.path,
                urlencode(query),
                REDACTED if parts.fragment else "",
            )
        )
    except ValueError:
        return safe


def redact(value: Any) -> Any:
    """Return a JSON-safe copy with credentials and payment identifiers removed."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def audit_log_context(
    context: TenantContext,
    *,
    event_type: str,
    resource_type: str,
    resource_id: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The structured fields safe to give both a logger and persistent audit."""
    return {
        **context.audit_actors(),
        "role": context.role,
        "authorization_epoch": context.authorization_epoch,
        "event_type": event_type,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": redact(details or {}),
    }


def append_event(
    session,
    *,
    event_type: str,
    resource_type: str,
    resource_id: str = "",
    details: dict[str, Any] | None = None,
) -> "AuditEventRecord":
    """Append an event to the same transaction as the domain mutation."""
    from ..adapters.persistence.models import AuditEventRecord, new_id

    context = current_context(session)
    fields = audit_log_context(
        context,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )
    event = AuditEventRecord(
        id=new_id("audit"),
        workspace_id=context.workspace_id,
        initiator_id=context.initiator_id,
        initiator_role=context.audit_initiator_role,
        executor=context.executor,
        request_id=context.request_id,
        trace_id=context.trace_id,
        authorization_epoch=context.authorization_epoch,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        details_json=fields["details"],
    )
    session.add(event)
    return event


def record_event(
    context: TenantContext,
    *,
    event_type: str,
    resource_type: str,
    resource_id: str = "",
    details: dict[str, Any] | None = None,
) -> str:
    """Append a standalone event when no command transaction is already open."""
    from ..adapters.persistence.database import tenant_session

    with tenant_session(context) as session:
        event = append_event(
            session,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )
        session.flush()
        return event.id
