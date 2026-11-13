"""The external-creative Worker job: gate -> asset -> tag (Issue #168).

``app.worker_runtime.WorkerRuntime._external_creative`` owns the Job
lifecycle (lease, retry, Operation folding); :func:`run_external_creative`
owns the three steps and their evidence, in short tenant transactions -- no
transaction is held open across blob or network I/O.

1. **gate** -- the committed upload goes through the Library reference gate
   (Issue #158: size, magic bytes and decode, declared-vs-decoded MIME,
   pixel limits, EXIF/GPS, content safety in the deployment's configured
   mode). A rejection is final (``rejected``); a transient read/commit
   failure retries.
2. **asset** -- the image becomes an ``AssetRecord`` (source ``external``,
   no Run) and an ``AssetVersionRecord`` (origin ``upload``, no execution
   mode -- it is customer material, never Controlled-Mock output). The
   stored master keeps the uploaded pixels; only when the original carries
   EXIF/XMP/comment metadata is it re-encoded without it (orientation
   applied first). Its 512px thumbnail comes from #159's helper; its MK
   code is a pure function of the AssetVersion id (#166).
3. **tag** -- one governed ``llm_chat`` vision call on the gate's
   controlled derivative, keyed by (Job, ``tag``): a re-run Job replays the
   recorded answer instead of paying again. An unusable answer or a refused
   call ends ``tagging_failed`` -- the asset exists and the user can still
   tag it by hand. A product with no vocabulary skips the call (nothing was
   reserved) and the creative is ``ready`` with no suggestion.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExternalCreativeRecord,
    ReferenceObjectRecord,
    new_id,
    utcnow,
)
from app.platform import audit
from app.platform.blob_store import (
    BlobStore,
    BlobStoreError,
    CommittedBlobRef,
    GeneratedArtifactSpec,
    ObjectNotFoundError,
    extension_for_mime,
)
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext
from app.providers.llm_chat import LlmChatClient, LlmChatError, LlmImage

from . import tagging
from .service import RESOURCE_TYPE, TERMINAL_STATUSES
from app.platform.runtime_role import WORKER_ROLE, runs_as

ASSET_PURPOSE = "external_creative"
TRUTH_POLICY_VERSION = "external-creative-truth-v1"
_PIL_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_METADATA_KEYS = ("exif", "xmp", "XML:com.adobe.xmp", "comment", "photoshop")
#: Common ad ratios the gallery and experiments speak in; anything else is W:H reduced.
_NAMED_RATIOS = ((1, 1), (4, 5), (5, 4), (9, 16), (16, 9), (2, 3), (3, 2), (3, 4), (4, 3), (1, 2), (2, 1))


@dataclass(frozen=True)
class CreativeOutcome:
    kind: str  # ready | retry | failed | uncertain
    code: str = ""
    detail: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(session, ctx: TenantContext, creative_id: str) -> ExternalCreativeRecord:
    record = session.get(ExternalCreativeRecord, creative_id)
    if record is None or record.workspace_id != ctx.workspace_id:
        raise LookupError(f"external creative {creative_id!r} not found in this Workspace")
    return record


def _begin(ctx: TenantContext, creative_id: str, *, status: str, step: str) -> dict:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        record.status = status
        record.current_step = step
        record.updated_at = utcnow()
        return dict((record.evidence_json or {}).get(step) or {})


def _payload(previous: dict, started: datetime, started_mono: float, **fields) -> dict:
    payload = {
        "attempts": int(previous.get("attempts") or 0) + 1,
        "started_at": started.isoformat(),
        "finished_at": _now().isoformat(),
        "duration_ms": int((time.monotonic() - started_mono) * 1000),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    return payload


def _record_step(
    ctx: TenantContext,
    creative_id: str,
    step: str,
    payload: dict,
    *,
    status: str | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
    **columns,
) -> None:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        evidence = dict(record.evidence_json or {})
        evidence[step] = payload
        record.evidence_json = evidence
        for key, value in columns.items():
            setattr(record, key, value)
        if status is not None:
            record.status = status
            if status in TERMINAL_STATUSES:
                record.current_step = None
                record.finished_at = utcnow()
            record.failure_code = failure_code
            record.failure_detail = (failure_detail or None) and failure_detail[:2000]
        record.updated_at = utcnow()
        audit.append_event(
            session,
            event_type="external_creative.step",
            resource_type=RESOURCE_TYPE,
            resource_id=creative_id,
            details={
                key: value for key, value in {
                    "step": step,
                    "outcome": payload.get("status"),
                    "code": payload.get("code"),
                    "duration_ms": payload.get("duration_ms"),
                    "attempt": payload.get("attempts"),
                }.items() if value is not None
            },
        )


def release_unused_reservation(ctx: TenantContext, creative_id: str) -> None:
    """The creative stopped before its tagging call: give the reserved maximum back."""
    from app.providers.llm_chat import release_llm_chat_reservation

    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        if record.job_id and record.execution_mode == "byok":
            if release_llm_chat_reservation(session, ctx, job_id=record.job_id, call_name=tagging.CALL_NAME):
                audit.append_event(
                    session,
                    event_type="external_creative.reservation_released",
                    resource_type=RESOURCE_TYPE,
                    resource_id=creative_id,
                    details={"reason": record.failure_code or record.status},
                )


@runs_as(WORKER_ROLE)
def mark_failed(ctx: TenantContext, creative_id: str, *, code: str, detail: str) -> None:
    """Record an unexpected handler fault (best effort)."""
    try:
        with tenant_session(ctx) as session:
            record = _load(session, ctx, creative_id)
            if record.status in TERMINAL_STATUSES:
                return
            step = record.current_step or "gate"
        _record_step(
            ctx, creative_id, step, {"status": "failed", "code": code},
            status="failed", failure_code=code, failure_detail=detail,
        )
        release_unused_reservation(ctx, creative_id)
    except Exception:  # noqa: BLE001 -- the Job row still carries the failure
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
async def run_external_creative(
    ctx: TenantContext,
    *,
    creative_id: str,
    job_id: str,
    operation_id: str,
    llm_client: LlmChatClient,
    blob_store: BlobStore,
    final_attempt: bool,
) -> CreativeOutcome:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        status = record.status
        gated = bool(record.reference_object_id) and (record.evidence_json or {}).get("gate", {}).get("status") == "succeeded"
        has_asset = bool(record.asset_version_id)
    if status in TERMINAL_STATUSES:
        return _outcome_for_status(ctx, creative_id)

    if not gated:
        outcome = await _gate_step(ctx, creative_id, blob_store, final_attempt=final_attempt)
        if outcome is not None:
            return outcome
    if not has_asset:
        outcome = await _asset_step(ctx, creative_id, blob_store, final_attempt=final_attempt)
        if outcome is not None:
            return outcome
    return await _tag_step(ctx, creative_id, llm_client, job_id=job_id, operation_id=operation_id)


def _outcome_for_status(ctx: TenantContext, creative_id: str) -> CreativeOutcome:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        if record.status in ("ready", "tagging_failed", "confirmed"):
            return CreativeOutcome("ready")
        return CreativeOutcome("failed", record.failure_code or record.status, record.failure_detail or "")


# ---------------------------------------------------------------------------
# Step 1: the reference gate
# ---------------------------------------------------------------------------


async def _gate_step(
    ctx: TenantContext, creative_id: str, blob_store: BlobStore, *, final_attempt: bool,
) -> CreativeOutcome | None:
    from app.library.reference_commands import quarantine_committed_blob
    from app.library.reference_gate_worker import run_reference_gate

    previous = _begin(ctx, creative_id, status="gating", step="gate")
    started, started_mono = _now(), time.monotonic()
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        reference, reused = quarantine_committed_blob(
            session, ctx,
            committed_key=record.upload_key,
            storage_version_id=record.upload_storage_version_id or "",
            content_sha256=record.upload_sha256,
            declared_mime=record.upload_mime_type,
            byte_size=int(record.upload_byte_size or 0),
            filename=record.filename or f"{creative_id}.{extension_for_mime(record.upload_mime_type)}",
            provenance={
                "source": "external_creative",
                "external_creative_id": creative_id,
                "upload_intent_id": record.upload_intent_id,
            },
        )
        object_id = reference.id
        record.reference_object_id = object_id
    gate = await run_reference_gate(ctx, blob_store, object_id)
    if gate.kind == "released":
        _record_step(
            ctx, creative_id, "gate",
            _payload(previous, started, started_mono, status="succeeded", reference_object_id=object_id,
                     reused_object=reused),
        )
        return None
    retryable = gate.kind in ("source_unavailable", "storage_unavailable") and gate.retryable
    if retryable and not final_attempt:
        _record_step(
            ctx, creative_id, "gate",
            _payload(previous, started, started_mono, status="retrying", code=gate.code, detail=gate.detail),
        )
        return CreativeOutcome("retry", gate.code, gate.detail)
    status = "rejected" if gate.kind == "rejected" else "failed"
    _record_step(
        ctx, creative_id, "gate",
        _payload(previous, started, started_mono, status="failed", code=gate.code, detail=gate.detail),
        status=status, failure_code=gate.code, failure_detail=gate.detail,
    )
    release_unused_reservation(ctx, creative_id)
    return CreativeOutcome("failed", gate.code, gate.detail)


# ---------------------------------------------------------------------------
# Step 2: the AssetVersion
# ---------------------------------------------------------------------------


def aspect_ratio(width: int, height: int) -> str:
    """``1:1``, ``4:5``, ``9:16`` ... when within 1%, else the reduced ``W:H``."""
    if width <= 0 or height <= 0:
        return "unknown"
    actual = width / height
    for w, h in _NAMED_RATIOS:
        if abs(actual - w / h) / (w / h) <= 0.01:
            return f"{w}:{h}"
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"[:20]


def clean_master(raw: bytes) -> tuple[bytes, str, int, int, bool]:
    """``(bytes, mime, width, height, stripped)`` of the stored master.

    The uploaded bytes are kept as they are unless they carry EXIF / XMP /
    comment metadata (camera, GPS, editing history): then the pixels are
    re-encoded in the same format without it, after applying the EXIF
    orientation so the picture looks the same. Raises on bytes Pillow
    cannot decode (the gate already decoded them, so this is defensive).
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        mime = _PIL_FORMATS.get(image.format or "", "")
        if not mime:
            raise ValueError(f"unsupported image format {image.format!r}")
        has_metadata = bool(image.getexif()) or any(key in image.info for key in _METADATA_KEYS) or bool(
            getattr(image, "text", None)
        )
        if not has_metadata:
            return raw, mime, int(image.width), int(image.height), False
        oriented = ImageOps.exif_transpose(image) or image
        icc = image.info.get("icc_profile")
        buffer = io.BytesIO()
        options: dict = {"icc_profile": icc} if icc else {}
        if mime == "image/jpeg":
            target = oriented.convert("RGB") if oriented.mode not in ("RGB", "L", "CMYK") else oriented
            target.save(buffer, format="JPEG", quality=95, subsampling=0, optimize=True, **options)
        elif mime == "image/png":
            oriented.save(buffer, format="PNG", optimize=True, **options)
        else:
            oriented.save(buffer, format="WEBP", quality=95, method=4, **options)
        width, height = oriented.size
    return buffer.getvalue(), mime, int(width), int(height), True


