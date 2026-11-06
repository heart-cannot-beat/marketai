"""Authoritative recovery for Creative Library command outcomes.

The browser supplies one opaque identity for a logical Library command.  The
domain stores only its SHA-256 hash, alongside the existing idempotency key.
Outcome lookup is therefore partitioned by Workspace and initiating actor,
while the raw identity is echoed only from the current request.

Reservations are committed before command work begins.  Command effects and
terminal outcome finalization commit in one later transaction.  Reconciliation
locks the reservation first: if it is still pending after that lock is
acquired, no command effect can have committed and ``not_applied`` is an
affirmative database proof rather than an inference from a missing row.
"""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryIdempotencyRecord, utcnow
from app.platform.tenancy import TenantContext


COMMAND_OUTCOME_CONTRACT_VERSION = 2
COMMAND_IDENTITY_MIN_LENGTH = 8
COMMAND_IDENTITY_MAX_LENGTH = 128
COMMAND_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class InvalidCommandIdentity(ValueError):
    """The opaque command identity is not safe to bind or place in a URL."""


class CommandOutcomeAccessDenied(RuntimeError):
    """A matching identity belongs to a different initiator."""


class CommandOutcomeBindingMismatch(RuntimeError):
    """A matching identity is bound to another command kind."""

    def __init__(self, existing_command: str, attempted_command: str) -> None:
        super().__init__("command identity is bound to a different command kind")
        self.existing_command = existing_command
        self.attempted_command = attempted_command


def validate_command_identity(value: str) -> str:
    """Validate without normalizing: the exact opaque value defines identity."""
    if not isinstance(value, str) or not COMMAND_IDENTITY_PATTERN.fullmatch(value):
        raise InvalidCommandIdentity(
            "command identity must be 8-128 URL-safe ASCII characters"
        )
    return value


def hash_command_identity(value: str, *, validate: bool = True) -> str:
    identity = validate_command_identity(value) if validate else value
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _record_for_identity(
    session,
    context: TenantContext,
    identity_hash: str,
) -> LibraryIdempotencyRecord | None:
    return session.scalars(
        select(LibraryIdempotencyRecord).where(
            LibraryIdempotencyRecord.workspace_id == context.workspace_id,
            LibraryIdempotencyRecord.command_identity_hash == identity_hash,
        )
    ).one_or_none()


def _server_times(
    record: LibraryIdempotencyRecord | None,
    observed_at: datetime,
) -> dict[str, datetime | None]:
    return {
        "created_at": record.created_at if record is not None else observed_at,
        "updated_at": (
            (record.updated_at or record.created_at)
            if record is not None
            else observed_at
        ),
        "reconciled_at": record.reconciled_at if record is not None else None,
        "observed_at": observed_at,
    }


def _problem_from_response(response: dict[str, Any]) -> dict[str, Any]:
    terminal = response.get("_terminal")
    mapping = {
        "oversized": (413, "file_too_large"),
        "not_found": (404, "library_not_found"),
        "revision_conflict": (409, "revision_conflict"),
        "conflict": (409, "library_conflict"),
        "validation": (422, "library_validation_failed"),
        "forbidden": (403, "library_forbidden"),
        "gate_error": (422, "reference_gate_failed"),
        "invalid": (400, "library_invalid"),
    }
    failure_code = "library_command_failed"
    status, code = mapping.get(str(terminal), (500, failure_code))
    # The exact code the command failed with (#253); older records have none.
    code = str(response.get("_code") or code)
    problem: dict[str, Any] = {
        "type": f"urn:marketai:error:{code.replace('_', '-')}",
        "title": code,
        "status": status,
        "detail": str(response.get("_message") or code.replace("_", " ")),
        "instance": "",
        "code": code,
        "trace_id": "",
        "retryable": False,
        "field_errors": [],
    }
    if isinstance(response.get("_params"), dict) and response["_params"]:
        problem["params"] = deepcopy(response["_params"])
    if isinstance(response.get("_detail"), dict):
        problem["conflict_info"] = deepcopy(response["_detail"])
    if isinstance(response.get("_report"), dict):
        problem["report"] = deepcopy(response["_report"])
    if terminal == "oversized":
        problem["limit"] = int(response.get("_limit") or 0)
    return problem


