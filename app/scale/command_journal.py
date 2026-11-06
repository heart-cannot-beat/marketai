"""Atomic command-claim journal for every Scale candidate/enrollment
activation mutation (spec §7.1, issue #86 Task 8).

``claim_command()`` is the *external* Interface: callers never branch on
dialect themselves. Internally it holds two Adapters, because SQLAlchemy
only exposes an ``INSERT ... ON CONFLICT DO NOTHING`` builder per dialect
(``sqlalchemy.dialects.postgresql.insert`` / ``sqlalchemy.dialects.sqlite.
insert``, not a dialect-generic one) -- PostgreSQL is the hosted,
concurrency-tested path; SQLite is this project's bounded local/import/test
path (see ``app/platform/tenancy.py``'s module docstring for the same
two-layer split). PostgreSQL waits when the conflicting row is uncommitted:
if the winning transaction rolls back this insert succeeds, if it commits
this reads that row and compares the fingerprint. No SELECT-then-INSERT
window exists.

Every journaled command preallocates its result id before claiming (the
caller decides the winning candidate/enrollment/preview id up front); the
claim row and the result row commit in the same transaction, so a later
replay finding a missing result is an ``idempotency_result_missing``
invariant failure the caller must detect itself. Lock ordering (spec §7.4):
this claim is always the *first* lock a Scale command takes, before the
lineage guard or any Scale aggregate row -- callers may perform non-locking
lookups to derive ``scenario_scope_key`` first, but must not acquire any row
lock before calling this.

This is the authoritative idempotency check for the five journaled command
types (``create_candidate``, ``create_enrollment``, ``create_preview``,
``activate_enrollment``, ``resume_enrollment``) and runs before any
lifecycle check -- matching the existing ``_existing_enrollment_event``
early-check placement in ``app/scale/enrollment.py`` (see its
``create_draft_enrollment``/``activate_enrollment``, which call it before
locking the Enrollment). ``ScaleEnrollmentEventRecord.deduplication_key``
remains a secondary safety net for those commands; this journal does not
replace it, both are written in the same transaction going forward.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.platform.tenancy import TenantContext, is_postgres
from .models import ScaleCommandJournalRecord, new_id

__all__ = [
    "CommandClaim",
    "CommandOutcome",
    "CommandOutcomeNotFoundError",
    "IdempotencyConflictError",
    "command_request_fingerprint",
    "compute_identity_hash",
    "claim_command",
    "lookup_command_outcome",
]

#: Mirrors the reserved marker used throughout ``app/scale`` (see
#: ``app/scale/enrollment.py``'s ``NO_SCENARIO_MARKER`` and
#: ``app/platform/tenancy.py``'s ``SCENARIO_BICONDITIONAL_SQL``).
NO_SCENARIO_MARKER = "__no_scenario__"

# spec §7.1's `result_resource_type` column is NOT NULL; every command_type
# it allows has exactly one natural result-resource-type, so callers do not
# need to repeat it at every call site (an explicit override is still
# accepted for forward-compatibility).
_DEFAULT_RESULT_RESOURCE_TYPE = {
    "create_candidate": "candidate",
    "create_enrollment": "enrollment",
    "create_preview": "activation_preview",
    "activate_enrollment": "enrollment",
    "resume_enrollment": "enrollment",
}

_CLAIM_CONFLICT_COLUMNS = ("workspace_id", "meta_mode", "command_type", "dedup_key")


class IdempotencyConflictError(Exception):
    """Same ``(workspace, mode, command_type, dedup_key)`` claimed with a
    different ``request_fingerprint`` -- the CC10 pattern (spec §7.7): one
    caller commits, every other caller using the same key with a different
    payload gets this, never a lifecycle error."""

    def __init__(
        self,
        command_type: str,
        dedup_key: str,
        existing_fingerprint: str,
        incoming_fingerprint: str,
    ) -> None:
        self.command_type = command_type
        self.dedup_key = dedup_key
        self.existing_fingerprint = existing_fingerprint
        self.incoming_fingerprint = incoming_fingerprint
        super().__init__(
            "idempotency_conflict: "
            f"{command_type}/{dedup_key} existing={existing_fingerprint!r} "
            f"incoming={incoming_fingerprint!r}"
        )


@dataclass(frozen=True)
class CommandClaim:
    """The winning (``replay=False``) or already-committed (``replay=True``)
    journal row. A ``replay`` claim means the caller must return the
    previously-committed result and must not re-run the command body."""

    record: ScaleCommandJournalRecord
    replay: bool


def command_request_fingerprint(**fields: object) -> str:
    """Return the stable 64-character identity for one command payload.

    Command callers pass semantic fields rather than concatenated resource IDs;
    canonical JSON removes delimiter ambiguity and SHA-256 fits the journal's
    fixed-width PostgreSQL column.
    """
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_identity_hash(command_type: str, dedup_key: str) -> str:
    """Return the server-derived 64-character identity for one
    ``(command_type, dedup_key)`` pair (issue #87 Task 4).

    This is distinct from :func:`command_request_fingerprint`: the
    fingerprint captures a command's *payload* (for CC10 conflict
    detection), while the identity hash captures the *claim key* itself,
    letting a caller recover a command's outcome later even when it only
    knows the dedup key it originally supplied (e.g. a client-generated
    idempotency key), not the internal journal row id.
    """
    canonical = f"{command_type}:{dedup_key}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_command(
    session: Session,
    context: TenantContext,
    *,
    command_type: str,
    deduplication_key: str,
    result_id: str,
    request_fingerprint: str,
    scenario_scope_key: str | None = None,
    result_resource_type: str | None = None,
) -> CommandClaim:
    """Atomically own ``(workspace, mode, command_type, dedup_key)`` or
    return its already-committed replay (spec §7.1).

    ``scenario_scope_key`` defaults to the reserved no-scenario marker for
    every mode except ``controlled_mock``, which must supply one explicitly
    (mirrors ``app/scale/enrollment.py``'s ``_validate_scenario_scope``).
    ``result_resource_type`` defaults from ``command_type``; pass it
    explicitly only to override that mapping.
    """
    if scenario_scope_key is None:
        if context.meta_mode == "controlled_mock":
            raise ValueError(
                "scenario_scope_key is required when meta_mode='controlled_mock'"
            )
        scenario_scope_key = NO_SCENARIO_MARKER

    if result_resource_type is None:
        result_resource_type = _DEFAULT_RESULT_RESOURCE_TYPE.get(command_type, command_type)

    now = datetime.now(timezone.utc)
    values = dict(
        id=new_id("scj"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        scenario_scope_key=scenario_scope_key,
        command_type=command_type,
        dedup_key=deduplication_key,
        request_fingerprint=request_fingerprint,
        result_resource_id=result_id,
        result_resource_type=result_resource_type,
        initiator_id=context.initiator_id or context.executor,
        created_at=now,
        identity_hash=compute_identity_hash(command_type, deduplication_key),
    )

    dialect_module = postgresql if is_postgres(session) else sqlite
    stmt = (
        dialect_module.insert(ScaleCommandJournalRecord)
        .values(**values)
        .on_conflict_do_nothing(index_elements=list(_CLAIM_CONFLICT_COLUMNS))
        .returning(ScaleCommandJournalRecord)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is not None:
        return CommandClaim(record=row, replay=False)

    existing = session.execute(
        select(ScaleCommandJournalRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            command_type=command_type,
            dedup_key=deduplication_key,
        )
    ).scalar_one()

    if existing.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError(
            command_type, deduplication_key,
            existing.request_fingerprint, request_fingerprint,
        )
    return CommandClaim(record=existing, replay=True)


class CommandOutcomeNotFoundError(Exception):
    """No journal row matches the given ``(workspace, mode, identity_hash)``
    -- either the command was never claimed, or the caller's identity hash
    does not correspond to any recorded ``(command_type, dedup_key)`` pair."""

    def __init__(self, identity_hash: str) -> None:
        self.identity_hash = identity_hash
        super().__init__(f"command_outcome_not_found: {identity_hash}")


@dataclass(frozen=True)
class CommandOutcome:
    """The recovered outcome of a previously-claimed command, keyed by its
    server-derived identity hash rather than the journal row's internal id
    (issue #87 Task 4)."""

    identity_hash: str
    command_type: str
    dedup_key: str
    result_resource_id: str
    result_resource_type: str
    request_fingerprint: str
    initiator_id: str
    created_at: datetime


def lookup_command_outcome(
    session: Session,
    context: TenantContext,
    identity_hash: str,
    command_kind: str | None = None,
) -> CommandOutcome:
    """Recover a previously-claimed command's outcome by its identity hash,
    scoped to ``context``'s workspace and mode (never leaking across
    tenants). Raises :class:`CommandOutcomeNotFoundError` when no matching
    row exists. ``command_kind`` optionally narrows the lookup to one
    ``command_type``, guarding against an identity-hash collision being
    reinterpreted as the wrong command."""
    stmt = select(ScaleCommandJournalRecord).filter_by(
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        identity_hash=identity_hash,
    )
    if command_kind:
        stmt = stmt.filter(ScaleCommandJournalRecord.command_type == command_kind)

    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        raise CommandOutcomeNotFoundError(identity_hash)

    return CommandOutcome(
        identity_hash=row.identity_hash,
        command_type=row.command_type,
        dedup_key=row.dedup_key,
        result_resource_id=row.result_resource_id,
        result_resource_type=row.result_resource_type,
        request_fingerprint=row.request_fingerprint,
        initiator_id=row.initiator_id,
        created_at=row.created_at,
    )
