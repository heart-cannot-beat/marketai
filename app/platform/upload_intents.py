"""UploadIntent state machine, CAS verification lease, and Worker verification
sequence (spec §7.5, §7.8-§7.11).

The ORM mapping (``UploadIntentRecord``) lives in
``app.adapters.persistence.models`` -- the codebase's single home for mapped
classes -- and is re-exported here, matching the pattern
``app.platform.operations`` sets for ``OperationRecord``.

Three phases share this module:

1. **Create** (``POST /api/v1/upload-intents``, spec §7.6-§7.7,§7.11): a
   sync, idempotent 201 that persists a ``pending`` intent and issues an
   ``UploadGrant`` (never stored -- re-issued fresh on every idempotent
   replay, capped at the original ``expires_at``).
2. **Finalize** (``POST /api/v1/upload-intents/{id}/finalize``, spec §7.8):
   always 202 + Operation -- this module never inspects or commits a blob
   synchronously inside the request. It only validates the intent is
   eligible and lets the caller enqueue the verification Job.
3. **Verify** (:func:`verify_upload_intent`, spec §7.8's numbered sequence):
   run by the Worker off the Job queue. Acquires the CAS lease, re-checks
   admission and policy, inspects and commits the blob, and transitions the
   intent to a terminal state (or releases the lease back to ``pending`` on
   a transient storage error).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..adapters.persistence.models import UploadIntentRecord, utcnow
from .blob_store import (
    BlobStore,
    BlobStoreError,
    CommittedBlobRef,
    InspectedUpload,
    ObjectChangedError,
    ObjectConstraints,
    ObjectNotFoundError,
    RevisionGoneError,
    StagedBlobRef,
)
from .purpose_policies import PURPOSE_POLICIES
from .tenancy import TenantContext, current_context
from .workspace_admission import WorkspaceAdmissionReader, WorkspaceNotFoundError

__all__ = [
    "UploadIntentRecord",
    "UploadIntentState",
    "VALID_TRANSITIONS",
    "TERMINAL_STATES",
    "validate_intent_transition",
    "LEASE_DURATION_SECONDS",
    "UploadIntentNotFoundError",
    "UploadIntentLeaseLostError",
    "build_staging_key",
    "get_upload_intent",
    "create_upload_intent_record",
    "expire_upload_intent",
    "acquire_verification_lease",
    "release_lease_to_pending",
    "sniff_mime",
    "VerificationOutcome",
    "verify_upload_intent",
    "REJECTION_CODES",
]


UploadIntentState = Literal["pending", "verifying", "finalized", "rejected", "expired"]

#: Spec §7.9's state diagram:
#:   pending -> verifying -> finalized
#:                        -> rejected
#:                        -> pending   (transient storage error, release lease)
#:   pending -> expired
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"verifying", "expired"},
    "verifying": {"finalized", "rejected", "pending"},
    "finalized": set(),
    "rejected": set(),
    "expired": set(),
}

TERMINAL_STATES = frozenset({"finalized", "rejected", "expired"})

#: Spec §7.9: "Lease exceeds max duration (30 seconds)" prevents a crashed
#: Worker from holding an intent in `verifying` forever.
LEASE_DURATION_SECONDS = 30


def validate_intent_transition(from_state: str, to_state: str) -> bool:
    """Whether the state machine admits ``from_state`` -> ``to_state``.

    Any pair not listed in :data:`VALID_TRANSITIONS` is rejected -- there is
    no default "allow" path, mirroring
    ``app.platform.operations.validate_transition``.
    """
    return to_state in VALID_TRANSITIONS.get(from_state, set())


class UploadIntentNotFoundError(LookupError):
    """No UploadIntent with this id exists inside the caller's Workspace."""


class UploadIntentLeaseLostError(RuntimeError):
    """A CAS state transition did not apply -- the expected prior state/lease
    was not held (concurrent Worker, expired/reclaimed lease, or the intent
    moved on already)."""


# --------------------------------------------------------------------------
# Object key construction (spec §7.13)
# --------------------------------------------------------------------------


