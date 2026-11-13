"""Keep a Workspace's private storage within ``storage.bytes.max`` (#158).

Contract §8 caps Beta private storage at 10 GiB; since #156 every Workspace's
Entitlement Projection carries ``storage.bytes.max``. Contract §11 step 3
enforces storage, among the other Plan Entitlements, in the admission
transaction. This module holds the two admission checks, and the maintainer's
rule for each (#147 triage):

* **Upload** (:func:`admit_upload`): refused when the bytes the Workspace
  stores, plus the declared ``content_length_max`` of its open upload intents,
  plus this upload's ``content_length_max`` would exceed the limit. An upload
  of exactly the remaining room passes. An intent is open while it is
  ``pending`` or ``verifying`` and has not expired; a finalized intent's bytes
  are counted by the row that stores them instead, and a rejected or expired
  one stores nothing.
* **Generation** (:func:`admit_generation`): refused only when the bytes the
  Workspace stores already reach the limit. Below it a generation is admitted
  even though its images may carry the Workspace over: its size is not known
  until the provider returns.

Neither check deletes or hides anything. A Workspace over its limit keeps
every byte; it only stops admitting new uploads and generations until its
limit is raised.

**What is stored** is :func:`app.entitlements.storage_usage.workspace_storage_bytes`
-- #148's rule, summed live inside the admission transaction, never the
daily ``storage_bytes`` Usage row.

**The limit** is read from the Workspace's Entitlement Projection and fails
closed: no projection, no ``storage.bytes.max`` key, or a value that is not a
non-negative whole number refuses the command as ``entitlement_unavailable``.

**Concurrent uploads contend for one quota** (contract §11, last paragraph).
On PostgreSQL :func:`admit_upload` first takes a transaction-scoped advisory
lock keyed by the Workspace, then reads the stored and open bytes in fresh
READ COMMITTED statements: a second upload admission waits until the first
commits (its intent is then open, and counted) or rolls back (it is not). The
API role holds only SELECT on ``workspaces``, so the lock is an advisory one
rather than ``SELECT ... FOR UPDATE`` on the Workspace row, which would need
UPDATE. Generation reserves no bytes at admission -- it only compares what is
already stored with the limit -- so it takes no lock: serializing it would not
change an outcome. SQLite, a local compatibility path, has one writer.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.adapters.persistence.models import UploadIntentRecord, utcnow
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.storage_usage import workspace_storage_bytes
from app.platform.tenancy import TenantContext, is_postgres

logger = logging.getLogger(__name__)

__all__ = [
    "OPEN_UPLOAD_INTENT_STATES",
    "STORAGE_LIMIT_KEY",
    "StorageAdmission",
    "StorageAdmissionError",
    "StorageLimitExceededError",
    "StorageLimitReachedError",
    "StorageLimitUnavailableError",
    "admit_generation",
    "admit_upload",
    "open_upload_bytes",
    "storage_limit_bytes",
]

STORAGE_LIMIT_KEY = "storage.bytes.max"
#: Upload intents whose declared bytes are still on their way in.
OPEN_UPLOAD_INTENT_STATES = ("pending", "verifying")
_GIB = Decimal(1024 ** 3)


class StorageAdmissionError(Exception):
    """A command was refused because of the Workspace's storage limit.

    ``code`` is the machine-readable reason the API surfaces, ``status`` its
    HTTP status, ``params`` what its copy interpolates.
    """

    status = 409
    retryable = False

    def __init__(self, code: str, detail: str, *, params: dict | None = None) -> None:
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


class StorageLimitUnavailableError(StorageAdmissionError):
    """The Workspace's limit cannot be read, so storage is refused (fail closed)."""

    status = 403

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.workspace_id = workspace_id
        self.reason = reason
        super().__init__(
            "entitlement_unavailable",
            f"the storage limit of workspace {workspace_id} cannot be read: {reason}",
        )


class StorageLimitExceededError(StorageAdmissionError):
    """This upload would take the Workspace's storage over its limit."""

    def __init__(self, admission: "StorageAdmission", requested_bytes: int) -> None:
        self.admission = admission
        self.requested_bytes = requested_bytes
        super().__init__(
            "storage_limit_exceeded",
            f"{admission.stored_bytes} stored + {admission.open_upload_bytes} in open uploads "
            f"+ {requested_bytes} requested exceeds the {admission.limit_bytes} byte storage limit",
            params={
                **admission.params(),
                "requested_bytes": requested_bytes,
            },
        )


class StorageLimitReachedError(StorageAdmissionError):
    """The Workspace already stores as much as its limit allows."""

    def __init__(self, admission: "StorageAdmission") -> None:
        self.admission = admission
        super().__init__(
            "storage_limit_reached",
            f"{admission.stored_bytes} bytes stored, at or over the "
            f"{admission.limit_bytes} byte storage limit",
            params=admission.params(),
        )


