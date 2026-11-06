"""Idempotency records + pending-reservation concurrency protocol (spec §5.3-5.4).

Two idempotency patterns share the ``idempotency_records`` table:

* **Async commands** (202 + Operation) — :func:`create_or_replay_operation`
  binds ``operation_id``.
* **Sync resource creation** (201, e.g. upload/download intents) —
  :func:`create_or_replay_intent` binds ``response_resource_type`` +
  ``response_resource_id`` instead. Exactly one of the two is ever set; the
  table's CHECK constraints (``app.adapters.persistence.models.IdempotencyRecord``,
  installed by ``migrations/versions/i1a2b3c4d5e6_idempotency_records.py``)
  enforce that XOR at the database level.

Why not ``ON CONFLICT DO NOTHING`` alone? With plain ``DO NOTHING``, two
concurrent transactions can each create their own Operation/resource before
either notices the other — the loser's ``INSERT`` into
``idempotency_records`` silently fails, but its Operation/resource is
already committed. The system ends up with two outcomes for one idempotency
key.

**Pending-reservation protocol:** the first step is always to claim a
*pending* reservation (an ``idempotency_records`` row with every outcome
column NULL). If that ``INSERT`` succeeds, this transaction owns the key —
it alone creates the outcome and then commits it onto the reservation. If
the ``INSERT`` conflicts, some other transaction already claimed the key;
this transaction waits for the winner to commit and then reads its result,
raising rather than racing it to create a second outcome.

This module talks to ``idempotency_records`` with raw SQL
(``session.execute(text(...))``), not the ORM mapping in
``app.adapters.persistence.models.IdempotencyRecord`` — the protocol is
built around ``INSERT ... ON CONFLICT DO NOTHING ... RETURNING``, which the
ORM has no vocabulary for, and every one of these statements must run
inside the caller's existing transaction (spec §5.5) rather than opening
its own.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..adapters.persistence.models import utcnow
from .operations import OperationRecord, create_operation, get_operation
from .tenancy import current_context

__all__ = [
    "IdempotencyRecord",
    "IdempotencyKeyReusedError",
    "IdempotencyTimeoutError",
    "IdempotencyReservationLostError",
    "compute_fingerprint",
    "hash_idempotency_key",
    "get_idempotency_record",
    "create_or_replay_operation",
    "create_or_replay_intent",
]

#: Spec §5.3: reservations and committed records alike expire 24h after
#: creation. A background sweep (out of this module's scope) reaps rows past
#: ``expires_at``; ``idx_idempotency_expires`` on the migration exists for it.
DEFAULT_TTL = timedelta(hours=24)

#: Spec §5.4 defaults for the losing side of a reservation race.
DEFAULT_MAX_WAIT_SECONDS = 5.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class IdempotencyRecord:
    """One ``idempotency_records`` row (spec §5.3), typed for callers.

    ``operation_id`` is set for the async path, ``response_resource_type`` +
    ``response_resource_id`` for the sync path — never both, and neither
    while ``state == "pending"``; the table's CHECK constraints are the
    authority for that invariant, this dataclass just carries it.
    """

    key_hash: str
    workspace_id: str
    actor_id: str
    command_type: str
    request_fingerprint_hash: str
    state: str
    operation_id: str | None
    response_resource_type: str | None
    response_resource_id: str | None
    response_status: int | None
    created_at: datetime
    expires_at: datetime


class IdempotencyKeyReusedError(Exception):
    """The same Idempotency-Key was reused for a different actor, command, or request.

    Spec §5.3: a mismatch on ``actor_id``, ``command_type``, or the request
    fingerprint returns 409 ``idempotency_key_reused`` — never a silent
    success against the wrong request.
    """

    def __init__(self, key_hash: str):
        self.key_hash = key_hash
        super().__init__(f"Idempotency key reused: {key_hash}")


class IdempotencyTimeoutError(Exception):
    """The concurrent winner did not commit an outcome before the wait deadline."""

    def __init__(self, key_hash: str):
        self.key_hash = key_hash
        super().__init__(f"Timed out waiting for idempotent outcome: {key_hash}")


class IdempotencyReservationLostError(Exception):
    """The pending reservation this caller was waiting on disappeared.

    This should not happen under the normal protocol (a reservation is only
    ever removed by expiry cleanup, long after any live waiter would have
    given up) — it exists so a waiter fails loudly instead of hanging or
    silently treating a vanished row as "nothing to replay".
    """

    def __init__(self, key_hash: str):
        self.key_hash = key_hash
        super().__init__(f"Idempotency reservation lost: {key_hash}")


def compute_fingerprint(
    method: str,
    operation_id: str,
    path: str,
    query: dict,
    body: dict | None,
    precondition_headers: dict[str, str],
) -> str:
    """SHA-256 of the full request identity, not just the body (spec §5.3).

    Binding method, path, query, and precondition headers (``If-Match``)
    alongside the body means the same Idempotency-Key reused against a
    different request is detected as a reuse conflict rather than replayed.
    """
    canonical = json.dumps(
        {
            "method": method,
            "operation_id": operation_id,
            "path": path,
            "query": dict(sorted(query.items())),
            "body": body,
            "precondition": dict(sorted(precondition_headers.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def hash_idempotency_key(idempotency_key: str) -> str:
    """SHA-256 of the raw ``Idempotency-Key`` header value -- the primary-key
    half of an ``idempotency_records`` row (paired with ``workspace_id``).

    Public so a caller that needs to peek at an existing reservation before
    deciding whether to apply its own resource-specific validation (e.g.
    ``app.main.finalize_upload_endpoint``'s state gate, which must not
    re-run on a replay of an already-committed request) can compute the
    same ``key_hash`` this module uses internally, via
    :func:`get_idempotency_record`.
    """
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


_SELECT_COLUMNS = (
    "key_hash, workspace_id, actor_id, command_type, request_fingerprint_hash, "
    "state, operation_id, response_resource_type, response_resource_id, "
    "response_status, created_at, expires_at"
)


def _row_to_record(row: Mapping[str, Any]) -> IdempotencyRecord:
    return IdempotencyRecord(**dict(row))


def get_idempotency_record(
    session: Session, key_hash: str, workspace_id: str
) -> IdempotencyRecord | None:
    """Fetch the current row for ``(key_hash, workspace_id)``, or None."""
    row = session.execute(
        text(
            f"SELECT {_SELECT_COLUMNS} FROM idempotency_records "
            "WHERE key_hash = :key_hash AND workspace_id = :workspace_id"
        ),
        {"key_hash": key_hash, "workspace_id": workspace_id},
    ).mappings().first()
    return None if row is None else _row_to_record(row)


def _check_binding(
    row: Mapping[str, Any], *, actor_id: str, command_type: str, fingerprint_hash: str, key_hash: str
) -> None:
    if row["actor_id"] != actor_id or row["command_type"] != command_type:
        raise IdempotencyKeyReusedError(key_hash)
    if row["request_fingerprint_hash"] != fingerprint_hash:
        raise IdempotencyKeyReusedError(key_hash)


def _claim_reservation(
    session: Session,
    *,
    key_hash: str,
    workspace_id: str,
    actor_id: str,
    command_type: str,
    fingerprint_hash: str,
    expires_at: datetime,
) -> bool:
    """Attempt the pending-reservation INSERT. True iff this caller won it.

    ``created_at`` is bound explicitly rather than left to a column default:
    every write in this module is a raw ``session.execute(text(...))``, not
    an ORM ``session.add()``, so the mapped class's Python-side
    ``default=utcnow`` (see ``IdempotencyRecord`` in
    ``app.adapters.persistence.models``) never fires for it.
    """
    result = session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                key_hash, workspace_id, actor_id, command_type,
                request_fingerprint_hash, state, created_at, expires_at
            )
            VALUES (
                :key_hash, :workspace_id, :actor_id, :command_type,
                :fingerprint_hash, 'pending', :created_at, :expires_at
            )
            ON CONFLICT (key_hash, workspace_id) DO NOTHING
            RETURNING key_hash
            """
        ),
        {
            "key_hash": key_hash,
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "command_type": command_type,
            "fingerprint_hash": fingerprint_hash,
            "created_at": utcnow(),
            "expires_at": expires_at,
        },
    ).first()
    return result is not None


