"""Worker-side reference gate: bytes in from the BlobStore, derivative out to it.

``app.worker_runtime._library_reference_gate`` owns the Job/Operation
lifecycle; this module owns everything between -- reading the committed
original, running the pure gate checks (``app.library.references``),
committing the controlled derivative through the same ``BlobStore`` seam
generated assets use, and writing the evidence (record + audit) in short
tenant transactions. No step here holds a database transaction open across
blob I/O. Each decision is written under the object's row lock, and only
while the object is still ``quarantined``, so two runs of one gate decide it
once and a rerun after a crash reports the decision already made (#22).

Outcome kinds (``GateOutcome.kind``):

- ``released``    -- all checks passed, derivative committed, object released;
- ``rejected``    -- a check failed; original purged (or purge recorded as
                     pending), object ``rejected``;
- ``unavailable`` -- the deployment selected ``real`` content safety but no
                     adapter is wired; object stays ``quarantined`` with the
                     stable ``content_safety_unavailable`` failure code;
- ``source_unavailable`` -- the original could not be read (``retryable``
                     says whether a later attempt may succeed);
- ``storage_unavailable`` -- the checks passed but the derivative could not
                     be committed (always retryable; nothing was decided).

Each outcome carries the stable ``code`` the Operation/Job/audit/UI share.
"""
from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select

from .. import config
from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import ReferenceObjectRecord
from ..platform.blob_store import (
    BlobRef,
    BlobStore,
    BlobStoreError,
    CommittedBlobRef,
    GeneratedArtifactSpec,
    ObjectNotFoundError,
)
from ..platform.tenancy import TenantContext
from . import references
from .service import _audit

#: Stream granularity cap: the gate never buffers more than the hard byte
#: limit plus one chunk, however large the committed object is (the purpose
#: policy allows uploads the Library gate must still refuse).
_READ_CAP_SLACK = 1


@dataclass(frozen=True)
class GateOutcome:
    kind: Literal[
        "released", "rejected", "unavailable", "source_unavailable", "storage_unavailable"
    ]
    object_id: str
    code: str = ""
    detail: str = ""
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.kind == "released"


@dataclass(frozen=True)
class _Source:
    object_id: str
    key: str
    storage_version_id: str
    content_sha256: str
    declared_mime: str
    byte_size: int


async def _read_original(blob_store: BlobStore, source: _Source) -> bytes:
    """Read the original's bytes, capped just past the hard limit."""
    cap = config.LIBRARY_MAX_REFERENCE_BYTES + _READ_CAP_SLACK
    if references.is_blob_object_key(source.key):
        ref = CommittedBlobRef(
            committed_key=source.key,
            storage_version_id=source.storage_version_id,
            size_bytes=source.byte_size,
            checksum_sha256_b64=base64.b64encode(
                bytes.fromhex(source.content_sha256)
            ).decode("ascii") if source.content_sha256 else "",
            content_type_verified=source.declared_mime,
        )
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
    # Dev-only sync-upload path: the original lives in LIBRARY_OBJECT_DIR.
    path = Path(source.key)
    with path.open("rb") as handle:
        return handle.read(cap)


async def _purge_original(blob_store: BlobStore, source: _Source) -> str:
    """Delete a rejected original; returns an error string ('' on success)."""
    if references.is_blob_object_key(source.key):
        try:
            await blob_store.delete(BlobRef(key=source.key))
        except BlobStoreError as exc:
            return f"{type(exc).__name__}: {exc}"
        return ""
    try:
        Path(source.key).unlink()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


async def _commit_derivative(
    blob_store: BlobStore,
    context: TenantContext,
    source: _Source,
    derivative: references.ControlledDerivative,
) -> CommittedBlobRef:
    key = references.derivative_object_key(context.workspace_id, source.content_sha256)
    with tempfile.TemporaryDirectory(prefix="refderiv-") as tmp:
        path = Path(tmp) / "derivative.jpg"
        path.write_bytes(derivative.raw)
        return await blob_store.commit_generated_artifact(
            path,
            GeneratedArtifactSpec(
                committed_key=key,
                content_type=derivative.mime,
                expected_sha256_b64=base64.b64encode(
                    bytes.fromhex(derivative.sha256)
                ).decode("ascii"),
            ),
        )


def _checks_detail(checks: list[references.GateCheck]) -> list[dict]:
    return [check.as_dict() for check in checks]