def build_staging_key(workspace_id: str, purpose: str) -> str:
    """``staging/{workspace_id}/{purpose}/{YYYY}/{MM}/{DD}/{leaf}.dat``.

    The leaf uses the same ``uuid4().hex`` scheme as
    ``app.adapters.persistence.models.new_id`` elsewhere in this codebase
    (not a true ULID library) -- what the key structure actually requires
    (spec §7.13) is a globally unique, path-safe leaf, which this satisfies.
    """
    now = utcnow()
    leaf = uuid.uuid4().hex
    return f"staging/{workspace_id}/{purpose}/{now:%Y}/{now:%m}/{now:%d}/{leaf}.dat"


# --------------------------------------------------------------------------
# Create phase
# --------------------------------------------------------------------------


def get_upload_intent(session: Session, intent_id: str) -> UploadIntentRecord:
    """Fetch an UploadIntent scoped to the session's bound Workspace, or raise.

    Cross-Workspace lookups (spec §7.18) surface identically to an unknown
    id -- existence is not revealed to a caller outside the owning
    Workspace.
    """
    context = current_context(session)
    record = session.get(UploadIntentRecord, intent_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise UploadIntentNotFoundError(intent_id)
    return record


def create_upload_intent_record(
    session: Session,
    *,
    intent_id: str,
    staging_key: str,
    owner_kind: str,
    owner_id: str | None,
    purpose: str,
    filename_original: str,
    content_type: str,
    content_length_min: int,
    content_length_max: int,
    checksum_sha256_b64: str,
    ttl_seconds: int,
) -> UploadIntentRecord:
    """Persist a new ``pending`` UploadIntent. Callers own idempotency (see
    ``app.platform.idempotency.create_or_replay_intent``)."""
    context = current_context(session)
    now = utcnow()
    record = UploadIntentRecord(
        id=intent_id,
        workspace_id=context.workspace_id,
        initiator_id=context.initiator_id,
        authorization_epoch_at_issue=context.authorization_epoch,
        owner_kind=owner_kind,
        owner_id=owner_id,
        staging_key=staging_key,
        purpose=purpose,
        filename_original=filename_original,
        content_type=content_type,
        content_length_min=content_length_min,
        content_length_max=content_length_max,
        checksum_sha256_b64=checksum_sha256_b64,
        state="pending",
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(record)
    session.flush()
    return record


def expire_upload_intent(session: Session, intent_id: str) -> bool:
    """Mark a pending intent expired and durably schedule staging cleanup.

    Returns whether this call performed the transition.  Replays against an
    already-expired intent still converge on the same idempotent cleanup Job.
    """
    context = current_context(session)
    result = session.execute(
        text(
            """
            UPDATE upload_intent_records
            SET state = 'expired'
            WHERE id = :intent_id AND workspace_id = :workspace_id
              AND state = 'pending'
            """
        ),
        {"intent_id": intent_id, "workspace_id": context.workspace_id},
    )
    # Raw SQL does not synchronize an UploadIntent already present in this
    # Session's identity map (the finalize endpoint loaded it just above).
    session.expire_all()
    record = get_upload_intent(session, intent_id)
    if record.state == "expired":
        _enqueue_staging_cleanup(session, intent_id)
    return result.rowcount == 1


# --------------------------------------------------------------------------
# CAS lease primitives (spec §7.9) -- raw SQL, mirroring
# app.platform.jobs.claim_next's conditional-UPDATE-with-rowcount pattern,
# since the ORM has no vocabulary for "UPDATE ... WHERE state = X".
# --------------------------------------------------------------------------


def acquire_verification_lease(
    session: Session,
    intent_id: str,
    lease_id: str,
    lease_seconds: int = LEASE_DURATION_SECONDS,
) -> UploadIntentRecord:
    """``pending -> verifying`` CAS. Only one Worker acquires; a lost race or
    an intent that is not ``pending`` raises :class:`UploadIntentLeaseLostError`."""
    context = current_context(session)
    now = utcnow()
    result = session.execute(
        text(
            """
            UPDATE upload_intent_records
            SET state = 'verifying',
                verification_lease_id = :lease_id,
                verification_lease_expires_at = :lease_expires_at
            WHERE id = :intent_id AND workspace_id = :workspace_id AND state = 'pending'
            """
        ),
        {
            "lease_id": lease_id,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "intent_id": intent_id,
            "workspace_id": context.workspace_id,
        },
    )
    if result.rowcount != 1:
        raise UploadIntentLeaseLostError(intent_id)
    session.flush()
    return get_upload_intent(session, intent_id)


def release_lease_to_pending(session: Session, intent_id: str, lease_id: str) -> None:
    """``verifying -> pending`` on a transient storage error (spec §7.9):
    releases the lease so the next Job attempt can retry, never a permanent
    rejection."""
    context = current_context(session)
    result = session.execute(
        text(
            """
            UPDATE upload_intent_records
            SET state = 'pending', verification_lease_id = NULL, verification_lease_expires_at = NULL
            WHERE id = :intent_id AND workspace_id = :workspace_id
              AND state = 'verifying' AND verification_lease_id = :lease_id
            """
        ),
        {"intent_id": intent_id, "workspace_id": context.workspace_id, "lease_id": lease_id},
    )
    if result.rowcount != 1:
        raise UploadIntentLeaseLostError(intent_id)


def _reject(session: Session, intent_id: str, lease_id: str, rejection_code: str) -> None:
    context = current_context(session)
    result = session.execute(
        text(
            """
            UPDATE upload_intent_records
            SET state = 'rejected', rejection_code = :rejection_code,
                verification_lease_id = NULL, verification_lease_expires_at = NULL
            WHERE id = :intent_id AND workspace_id = :workspace_id
              AND state = 'verifying' AND verification_lease_id = :lease_id
            """
        ),
        {
            "intent_id": intent_id,
            "workspace_id": context.workspace_id,
            "lease_id": lease_id,
            "rejection_code": rejection_code,
        },
    )
    if result.rowcount != 1:
        raise UploadIntentLeaseLostError(intent_id)
    _enqueue_staging_cleanup(session, intent_id)


def _finalize(session: Session, intent_id: str, lease_id: str, committed: CommittedBlobRef) -> None:
    context = current_context(session)
    now = utcnow()
    result = session.execute(
        text(
            """
            UPDATE upload_intent_records
            SET state = 'finalized',
                committed_blob_ref_key = :key,
                committed_blob_ref_version = :version,
                verification_lease_id = NULL,
                verification_lease_expires_at = NULL,
                finalized_at = :now
            WHERE id = :intent_id AND workspace_id = :workspace_id
              AND state = 'verifying' AND verification_lease_id = :lease_id
            """
        ),
        {
            "intent_id": intent_id,
            "workspace_id": context.workspace_id,
            "lease_id": lease_id,
            "key": committed.committed_key,
            "version": committed.storage_version_id,
            "now": now,
        },
    )
    if result.rowcount != 1:
        raise UploadIntentLeaseLostError(intent_id)
    _enqueue_staging_cleanup(session, intent_id)


def _enqueue_staging_cleanup(session: Session, intent_id: str) -> None:
    """Persist an idempotent cleanup job in the terminal-state transaction.

    Railway Bucket has no lifecycle rules.  Enqueuing here means a crash
    after finalization/rejection cannot lose cleanup work, while the queue's
    normal lease recovery makes deletion replayable.  The key is a
    server-generated object identifier, never a signed URL or credential.
    """
    from .jobs import enqueue_in_session

    context = current_context(session)
    intent = get_upload_intent(session, intent_id)
    enqueue_in_session(
        session,
        context=context,
        job_type="cleanup_staging_upload",
        payload={
            "intent_id": intent.id,
            "staging_key": intent.staging_key,
            # A presigned upload is reusable until it expires.  Deleting
            # earlier would let a late client recreate an untracked staging
            # object after cleanup had already reported success.
            "not_before": intent.expires_at.isoformat(),
        },
        idempotency_key=f"cleanup_staging_upload:{intent.id}",
        resource_type="upload_intent",
        resource_id=intent.id,
        safe_to_retry_after_crash=True,
        max_attempts=10,
    )


# --------------------------------------------------------------------------
# MIME sniffing (spec §7.5) -- pure-Python magic-byte signatures for exactly
# the verified MIME types PURPOSE_POLICIES allowlists. A deployment that
# wants broader coverage may swap in `python-magic`; this default has no
# external/system dependency (libmagic) and is sufficient for every MIME
# this seam ever commits.
# --------------------------------------------------------------------------

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),  # empty archive
)