@dataclass(frozen=True)
class StorageAdmission:
    """What an admitted (or refused) check read."""

    limit_bytes: int
    stored_bytes: int
    #: Declared bytes of the Workspace's open upload intents; 0 for generation.
    open_upload_bytes: int = 0

    def params(self) -> dict:
        return {
            "limit_gib": _gib(self.limit_bytes),
            "limit_bytes": self.limit_bytes,
            "stored_bytes": self.stored_bytes,
            "open_upload_bytes": self.open_upload_bytes,
        }


def _gib(value: int) -> str:
    """``10737418240`` -> ``"10"``; ``1610612736`` -> ``"1.5"``."""
    gib = (Decimal(value) / _GIB).quantize(Decimal("0.1"))
    return format(gib.normalize(), "f")


def storage_limit_bytes(session: Session, context: TenantContext) -> int:
    """The context Workspace's ``storage.bytes.max``, or refuse (fail closed)."""
    projection = session.execute(
        select(EntitlementProjectionRecord).where(
            EntitlementProjectionRecord.workspace_id == context.workspace_id,
        )
    ).scalar_one_or_none()
    if projection is None:
        raise StorageLimitUnavailableError(context.workspace_id, "no entitlement projection")
    raw = projection.resolved_entitlements_json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict) or STORAGE_LIMIT_KEY not in raw:
        raise StorageLimitUnavailableError(context.workspace_id, f"no {STORAGE_LIMIT_KEY!r} entitlement")
    value = raw[STORAGE_LIMIT_KEY]
    if isinstance(value, bool):
        raise StorageLimitUnavailableError(context.workspace_id, f"{STORAGE_LIMIT_KEY!r} is not a number")
    try:
        limit = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise StorageLimitUnavailableError(
            context.workspace_id, f"{STORAGE_LIMIT_KEY!r} is not a number",
        ) from None
    if not limit.is_finite() or limit < 0 or limit != limit.to_integral_value():
        raise StorageLimitUnavailableError(
            context.workspace_id, f"{STORAGE_LIMIT_KEY!r} is not a whole number of bytes",
        )
    return int(limit)


def open_upload_bytes(session: Session, context: TenantContext, *, now: datetime | None = None) -> int:
    """Declared ``content_length_max`` of the Workspace's open upload intents."""
    now = now or utcnow()
    total = session.scalar(
        select(func.coalesce(func.sum(UploadIntentRecord.content_length_max), 0)).where(
            UploadIntentRecord.workspace_id == context.workspace_id,
            UploadIntentRecord.state.in_(OPEN_UPLOAD_INTENT_STATES),
            UploadIntentRecord.expires_at > now,
        )
    )
    return int(total or 0)


def _lock_storage_quota(session: Session, context: TenantContext) -> None:
    """Serialize the Workspace's upload admissions until this transaction ends."""
    if not is_postgres(session):
        return
    scope = f"storage-quota:{context.workspace_id}".encode("utf-8")
    key = int.from_bytes(hashlib.sha256(scope).digest()[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def admit_upload(
    session: Session,
    context: TenantContext,
    requested_bytes: int,
    *,
    now: datetime | None = None,
) -> StorageAdmission:
    """Admit an upload of up to ``requested_bytes``, or refuse it.

    Runs in the caller's transaction, before the intent is written, and holds
    the Workspace's quota lock until that transaction ends (module docstring).
    """
    _lock_storage_quota(session, context)
    limit = storage_limit_bytes(session, context)
    admission = StorageAdmission(
        limit_bytes=limit,
        stored_bytes=workspace_storage_bytes(session, context),
        open_upload_bytes=open_upload_bytes(session, context, now=now),
    )
    if admission.stored_bytes + admission.open_upload_bytes + requested_bytes > limit:
        _log_refusal("upload", context, admission, requested_bytes)
        raise StorageLimitExceededError(admission, requested_bytes)
    return admission


def admit_generation(session: Session, context: TenantContext) -> StorageAdmission:
    """Admit a generation unless the Workspace already stores its limit.

    Runs in the caller's admission transaction, after an idempotent replay has
    been answered: a replay never re-checks the limit.
    """
    limit = storage_limit_bytes(session, context)
    admission = StorageAdmission(
        limit_bytes=limit,
        stored_bytes=workspace_storage_bytes(session, context),
    )
    if admission.stored_bytes >= limit:
        _log_refusal("generation", context, admission, 0)
        raise StorageLimitReachedError(admission)
    return admission


def _log_refusal(kind: str, context: TenantContext, admission: StorageAdmission, requested: int) -> None:
    logger.info(
        "storage_admission_refused",
        extra={
            "workspace_id": context.workspace_id,
            "admission": kind,
            "limit_bytes": admission.limit_bytes,
            "stored_bytes": admission.stored_bytes,
            "open_upload_bytes": admission.open_upload_bytes,
            "requested_bytes": requested,
        },
    )
