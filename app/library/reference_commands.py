"""Reference creation, gate evidence, download and promotion glue (Gate 0.8, contract §7.2).

Operations:

1. **create_reference_from_upload_intent** — consumes a finalized Upload
   Intent, creates a quarantined ReferenceObject identity, enqueues the gate
   Job + Operation, and returns an ``OperationAccepted`` dict (202).
   The actual gate checks run in the Worker (``library.reference_gate``),
   which reads the committed original through the ``BlobStore`` and writes
   the controlled derivative back through it (Issue #158) -- this module
   never touches the blob bytes inside the request.

2. **get_reference_object** — Workspace-scoped read of one object's gate
   evidence: state, per-check results and the stable ``failure_code`` the
   browser renders ("内容安全服务未配置" vs "门禁未通过") after the gate
   Operation fails.

3. **resolve_reference_derivative** — resolves a Version ID to its released
   derivative as a ``CommittedBlobRef`` (production) or a local path (dev
   sync-upload path), so the route can issue a short-lived signed download
   capability through the same ``BlobStore`` seam generated assets use.

Invariants:
- Upload Intent must be ``finalized``, purpose ``reference_asset``, same
  Workspace.
- A consumed Upload Intent (already created a Reference via its committed
  key) returns ``upload_intent_consumed`` without duplication.
- Reference objects start quarantined; only the Worker gate path releases.
- The content-safety execution mode is never read from the request: the
  Worker resolves it from deployment configuration
  (``references.resolve_safety_mode``).
- Download resolves the derivative via the Version's content; the signed URL
  is never stored in Library content or events.
- Initiator/executor attribution is preserved across the Worker boundary.
"""
from __future__ import annotations

import base64
from pathlib import Path

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    LibraryVersionRecord,
    ReferenceObjectRecord,
    new_id,
    utcnow,
)
from ..platform.blob_store import CommittedBlobRef
from ..platform.tenancy import TenantContext
from . import service
from .contracts import ENTRY_REFERENCE_ASSET
from .references import (
    STATE_RELEASED,
    STATE_QUARANTINED,
    STATE_REJECTED,
    FAILURE_CONTENT_SAFETY_UNAVAILABLE,
    gate_failure,
    is_blob_object_key,
)
from .service import (
    LibraryConflict,
    LibraryError,
    LibraryNotFound,
)


class ReferenceCommandError(LibraryError):
    pass


def _sha256_hex_from_b64(checksum_b64: str) -> str:
    """The Upload Intent carries the Worker-verified SHA-256 as base64; the
    reference object keeps hex (what the gate compares against)."""
    try:
        digest = base64.b64decode(checksum_b64, validate=True)
    except (ValueError, TypeError):
        raise ReferenceCommandError("Upload Intent 的 checksum 不是合法的 base64")
    if len(digest) != 32:
        raise ReferenceCommandError("Upload Intent 的 checksum 不是 SHA-256")
    return digest.hex()