def sniff_mime(magic_bytes: bytes) -> str:
    """Verified MIME from the first bytes of a file -- never the declared
    ``Content-Type`` (spec §7.5)."""
    if magic_bytes[:4] == b"RIFF" and magic_bytes[8:12] == b"WEBP":
        return "image/webp"
    for signature, mime in _MAGIC_SIGNATURES:
        if magic_bytes.startswith(signature):
            return mime

    # JSON and CSV have no reliable binary magic signature. For the manual
    # fact-import purpose, classify only UTF-8 text with the expected leading
    # structure; the import parser still validates the complete document and
    # its fact schema before any Library data is created.
    text_bytes = magic_bytes[3:] if magic_bytes.startswith(b"\xef\xbb\xbf") else magic_bytes
    try:
        text = text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return "application/json"

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and "," in lines[0]:
        return "text/csv"
    return "application/octet-stream"


# --------------------------------------------------------------------------
# Worker verification sequence (spec §7.8)
# --------------------------------------------------------------------------

REJECTION_CODES = {
    "workspace_suspended",
    "authorization_epoch_stale",
    "purpose_policy_unavailable",
    "mime_type_not_allowed",
    "size_mismatch",
    "checksum_mismatch",
    "object_replaced_during_verification",
    "staging_object_not_found",
    "staging_object_gone",
}


