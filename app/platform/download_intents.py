"""DownloadIntent: an immutable, per-click audit record for a signed
download capability (spec §7.12).

``create_download_intent`` is POST, not GET, because it (1) creates an
immutable ``DownloadIntentRecord`` audit row, (2) is a non-safe request that
goes through Origin/CSRF/session verification at the BFF, and (3) each user
click produces a new intent with a fresh signed capability. Idempotent
replay (same ``Idempotency-Key``) returns the same record and re-issues a
signed URL against the same ``CommittedBlobRef``, capped at the original
``expires_at`` -- never a new record.

The ORM mapping (``DownloadIntentRecord``) lives in
``app.adapters.persistence.models``, matching the pattern
``app.platform.operations``/``app.platform.upload_intents`` set for their
own records.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..adapters.persistence.models import DownloadIntentRecord, new_id, utcnow
from .blob_store import CommittedBlobRef
from .idempotency import create_or_replay_intent
from .tenancy import current_context

__all__ = [
    "DownloadIntentRecord",
    "DEFAULT_DOWNLOAD_TTL_SECONDS",
    "DownloadIntentNotFoundError",
    "get_download_intent",
    "create_download_intent_record",
    "create_or_replay_download_intent",
]

#: DownloadIntentRecord (spec §7.12) does not carry a `purpose`, so it has no
#: PURPOSE_POLICIES-derived TTL the way an UploadIntent does -- a single
#: default applies to every download capability.
DEFAULT_DOWNLOAD_TTL_SECONDS = 300


class DownloadIntentNotFoundError(LookupError):
    """No DownloadIntent with this id exists inside the caller's Workspace."""


def get_download_intent(session: Session, intent_id: str) -> DownloadIntentRecord:
    context = current_context(session)
    record = session.get(DownloadIntentRecord, intent_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise DownloadIntentNotFoundError(intent_id)
    return record


def create_download_intent_record(
    session: Session,
    *,
    intent_id: str,
    owner_kind: str,
    owner_id: str,
    committed_blob_ref_key: str,
    committed_blob_ref_version: str,
    ttl_seconds: int = DEFAULT_DOWNLOAD_TTL_SECONDS,
) -> DownloadIntentRecord:
    context = current_context(session)
    now = utcnow()
    record = DownloadIntentRecord(
        id=intent_id,
        workspace_id=context.workspace_id,
        initiator_id=context.initiator_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        committed_blob_ref_key=committed_blob_ref_key,
        committed_blob_ref_version=committed_blob_ref_version,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(record)
    session.flush()
    return record


def create_or_replay_download_intent(
    session: Session,
    *,
    idempotency_key: str,
    command_type: str,
    fingerprint_hash: str,
    owner_kind: str,
    owner_id: str,
    committed_ref: CommittedBlobRef,
    ttl_seconds: int = DEFAULT_DOWNLOAD_TTL_SECONDS,
) -> DownloadIntentRecord:
    """Idempotent DownloadIntent creation for a server-resolved CommittedBlobRef.

    Wraps :func:`app.platform.idempotency.create_or_replay_intent` (spec
    §5.4's sync resource path) around :func:`create_download_intent_record`,
    so every caller that already knows the exact ``CommittedBlobRef`` to
    grant against (Issue #49 Task 2's AssetVersion download-intent route,
    Task 9's export-package download-intent route) gets the same
    idempotent-replay guarantee the platform's reference-object
    ``/download-intents`` route has (``app.main.create_download_intent_endpoint``)
    without duplicating its create/replay closures per caller.

    A replayed call still re-resolves ``committed_ref`` in the caller before
    reaching here (spec §0.1: "Intent creation ... revalidate the live
    storage revision") -- this function only makes *persisting the intent
    metadata* idempotent, not the resolution step that precedes it.
    """

    def _create(inner_session: Session) -> tuple[str, str, DownloadIntentRecord]:
        intent_id = new_id("di")
        record = create_download_intent_record(
            inner_session,
            intent_id=intent_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            committed_blob_ref_key=committed_ref.committed_key,
            committed_blob_ref_version=committed_ref.storage_version_id,
            ttl_seconds=ttl_seconds,
        )
        return ("download_intent", intent_id, record)

    def _replay(
        _resource_type: str, resource_id: str, inner_session: Session
    ) -> DownloadIntentRecord:
        return get_download_intent(inner_session, resource_id)

    return create_or_replay_intent(
        session,
        idempotency_key=idempotency_key,
        command_type=command_type,
        fingerprint_hash=fingerprint_hash,
        create_fn=_create,
        replay_fn=_replay,
    )