def create_reference_from_upload_intent(
    context: TenantContext,
    *,
    upload_intent_id: str,
    filename: str = "",
    declared_mime: str = "",
    rights: dict | None = None,
    idempotency_key: str,
) -> dict:
    """Consume a finalized Upload Intent → quarantined Reference + gate Job.

    Returns a dict suitable for ``OperationAccepted`` serialization.

    ``rights`` is the uploader's rights declaration (``scope``/``valid_to``/
    optional ``evidence``); it is normalized server-side (``declared_by`` is
    always the current initiator) and stored on the object so the promote
    command can publish the ReferenceAssetVersion without raw JSON.
    """
    from ..platform.jobs import enqueue_in_session
    from ..platform.operations import create_operation
    from ..platform.upload_intents import (
        UploadIntentNotFoundError,
        get_upload_intent,
    )

    declaration = service.normalize_rights_declaration(
        rights, declared_by=context.initiator_id or "",
    )

    with tenant_session(context) as session:
        try:
            intent = get_upload_intent(session, upload_intent_id)
        except UploadIntentNotFoundError:
            raise LibraryNotFound("Upload Intent 不存在或不属于当前工作区", code="upload_intent_not_found")

        if intent.state != "finalized":
            raise ReferenceCommandError(
                f"Upload Intent 状态为 {intent.state}，需要 finalized"
            )

        if intent.purpose != "reference_asset":
            raise ReferenceCommandError(
                f"Upload Intent purpose 为 {intent.purpose}，需要 reference_asset"
            )

        committed_key = intent.committed_blob_ref_key or ""
        committed_version = intent.committed_blob_ref_version or ""

        if not committed_key:
            raise ReferenceCommandError("Upload Intent 没有 committed blob ref")

        existing_ref = (
            session.query(ReferenceObjectRecord)
            .filter(
                ReferenceObjectRecord.workspace_id == context.workspace_id,
                ReferenceObjectRecord.original_object_key == committed_key,
            )
            .one_or_none()
        )
        if existing_ref is not None:
            raise LibraryConflict("upload_intent_consumed", code="upload_intent_consumed")

        resolved_mime = (declared_mime or intent.content_type or "").strip().lower()
        resolved_filename = filename or intent.filename_original or ""

        # The finalized intent's checksum was verified against the committed
        # bytes by the Worker (spec §7.8); deriving the content hash from it
        # keeps the request off the blob store entirely. The exact byte size
        # is re-measured by the gate; ``content_length_max`` (the browser
        # sends min == max == file.size) is the provisional value.
        digest = _sha256_hex_from_b64(intent.checksum_sha256_b64)
        provisional_size = int(intent.content_length_max or 0)
        if provisional_size <= 0:
            raise ReferenceCommandError("committed blob 为空")

        same_content = (
            session.query(ReferenceObjectRecord)
            .filter(
                ReferenceObjectRecord.workspace_id == context.workspace_id,
                ReferenceObjectRecord.content_sha256 == digest,
            )
            .one_or_none()
        )
        if same_content is not None:
            if same_content.state == STATE_REJECTED:
                raise ReferenceCommandError(
                    "该文件此前已被上传门禁拒绝，不接受重复上传"
                )
            # Same bytes already live in this Workspace (§7.1 object reuse):
            # re-run (or short-circuit) the gate on the existing identity
            # instead of violating the (workspace, sha256) uniqueness. A new
            # rights declaration supersedes the stored one.
            record = same_content
            if declaration is not None:
                meta = dict(record.checks_json or {})
                meta["rights_declaration"] = declaration
                record.checks_json = meta
                record.updated_at = utcnow()
        else:
            meta = {"filename": Path(resolved_filename).name, "checks": []}
            if declaration is not None:
                meta["rights_declaration"] = declaration
            record = ReferenceObjectRecord(
                id=new_id("refobj"),
                workspace_id=context.workspace_id,
                content_sha256=digest,
                original_object_key=committed_key,
                original_storage_version_id=committed_version,
                declared_mime=resolved_mime,
                mime_type="",
                byte_size=provisional_size,
                state=STATE_QUARANTINED,
                checks_json=meta,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(record)
        session.flush()

        operation = create_operation(
            session,
            command_type="reference_gate",
            idempotency_key_hash=service.compute_fingerprint(
                {"key": idempotency_key}
            ),
        )

        enqueue_in_session(
            session,
            context=context,
            job_type="library.reference_gate",
            payload={
                "object_id": record.id,
                "operation_id": operation.id,
                "upload_intent_id": upload_intent_id,
            },
            idempotency_key=f"reference_gate:{idempotency_key}",
            resource_type="reference_object",
            resource_id=record.id,
            max_attempts=3,
        )

    return {
        "operation_id": operation.id,
        "state": "queued",
        "command_type": "reference_gate",
    }


def get_reference_object(context: TenantContext, object_id: str) -> dict:
    """Workspace-scoped gate evidence for one reference object.

    A cross-Workspace id resolves exactly like an unknown one (``LibraryNotFound``).
    Never exposes object keys or signed URLs -- only facts and check results.
    """
    with tenant_session(context) as session:
        record = session.get(ReferenceObjectRecord, object_id)
        if record is None or record.workspace_id != context.workspace_id:
            raise LibraryNotFound("参考对象不存在", code="reference_object_not_found")
        meta = dict(record.checks_json or {})
        failure_code, failure_detail = gate_failure(record)
        if record.state == STATE_RELEASED:
            failure_code, failure_detail = None, None
        derivative = None
        if record.state == STATE_RELEASED and record.derivative_object_key:
            derivative = {
                "mime_type": record.derivative_mime,
                "byte_size": record.derivative_byte_size,
                "width": record.derivative_width,
                "height": record.derivative_height,
            }
        return {
            "object_id": record.id,
            "state": record.state,
            "execution_mode": record.execution_mode,
            "filename": str(meta.get("filename") or ""),
            "declared_mime": record.declared_mime,
            "mime_type": record.mime_type,
            "byte_size": record.byte_size,
            "width": record.width,
            "height": record.height,
            "content_sha256": record.content_sha256,
            "checks": list(meta.get("checks") or []),
            "failure_code": failure_code,
            "failure_detail": failure_detail,
            "content_safety_unavailable": failure_code == FAILURE_CONTENT_SAFETY_UNAVAILABLE,
            "rights_declared": isinstance(meta.get("rights_declaration"), dict),
            "promoted_entry_id": record.promoted_entry_id,
            "derivative": derivative,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }


def resolve_reference_derivative(
    context: TenantContext,
    *,
    version_id: str,
) -> dict:
    """Resolve a published Reference Asset Version to its released derivative.

    Returns the derivative file facts plus either ``committed_ref`` (a
    ``CommittedBlobRef`` into the BlobStore -- the production path) or
    ``local_path`` (the dev sync-upload path's filesystem derivative). The
    browser supplies a Version ID, never an object key.
    """
    with tenant_session(context) as session:
        version = session.get(LibraryVersionRecord, version_id)
        if version is None or version.workspace_id != context.workspace_id:
            raise LibraryNotFound("版本不存在")

        if version.entry_type != ENTRY_REFERENCE_ASSET:
            raise ReferenceCommandError("版本不是参考素材类型")

        if version.state == "withdrawn":
            raise LibraryConflict("版本已撤回，不可下载", code="library_version_withdrawn")

        content = version.content_json or {}
        object_id = content.get("object_id")
        if not object_id:
            raise ReferenceCommandError("版本内容缺少 object_id")

        record = session.get(ReferenceObjectRecord, object_id)
        if record is None or record.workspace_id != context.workspace_id:
            raise LibraryNotFound("参考对象不存在", code="reference_object_not_found")

        if record.state != STATE_RELEASED:
            raise ReferenceCommandError(
                f"参考对象状态为 {record.state}，只有 released 才可下载"
            )

        if not record.derivative_object_key:
            raise ReferenceCommandError("参考对象没有受控派生，无法下载")

        derivative_key = record.derivative_object_key
        derivative_mime = record.derivative_mime or "image/jpeg"
        file_meta = content.get("file") if isinstance(content.get("file"), dict) else {}
        filename = (
            str(file_meta.get("filename") or "")
            or str((record.checks_json or {}).get("filename") or "")
            or Path(derivative_key).name
        )
        if not filename.lower().endswith(".jpg"):
            filename = f"{Path(filename).stem or 'reference'}.jpg"

        committed_ref = None
        local_path = None
        if is_blob_object_key(derivative_key):
            committed_ref = CommittedBlobRef(
                committed_key=derivative_key,
                storage_version_id=record.derivative_storage_version_id,
                size_bytes=record.derivative_byte_size,
                checksum_sha256_b64=base64.b64encode(
                    bytes.fromhex(record.derivative_sha256)
                ).decode("ascii") if record.derivative_sha256 else "",
                content_type_verified=derivative_mime,
            )
        else:
            local_path = derivative_key

        return {
            "derivative_object_key": derivative_key,
            "derivative_mime": derivative_mime,
            "derivative_byte_size": record.derivative_byte_size,
            "filename": filename,
            "object_id": record.id,
            "version_id": version_id,
            "committed_ref": committed_ref,
            "local_path": local_path,
        }


def quarantine_committed_blob(
    session,
    context: TenantContext,
    *,
    committed_key: str,
    storage_version_id: str,
    content_sha256: str,
    declared_mime: str,
    byte_size: int,
    filename: str = "",
    provenance: dict | None = None,
) -> tuple[ReferenceObjectRecord, bool]:
    """A quarantined ReferenceObject for a blob the server already committed.

    Product intake (Issue #161) gates images that never had a browser Upload
    Intent of purpose ``reference_asset``: product-page images the Worker
    downloaded and committed itself, and ``product_intake_image`` uploads.
    Same identity rules as :func:`create_reference_from_upload_intent`:
    identical bytes in this Workspace reuse the existing object (returned
    with ``reused=True``; a previously *rejected* one stays rejected), and a
    new object starts ``quarantined`` until ``run_reference_gate`` decides.
    ``provenance`` (source, URL, intake id) is kept on the object's checks
    for the audit trail. Runs in the caller's tenant transaction.
    """
    if byte_size <= 0:
        raise ReferenceCommandError("committed blob 为空")
    existing = (
        session.query(ReferenceObjectRecord)
        .filter(
            ReferenceObjectRecord.workspace_id == context.workspace_id,
            ReferenceObjectRecord.content_sha256 == content_sha256,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing, True
    meta: dict = {"filename": Path(filename or "").name, "checks": []}
    if provenance:
        meta["provenance"] = dict(provenance)
    record = ReferenceObjectRecord(
        id=new_id("refobj"),
        workspace_id=context.workspace_id,
        content_sha256=content_sha256,
        original_object_key=committed_key,
        original_storage_version_id=storage_version_id or "",
        declared_mime=(declared_mime or "").strip().lower(),
        mime_type="",
        byte_size=int(byte_size),
        state=STATE_QUARANTINED,
        checks_json=meta,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(record)
    session.flush()
    return record, False


def create_reference_download_intent(
    context: TenantContext,
    *,
    version_id: str,
) -> dict:
    """Backward-compatible name for :func:`resolve_reference_derivative`."""
    return resolve_reference_derivative(context, version_id=version_id)


async def materialize_local_derivative(
    context: TenantContext,
    blob_store,
    *,
    object_id: str,
) -> CommittedBlobRef:
    """Commit a dev sync-upload-path derivative (a ``LIBRARY_OBJECT_DIR`` file)
    into the BlobStore so it can be served through the same signed-link seam.

    The production path (Worker gate) always commits through the BlobStore
    already; this is only reached for objects released by the synchronous
    ``service.upload_reference_object`` path. Content-addressed and
    byte-identical, so repeating it is safe; the record is re-pointed at the
    committed key once, after which ``resolve_reference_derivative`` returns a
    ``committed_ref`` directly.
    """
    from ..platform.blob_store import GeneratedArtifactSpec
    from .references import derivative_object_key

    with tenant_session(context) as session:
        record = session.get(ReferenceObjectRecord, object_id)
        if record is None or record.workspace_id != context.workspace_id:
            raise LibraryNotFound("参考对象不存在", code="reference_object_not_found")
        if record.state != STATE_RELEASED or not record.derivative_object_key:
            raise ReferenceCommandError("参考对象没有受控派生，无法下载")
        if is_blob_object_key(record.derivative_object_key):
            return CommittedBlobRef(
                committed_key=record.derivative_object_key,
                storage_version_id=record.derivative_storage_version_id,
                size_bytes=record.derivative_byte_size,
                checksum_sha256_b64=base64.b64encode(
                    bytes.fromhex(record.derivative_sha256)
                ).decode("ascii"),
                content_type_verified=record.derivative_mime or "image/jpeg",
            )
        local_path = Path(record.derivative_object_key)
        key = derivative_object_key(context.workspace_id, record.content_sha256)
        spec = GeneratedArtifactSpec(
            committed_key=key,
            content_type=record.derivative_mime or "image/jpeg",
            expected_sha256_b64=base64.b64encode(
                bytes.fromhex(record.derivative_sha256)
            ).decode("ascii"),
        )
    if not local_path.exists():
        raise ReferenceCommandError("受控派生文件不存在，无法下载")
    committed = await blob_store.commit_generated_artifact(local_path, spec)
    with tenant_session(context) as session:
        record = session.get(ReferenceObjectRecord, object_id)
        if record is not None and record.workspace_id == context.workspace_id:
            record.derivative_object_key = committed.committed_key
            record.derivative_storage_version_id = committed.storage_version_id
            record.updated_at = utcnow()
            session.flush()
    return committed