def _result_reference(command_kind: str, result: dict[str, Any]) -> dict[str, str] | None:
    operation_id = result.get("operation_id")
    if isinstance(operation_id, str) and operation_id:
        return {"resource_type": "operation", "resource_id": operation_id}

    if command_kind == "upload_reference":
        object_id = result.get("object_id")
        if isinstance(object_id, str) and object_id:
            return {"resource_type": "reference_object", "resource_id": object_id}

    resource_id = result.get("id")
    if isinstance(resource_id, str) and resource_id:
        resource_type = {
            "publish": "library_version",
            "bind": "library_draft",
        }.get(command_kind, "library_entry")
        return {"resource_type": resource_type, "resource_id": resource_id}

    version_id = result.get("version_id")
    if isinstance(version_id, str) and version_id:
        return {"resource_type": "library_version", "resource_id": version_id}
    return None


def project_outcome(
    record: LibraryIdempotencyRecord,
    command_identity: str,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the public discriminated outcome without exposing stored hashes."""
    observed = observed_at or utcnow()
    common: dict[str, Any] = {
        "contract_schema_version": record.contract_schema_version,
        "command_kind": record.command,
        "command_identity": command_identity,
        **_server_times(record, observed),
    }
    response = deepcopy(dict(record.response_json or {}))

    if record.lifecycle_state == "pending" or record.response_kind == "pending":
        return {"outcome": "pending", **common}
    if record.response_kind == "not_applied":
        return {"outcome": "not_applied", **common}
    if record.response_kind == "problem":
        return {
            "outcome": "problem",
            **common,
            "problem": _problem_from_response(response),
        }
    return {
        "outcome": "succeeded",
        **common,
        "result": response,
        "result_reference": _result_reference(record.command, response),
    }


def _missing_outcome(
    command_identity: str,
    command_kind: str | None,
    observed_at: datetime,
) -> dict[str, Any]:
    return {
        "outcome": "reconciliation_required",
        "contract_schema_version": COMMAND_OUTCOME_CONTRACT_VERSION,
        "command_kind": command_kind or "unknown",
        "command_identity": command_identity,
        **_server_times(None, observed_at),
        "reason_code": "command_outcome_not_found",
    }


def _validate_access_and_binding(
    record: LibraryIdempotencyRecord,
    context: TenantContext,
    expected_command_kind: str | None,
) -> None:
    if record.initiator_id != (context.initiator_id or ""):
        raise CommandOutcomeAccessDenied()
    if expected_command_kind and record.command != expected_command_kind:
        raise CommandOutcomeBindingMismatch(record.command, expected_command_kind)


def get_outcome(
    context: TenantContext,
    command_identity: str,
    *,
    expected_command_kind: str | None = None,
) -> dict[str, Any]:
    identity = validate_command_identity(command_identity)
    identity_hash = hash_command_identity(identity, validate=False)
    observed_at = utcnow()
    with tenant_session(context) as session:
        record = _record_for_identity(session, context, identity_hash)
        if record is None:
            return _missing_outcome(identity, expected_command_kind, observed_at)
        _validate_access_and_binding(record, context, expected_command_kind)
        return project_outcome(record, identity, observed_at=observed_at)


def reconcile_outcome(
    context: TenantContext,
    command_identity: str,
    *,
    expected_command_kind: str | None = None,
) -> dict[str, Any]:
    """Lock and reconcile one reservation into a definitive public outcome."""
    identity = validate_command_identity(command_identity)
    identity_hash = hash_command_identity(identity, validate=False)
    observed_at = utcnow()
    with tenant_session(context) as session:
        # A no-op UPDATE obtains PostgreSQL's row lock and SQLite's writer
        # lock before the read.  An in-flight command must commit or roll back
        # first, so a pending row observed afterwards proves no effects
        # committed from that command transaction.
        session.execute(
            update(LibraryIdempotencyRecord)
            .where(
                LibraryIdempotencyRecord.workspace_id == context.workspace_id,
                LibraryIdempotencyRecord.command_identity_hash == identity_hash,
            )
            .values(updated_at=LibraryIdempotencyRecord.updated_at)
        )
        record = _record_for_identity(session, context, identity_hash)
        if record is None:
            return _missing_outcome(identity, expected_command_kind, observed_at)
        _validate_access_and_binding(record, context, expected_command_kind)

        record.reconciled_at = observed_at
        if record.lifecycle_state == "pending" or record.response_kind == "pending":
            record.lifecycle_state = "terminal"
            record.response_kind = "not_applied"
            record.response_json = {"_state": "not_applied"}
            record.updated_at = observed_at
        session.flush()
        return project_outcome(record, identity, observed_at=observed_at)