@dataclass(frozen=True)
class VerificationOutcome:
    """What :func:`verify_upload_intent` did, for the caller to translate
    into an ``OperationState`` transition (spec §7.8 step 11)."""

    kind: Literal["finalized", "rejected", "retry"]
    rejection_code: str | None = None
    committed: CommittedBlobRef | None = None
    detail: str = ""


async def verify_upload_intent(
    context: TenantContext,
    intent_id: str,
    *,
    session_factory,
    blob_store: BlobStore,
    admission_reader: WorkspaceAdmissionReader | None,
    lease_id: str,
) -> VerificationOutcome:
    """Run spec §7.8's numbered verification sequence for one UploadIntent.

    ``session_factory()`` opens a tenant-scoped session
    (``app.adapters.persistence.database.tenant_session(context)``) --
    injected rather than imported, so this stays a plain async function the
    Worker can call without importing the database module's context manager
    signature here too. Steps 1-4 (admission/epoch/purpose re-check) and the
    CAS lease acquisition (step 5) run in one short transaction; steps 6-9
    (the actual blob I/O) run outside any open transaction, since they may
    be slow; the terminal transition (steps 10-11 or the reject/retry path)
    runs in a final short transaction.

    ``admission_reader`` is ``None`` when delegated identity is not wired
    (``config.DELEGATED_IDENTITY_ENABLED`` is false -- the local/SQLite
    path, which also has no RLS): steps 1-3's domain-side admission re-check
    calls ``marketai_workspace_admission``, a PostgreSQL-only function
    (migration ``g1a2b3c4d5e6``), so there is nothing to re-check on that
    path and this skips straight to step 4.
    """
    # Steps 1-4: re-check domain-side projections and purpose policy before
    # touching the blob at all -- cheapest checks first.
    if admission_reader is not None:
        try:
            admission = await admission_reader.read(context.workspace_id)
        except WorkspaceNotFoundError:
            with session_factory() as session:
                _reject_without_lease(session, intent_id, "workspace_suspended")
            return VerificationOutcome(kind="rejected", rejection_code="workspace_suspended")
    else:
        admission = None

    with session_factory() as session:
        intent = get_upload_intent(session, intent_id)

        # Steps 2-4 run before the CAS lease is acquired (step 5) -- the
        # intent is still `pending` here, so a rejection at this point uses
        # `_reject_without_lease` rather than `_reject` (which requires the
        # `verifying` state + a matching `verification_lease_id`).
        if admission is not None and admission.suspended:
            _reject_without_lease(session, intent_id, "workspace_suspended")
            return VerificationOutcome(kind="rejected", rejection_code="workspace_suspended")

        if admission is not None and admission.authorization_epoch != intent.authorization_epoch_at_issue:
            _reject_without_lease(session, intent_id, "authorization_epoch_stale")
            return VerificationOutcome(kind="rejected", rejection_code="authorization_epoch_stale")

        policy = PURPOSE_POLICIES.get(intent.purpose)
        if policy is None or intent.content_length_max > policy.max_size_bytes:
            _reject_without_lease(session, intent_id, "purpose_policy_unavailable")
            return VerificationOutcome(kind="rejected", rejection_code="purpose_policy_unavailable")

        # Step 5: acquire the verifying lease.
        acquire_verification_lease(session, intent_id, lease_id)
        staging_key = intent.staging_key
        content_length_min = intent.content_length_min
        content_length_max = intent.content_length_max
        checksum_sha256_b64 = intent.checksum_sha256_b64

    # Steps 6-9: the actual blob I/O, outside any open transaction.
    try:
        inspected: InspectedUpload = await blob_store.inspect_upload(StagedBlobRef(staging_key))
    except ObjectNotFoundError:
        with session_factory() as session:
            _reject(session, intent_id, lease_id, "staging_object_not_found")
        return VerificationOutcome(kind="rejected", rejection_code="staging_object_not_found")
    except BlobStoreError as exc:
        with session_factory() as session:
            release_lease_to_pending(session, intent_id, lease_id)
        return VerificationOutcome(kind="retry", detail=str(exc))

    verified_mime = sniff_mime(inspected.magic_bytes)
    if verified_mime not in policy.allowed_mimes:
        with session_factory() as session:
            _reject(session, intent_id, lease_id, "mime_type_not_allowed")
        return VerificationOutcome(kind="rejected", rejection_code="mime_type_not_allowed")

    if not (content_length_min <= inspected.size_bytes <= content_length_max):
        with session_factory() as session:
            _reject(session, intent_id, lease_id, "size_mismatch")
        return VerificationOutcome(kind="rejected", rejection_code="size_mismatch")

    if inspected.checksum_sha256_b64 != checksum_sha256_b64:
        with session_factory() as session:
            _reject(session, intent_id, lease_id, "checksum_mismatch")
        return VerificationOutcome(kind="rejected", rejection_code="checksum_mismatch")

    try:
        committed = await blob_store.commit_upload(
            inspected,
            ObjectConstraints(
                expected_size=inspected.size_bytes,
                expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
                expected_magic_mime=verified_mime,
                storage_revision=inspected.storage_revision,
            ),
        )
    except ObjectChangedError:
        with session_factory() as session:
            _reject(session, intent_id, lease_id, "object_replaced_during_verification")
        return VerificationOutcome(kind="rejected", rejection_code="object_replaced_during_verification")
    except RevisionGoneError:
        with session_factory() as session:
            _reject(session, intent_id, lease_id, "staging_object_gone")
        return VerificationOutcome(kind="rejected", rejection_code="staging_object_gone")
    except BlobStoreError as exc:
        with session_factory() as session:
            release_lease_to_pending(session, intent_id, lease_id)
        return VerificationOutcome(kind="retry", detail=str(exc))

    # Steps 10-11: record the CommittedBlobRef and transition to finalized.
    with session_factory() as session:
        _finalize(session, intent_id, lease_id, committed)
    return VerificationOutcome(kind="finalized", committed=committed)


def reject_before_verification(session: Session, intent_id: str, rejection_code: str) -> None:
    """Reject an intent whose verification Job never ran (#22).

    The same terminal write step 2 makes for a stale authorization epoch,
    for the reconciler that settles a verification the dispatcher refused.
    """
    if rejection_code not in REJECTION_CODES:
        raise ValueError(f"unknown upload rejection code {rejection_code!r}")
    _reject_without_lease(session, intent_id, rejection_code)


def _reject_without_lease(session: Session, intent_id: str, rejection_code: str) -> None:
    """Terminal marking for step 1's admission check, which runs before the
    CAS lease (step 5) is ever acquired -- so there is no ``lease_id`` to
    match against yet."""
    context = current_context(session)
    result = session.execute(
        text(
            """
            UPDATE upload_intent_records
            SET state = 'rejected', rejection_code = :rejection_code,
                verification_lease_id = NULL, verification_lease_expires_at = NULL
            WHERE id = :intent_id AND workspace_id = :workspace_id
              AND state IN ('pending', 'verifying')
            """
        ),
        {"intent_id": intent_id, "workspace_id": context.workspace_id, "rejection_code": rejection_code},
    )
    if result.rowcount == 1:
        _enqueue_staging_cleanup(session, intent_id)