def _poll_until_committed(
    session: Session,
    *,
    key_hash: str,
    workspace_id: str,
    actor_id: str,
    command_type: str,
    fingerprint_hash: str,
    max_wait_seconds: float,
    poll_interval: float,
) -> Mapping[str, Any]:
    """Poll the reservation row until the winner commits, or raise.

    Spec §5.4: the loser of the reservation race never creates its own
    outcome — it waits for the winner and replays what the winner produced.
    """
    deadline = time.monotonic() + max_wait_seconds
    while True:
        row = session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM idempotency_records "
                "WHERE key_hash = :key_hash AND workspace_id = :workspace_id"
            ),
            {"key_hash": key_hash, "workspace_id": workspace_id},
        ).mappings().first()

        if row is None:
            raise IdempotencyReservationLostError(key_hash)
        _check_binding(
            row,
            actor_id=actor_id,
            command_type=command_type,
            fingerprint_hash=fingerprint_hash,
            key_hash=key_hash,
        )
        if row["state"] == "committed":
            return row

        if time.monotonic() >= deadline:
            raise IdempotencyTimeoutError(key_hash)
        time.sleep(poll_interval)


def create_or_replay_operation(
    session: Session,
    *,
    idempotency_key: str | None,
    command_type: str,
    fingerprint_hash: str,
    actor: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> OperationRecord:
    """Create a new Operation, or replay the one an earlier identical request created.

    Spec §5.4 async command path (202 + Operation). Workspace and actor come
    from the session's bound :class:`~app.platform.tenancy.TenantContext`
    (see :func:`~app.platform.tenancy.current_context`), matching how every
    other platform module in this package derives tenant scope. Without an
    ``idempotency_key`` this degrades to a plain
    :func:`~app.platform.operations.create_operation` call — idempotency is
    opt-in per request, not mandatory.
    """
    if idempotency_key is None:
        return create_operation(session, command_type=command_type, actor=actor)

    context = current_context(session)
    workspace_id = context.workspace_id
    actor_id = context.initiator_id or context.executor
    key_hash = hash_idempotency_key(idempotency_key)

    existing = get_idempotency_record(session, key_hash, workspace_id)
    if existing is not None:
        _check_binding(
            existing.__dict__,
            actor_id=actor_id,
            command_type=command_type,
            fingerprint_hash=fingerprint_hash,
            key_hash=key_hash,
        )
        if existing.state == "committed":
            return get_operation(session, existing.operation_id)
        # state == "pending": another transaction is creating the outcome.
        # Fall through -- the claim attempt below will lose (row already
        # exists) and this caller waits for that transaction to commit.

    won = _claim_reservation(
        session,
        key_hash=key_hash,
        workspace_id=workspace_id,
        actor_id=actor_id,
        command_type=command_type,
        fingerprint_hash=fingerprint_hash,
        expires_at=context_expiry(ttl),
    )

    if won:
        operation = create_operation(
            session,
            command_type=command_type,
            idempotency_key_hash=key_hash,
            actor=actor,
        )
        session.execute(
            text(
                """
                UPDATE idempotency_records
                SET state = 'committed', operation_id = :operation_id, response_status = 202
                WHERE key_hash = :key_hash AND workspace_id = :workspace_id
                """
            ),
            {"operation_id": operation.id, "key_hash": key_hash, "workspace_id": workspace_id},
        )
        return operation

    row = _poll_until_committed(
        session,
        key_hash=key_hash,
        workspace_id=workspace_id,
        actor_id=actor_id,
        command_type=command_type,
        fingerprint_hash=fingerprint_hash,
        max_wait_seconds=max_wait_seconds,
        poll_interval=poll_interval,
    )
    return get_operation(session, row["operation_id"])


def create_or_replay_intent(
    session: Session,
    *,
    idempotency_key: str,
    command_type: str,
    fingerprint_hash: str,
    create_fn: Callable[[Session], tuple[str, str, Any]],
    replay_fn: Callable[[str, str, Session], Any] | None = None,
    ttl: timedelta = DEFAULT_TTL,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> Any:
    """Create a resource, or replay the one an earlier identical request created.

    Spec §5.4 sync resource path (201, e.g. upload/download intents). Same
    reservation protocol as :func:`create_or_replay_operation` with
    ``operation_id`` left NULL, binding ``response_resource_type`` +
    ``response_resource_id`` instead.

    ``create_fn(session) -> (resource_type, resource_id, result)`` performs
    the actual creation and returns the value the *first* caller should get
    back. ``replay_fn(resource_type, resource_id, session) -> result``
    reconstructs that same value for a replaying caller; if omitted it
    defaults to returning ``(resource_type, resource_id)``, since this
    module has no generic way to look up an arbitrary resource.

    Unlike :func:`create_or_replay_operation`, ``idempotency_key`` is
    required here -- sync resource creation always has a caller-supplied key
    (spec §5.4), there is no "fire and forget" variant.
    """
    context = current_context(session)
    workspace_id = context.workspace_id
    actor_id = context.initiator_id or context.executor
    key_hash = hash_idempotency_key(idempotency_key)

    def _replay(resource_type: str, resource_id: str) -> Any:
        if replay_fn is not None:
            return replay_fn(resource_type, resource_id, session)
        return (resource_type, resource_id)

    existing = get_idempotency_record(session, key_hash, workspace_id)
    if existing is not None:
        _check_binding(
            existing.__dict__,
            actor_id=actor_id,
            command_type=command_type,
            fingerprint_hash=fingerprint_hash,
            key_hash=key_hash,
        )
        if existing.state == "committed":
            return _replay(existing.response_resource_type, existing.response_resource_id)
        # pending: fall through, same as create_or_replay_operation above.

    won = _claim_reservation(
        session,
        key_hash=key_hash,
        workspace_id=workspace_id,
        actor_id=actor_id,
        command_type=command_type,
        fingerprint_hash=fingerprint_hash,
        expires_at=context_expiry(ttl),
    )

    if won:
        resource_type, resource_id, result = create_fn(session)
        session.execute(
            text(
                """
                UPDATE idempotency_records
                SET state = 'committed',
                    response_resource_type = :resource_type,
                    response_resource_id = :resource_id,
                    response_status = 201
                WHERE key_hash = :key_hash AND workspace_id = :workspace_id
                """
            ),
            {
                "key_hash": key_hash,
                "workspace_id": workspace_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
        )
        return result

    row = _poll_until_committed(
        session,
        key_hash=key_hash,
        workspace_id=workspace_id,
        actor_id=actor_id,
        command_type=command_type,
        fingerprint_hash=fingerprint_hash,
        max_wait_seconds=max_wait_seconds,
        poll_interval=poll_interval,
    )
    return _replay(row["response_resource_type"], row["response_resource_id"])


def context_expiry(ttl: timedelta) -> datetime:
    """``now + ttl`` using the same UTC clock every other platform module uses."""
    return utcnow() + ttl