async def _read(blob_store: BlobStore, ref: CommittedBlobRef) -> bytes:
    cap = config.LIBRARY_MAX_REFERENCE_BYTES + 1
    chunks: list[bytes] = []
    total = 0
    stream = blob_store.open_committed(ref)
    try:
        async for chunk in stream:
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                break
    finally:
        await stream.aclose()
    return b"".join(chunks)


async def _asset_step(
    ctx: TenantContext, creative_id: str, blob_store: BlobStore, *, final_attempt: bool,
) -> CreativeOutcome | None:
    import anyio

    from app.generation.thumbnails import apply_thumbnail, commit_thumbnail_best_effort

    previous = _begin(ctx, creative_id, status="preparing_asset", step="asset")
    started, started_mono = _now(), time.monotonic()
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        source = CommittedBlobRef(
            committed_key=record.upload_key,
            storage_version_id=record.upload_storage_version_id or "",
            size_bytes=int(record.upload_byte_size or 0),
            checksum_sha256_b64=base64.b64encode(bytes.fromhex(record.upload_sha256)).decode("ascii"),
            content_type_verified=record.upload_mime_type,
        )
        filename = record.filename
        product_id = record.product_id

    def _retry_or_fail(code: str, detail: str, *, retryable: bool) -> CreativeOutcome:
        if retryable and not final_attempt:
            _record_step(ctx, creative_id, "asset", _payload(
                previous, started, started_mono, status="retrying", code=code, detail=detail,
            ))
            return CreativeOutcome("retry", code, detail)
        _record_step(
            ctx, creative_id, "asset",
            _payload(previous, started, started_mono, status="failed", code=code, detail=detail),
            status="failed", failure_code=code, failure_detail=detail,
        )
        release_unused_reservation(ctx, creative_id)
        return CreativeOutcome("failed", code, detail)

    try:
        raw = await _read(blob_store, source)
    except ObjectNotFoundError:
        return _retry_or_fail("reference_source_unavailable", "上传的原图不存在", retryable=False)
    except BlobStoreError as exc:
        return _retry_or_fail("reference_source_unavailable", f"读取原图失败: {type(exc).__name__}", retryable=True)
    if hashlib.sha256(raw).hexdigest() != source_sha(source):
        return _retry_or_fail("reference_source_unavailable", "原图内容与上传校验和不一致", retryable=False)
    try:
        master, mime, width, height, stripped = await anyio.to_thread.run_sync(clean_master, raw)
    except Exception as exc:  # noqa: BLE001 -- the gate decoded it; this is defensive
        return _retry_or_fail("image_undecodable", f"{type(exc).__name__}: {exc}"[:300], retryable=False)

    digest = hashlib.sha256(master)
    key = f"committed/{ctx.workspace_id}/{ASSET_PURPOSE}/{digest.hexdigest()}.{extension_for_mime(mime)}"
    try:
        with tempfile.TemporaryDirectory(prefix="external-creative-") as tmp:
            path = Path(tmp) / "master"
            path.write_bytes(master)
            committed = await blob_store.commit_generated_artifact(
                path,
                GeneratedArtifactSpec(
                    committed_key=key,
                    content_type=mime,
                    expected_sha256_b64=base64.b64encode(digest.digest()).decode("ascii"),
                ),
            )
    except BlobStoreError as exc:
        return _retry_or_fail("reference_storage_unavailable", f"保存图片失败: {type(exc).__name__}", retryable=True)
    thumbnail = await commit_thumbnail_best_effort(
        blob_store, ctx.workspace_id, master, log_context={"external_creative_id": creative_id},
    )

    ratio = aspect_ratio(width, height)
    truth_evidence = {
        "determination": "external_creative_no_truth_package",
        "policy": TRUTH_POLICY_VERSION,
    }
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        if record.asset_version_id:  # a concurrent attempt already finished this step
            return None
        asset = AssetRecord(
            id=new_id("asset"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            run_id=None,
            source="external",
            combo_id="external",
            combo_code="external",
            tags_json={},
            filename=(filename or Path(key).name)[:500],
            provider="external",
            model="",
            prompt="",
            fingerprint=digest.hexdigest(),
            status="ok",
        )
        session.add(asset)
        session.flush()
        version = AssetVersionRecord(
            id=new_id("av"),
            workspace_id=ctx.workspace_id,
            asset_id=asset.id,
            kind="external",
            aspect_ratio=ratio,
            object_key=committed.committed_key,
            committed_object_key=committed.committed_key,
            committed_storage_version_id=committed.storage_version_id,
            content_sha256=digest.hexdigest(),
            byte_size=committed.size_bytes or len(master),
            mime_type=committed.content_type_verified or mime,
            origin_kind="upload",
            execution_mode=None,
            seed_unavailable_reason="external_creative",
            truth_dependency_expected=False,
            truth_dependency_policy_version=TRUTH_POLICY_VERSION,
            truth_dependency_evidence_json=truth_evidence,
            truth_dependency_evidence_hash=canonical_hash(truth_evidence),
        )
        apply_thumbnail(version, thumbnail)
        session.add(version)
        session.flush()
        record.asset_id = asset.id
        record.asset_version_id = version.id
        asset_id, version_id = asset.id, version.id
    _record_step(ctx, creative_id, "asset", _payload(
        previous, started, started_mono, status="succeeded",
        asset_id=asset_id, asset_version_id=version_id, mime_type=mime, width=width, height=height,
        aspect_ratio=ratio, byte_size=len(master), content_sha256=digest.hexdigest(),
        metadata_stripped=stripped, thumbnail=thumbnail is not None,
    ))
    return None


def source_sha(ref: CommittedBlobRef) -> str:
    return base64.b64decode(ref.checksum_sha256_b64).hex()


# ---------------------------------------------------------------------------
# Step 3: tagging
# ---------------------------------------------------------------------------


def _derivative_ref(session, ctx: TenantContext, object_id: str) -> CommittedBlobRef | None:
    reference = session.get(ReferenceObjectRecord, object_id)
    if (
        reference is None or reference.workspace_id != ctx.workspace_id
        or reference.state != "released" or not reference.derivative_object_key
    ):
        return None
    return CommittedBlobRef(
        committed_key=reference.derivative_object_key,
        storage_version_id=reference.derivative_storage_version_id or "",
        size_bytes=int(reference.derivative_byte_size or 0),
        checksum_sha256_b64=(
            base64.b64encode(bytes.fromhex(reference.derivative_sha256)).decode("ascii")
            if reference.derivative_sha256 else ""
        ),
        content_type_verified=reference.derivative_mime or "image/jpeg",
    )


async def _tag_step(
    ctx: TenantContext, creative_id: str, llm_client: LlmChatClient, *, job_id: str, operation_id: str,
) -> CreativeOutcome:
    previous = _begin(ctx, creative_id, status="tagging", step="tag")
    started, started_mono = _now(), time.monotonic()
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        vocabulary = dict(record.vocabulary_json or {})
        mode = record.execution_mode
        derivative = _derivative_ref(session, ctx, record.reference_object_id or "")

    if not vocabulary.get("factors"):
        _record_step(
            ctx, creative_id, "tag",
            _payload(previous, started, started_mono, status="skipped", reason="no_vocabulary"),
            status="ready",
        )
        return CreativeOutcome("ready")
    if derivative is None:
        _record_step(
            ctx, creative_id, "tag",
            _payload(previous, started, started_mono, status="failed", code="reference_source_unavailable"),
            status="tagging_failed", failure_code="reference_source_unavailable",
            failure_detail="受控派生不可用，无法打标",
        )
        release_unused_reservation(ctx, creative_id)
        return CreativeOutcome("ready")

    request = tagging.build_request(vocabulary, LlmImage.from_blob(derivative))
    try:
        result = await llm_client.chat(
            ctx, job_id=job_id, operation_id=operation_id, call_name=tagging.CALL_NAME,
            request=request, execution_mode=mode,
        )
    except LlmChatError as error:
        payload = _payload(
            previous, started, started_mono, status="failed", code=error.code,
            retryable=error.retryable, dispatch_certainty=error.dispatch_certainty, attempt_id=error.attempt_id,
        )
        _record_step(
            ctx, creative_id, "tag", payload,
            status="tagging_failed", failure_code=error.code, failure_detail=error.detail,
        )
        if error.dispatch_certainty == "uncertain":
            return CreativeOutcome("uncertain", error.code, error.detail)
        # Only a reservation no attempt consumed is released (a billed
        # answer that failed validation is settled by the coordinator).
        release_unused_reservation(ctx, creative_id)
        return CreativeOutcome("ready")

    call = {
        "attempt_id": result.attempt_id,
        "provider": result.provider,
        "model": result.model,
        "model_version": result.model_version,
        "execution_mode": result.execution_mode,
        "usage": result.usage,
        "cost": result.cost,
        "replayed": result.replayed,
    }
    suggestion = {**tagging.normalize(result.data, vocabulary), "call": call}
    _record_step(
        ctx, creative_id, "tag",
        _payload(previous, started, started_mono, status="succeeded",
                 suggested=sum(1 for item in suggestion["factors"] if item["value_id"]), **call),
        status="ready", suggestion_json=suggestion,
    )
    return CreativeOutcome("ready")


__all__ = [
    "CreativeOutcome",
    "aspect_ratio",
    "clean_master",
    "mark_failed",
    "release_unused_reservation",
    "run_external_creative",
]