def _lock_gate_target(session, context: TenantContext, object_id: str) -> ReferenceObjectRecord:
    """``load_gate_target`` under ``SELECT ... FOR UPDATE``, as committed now.

    Every write that decides the object goes through here and first checks
    the object is still ``quarantined``: two runs of one gate (a lease that
    expired mid-check, or the same bytes uploaded twice) serialize on the
    row, and the second finds the decision already made. SQLite ignores the
    clause and serializes writers itself.
    """
    record = session.scalars(
        select(ReferenceObjectRecord)
        .where(
            ReferenceObjectRecord.id == object_id,
            ReferenceObjectRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if record is None:
        raise references.ReferenceGateError("参考对象不存在", code="reference_object_not_found")
    return record


def _decided_outcome(record: ReferenceObjectRecord) -> GateOutcome | None:
    """The outcome an earlier run already committed, or ``None`` if undecided."""
    if record.state == references.STATE_RELEASED:
        return GateOutcome("released", record.id)
    if record.state == references.STATE_REJECTED:
        code, detail = references.gate_failure(record)
        return GateOutcome(
            "rejected", record.id,
            code=code or references.FAILURE_GATE_REJECTED,
            detail=detail or "该对象此前已被上传门禁拒绝",
        )
    return None


async def run_reference_gate(
    context: TenantContext,
    blob_store: BlobStore,
    object_id: str,
    *,
    execution_mode: str = "",
    safety_scenario: str = "",
) -> GateOutcome:
    """Gate one quarantined object end to end; see the module docstring.

    ``execution_mode``/``safety_scenario`` exist for tests only -- the Worker
    passes nothing and the deployment configuration decides
    (``references.resolve_safety_mode``).
    """
    mode, scenario = references.resolve_safety_mode(execution_mode, safety_scenario)

    with tenant_session(context) as session:
        record = references.load_gate_target(session, context, object_id)
        decided = _decided_outcome(record)
        if decided is not None:
            return decided
        if not record.original_object_key or record.original_purged:
            return GateOutcome(
                "source_unavailable", object_id,
                code=references.FAILURE_SOURCE_UNAVAILABLE,
                detail="原件已清除，无法重跑门禁；需要重新上传一个未被拒绝的文件",
            )
        source = _Source(
            object_id=record.id,
            key=record.original_object_key,
            storage_version_id=record.original_storage_version_id or "",
            content_sha256=record.content_sha256,
            declared_mime=record.declared_mime,
            byte_size=record.byte_size,
        )

    try:
        raw = await _read_original(blob_store, source)
    except (ObjectNotFoundError, FileNotFoundError):
        detail = "committed blob 不存在"
        with tenant_session(context) as session:
            # A run that rejected the object purged its original; that is
            # the decision to report, not the missing bytes it left behind.
            decided = _decided_outcome(_lock_gate_target(session, context, object_id))
            if decided is not None:
                return decided
            _audit(session, context, "reference_gate", result="blocked", detail={
                "object_id": object_id, "execution_mode": mode,
                "failure_code": references.FAILURE_SOURCE_UNAVAILABLE, "reason": detail,
            })
        return GateOutcome(
            "source_unavailable", object_id,
            code=references.FAILURE_SOURCE_UNAVAILABLE, detail=detail,
        )
    except BlobStoreError as exc:
        # Transient storage trouble: nothing was decided about the content.
        return GateOutcome(
            "source_unavailable", object_id,
            code=references.FAILURE_SOURCE_UNAVAILABLE,
            detail=f"读取原件失败: {type(exc).__name__}", retryable=True,
        )

    try:
        evaluation = references.evaluate_gate(
            raw,
            expected_sha256=source.content_sha256,
            declared_mime=source.declared_mime,
            mode=mode,
            scenario=scenario,
        )
    except references.ContentSafetyUnavailable as exc:
        with tenant_session(context) as session:
            record = _lock_gate_target(session, context, object_id)
            decided = _decided_outcome(record)
            if decided is not None:
                return decided
            record.byte_size = len(raw) or record.byte_size
            references.record_gate_unavailable(record, exc, mode=mode)
            _audit(session, context, "reference_gate", result="unavailable", detail={
                "object_id": object_id,
                "execution_mode": mode,
                "checks": _checks_detail(exc.checks),
                "failure_code": exc.failure_code,
                "reason": str(exc),
            })
            session.flush()
        return GateOutcome("unavailable", object_id, code=exc.failure_code, detail=str(exc))

    if not evaluation.passed:
        purge_error = await _purge_original(blob_store, source)
        with tenant_session(context) as session:
            record = _lock_gate_target(session, context, object_id)
            decided = _decided_outcome(record)
            if decided is not None:
                return decided
            record.byte_size = len(raw) or record.byte_size
            reason = references.record_gate_rejection(record, evaluation)
            references.mark_original_purged(record, purge_error)
            _audit(session, context, "reference_gate", result="blocked", detail={
                "object_id": object_id,
                "execution_mode": mode,
                "checks": _checks_detail(evaluation.checks),
                "failed_checks": evaluation.failed_checks,
                "failure_code": references.FAILURE_GATE_REJECTED,
                "original_purged": not purge_error,
                "purge_error": purge_error,
                "reason": reason,
            })
            session.flush()
        return GateOutcome(
            "rejected", object_id, code=references.FAILURE_GATE_REJECTED, detail=reason,
        )

    assert evaluation.image is not None
    derivative = references.build_controlled_derivative(evaluation.image)
    try:
        committed = await _commit_derivative(blob_store, context, source, derivative)
    except BlobStoreError as exc:
        # The checks passed but the derivative could not be written; nothing
        # about the object changed, so a later attempt re-evaluates and
        # re-commits the byte-identical derivative.
        return GateOutcome(
            "storage_unavailable", object_id,
            code=references.FAILURE_STORAGE_UNAVAILABLE,
            detail=f"写入受控派生失败: {type(exc).__name__}", retryable=True,
        )

    with tenant_session(context) as session:
        record = _lock_gate_target(session, context, object_id)
        decided = _decided_outcome(record)
        if decided is not None:
            return decided
        record.byte_size = len(raw) or record.byte_size
        references.record_gate_release(
            record, evaluation, derivative,
            object_key=committed.committed_key,
            storage_version_id=committed.storage_version_id,
        )
        _audit(session, context, "reference_gate", detail={
            "object_id": object_id,
            "execution_mode": mode,
            "checks": _checks_detail(evaluation.checks),
            "content_sha256": source.content_sha256,
            "derivative_sha256": derivative.sha256,
            "derivative_width": derivative.width,
            "derivative_height": derivative.height,
            "had_exif": evaluation.had_exif,
        })
        session.flush()
    return GateOutcome("released", object_id)
