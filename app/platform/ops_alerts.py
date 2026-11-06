"""Structured operational alerts for Railway log-based alerting.

Emits JSON-structured log lines that Railway alert rules can match on.
Every alert carries bounded identifiers only — never tokens, credentials,
or raw provider payloads.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("ops.alerts")

_SECRET_PATTERNS = re.compile(
    r"(token|secret|password|credential|api_key|access_key|signing_key)"
    r"|"
    r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)"
    r"|"
    r"(EAA[A-Za-z0-9]+)",  # Meta access-token prefix
    re.IGNORECASE,
)

_MAX_DETAIL_LENGTH = 500


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCategory(str, Enum):
    RECONCILIATION = "reconciliation"
    UNCERTAIN_STATE = "uncertain_state"
    KEY_ROTATION = "key_rotation"
    RETENTION = "retention"
    BUDGET = "budget"
    SHUTDOWN = "shutdown"
    WORKER_LIFECYCLE = "worker_lifecycle"


def redact(value: str) -> str:
    """Strip tokens, credentials, and unbounded identifiers from a string."""
    if not value:
        return value
    result = _SECRET_PATTERNS.sub("[REDACTED]", value)
    if len(result) > _MAX_DETAIL_LENGTH:
        result = result[:_MAX_DETAIL_LENGTH] + "...[truncated]"
    return result


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact secret-shaped values from a dict."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if _SECRET_PATTERNS.search(key):
            cleaned[key] = "[REDACTED]"
        elif isinstance(value, str):
            cleaned[key] = redact(value)
        elif isinstance(value, dict):
            cleaned[key] = redact_dict(value)
        else:
            cleaned[key] = value
    return cleaned


def emit(
    severity: Severity,
    category: AlertCategory,
    summary: str,
    *,
    workspace_id: str = "",
    job_id: str = "",
    operation_id: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit a structured operational alert as a JSON log line.

    Returns the emitted payload for testability.
    """
    payload: dict[str, Any] = {
        "alert": True,
        "severity": severity.value,
        "category": category.value,
        "summary": redact(summary),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if workspace_id:
        payload["workspace_id"] = workspace_id
    if job_id:
        payload["job_id"] = job_id
    if operation_id:
        payload["operation_id"] = operation_id
    if details:
        payload["details"] = redact_dict(details)

    log_method = {
        Severity.INFO: logger.info,
        Severity.WARNING: logger.warning,
        Severity.CRITICAL: logger.critical,
    }[severity]
    log_method(json.dumps(payload, ensure_ascii=False))
    return payload
