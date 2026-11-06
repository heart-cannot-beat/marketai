"""Scale export creation and download intents (spec §16, §22).

Issue #87 Task 5.

Creates a durable Operation for each export request and dispatches the
build to a Job the Scale Worker (``app.scale.worker``) runs in its own
capability-isolated composition root -- no Meta write port, matching every
other module under ``app/scale`` (see ``app.scale.worker``'s module
docstring; ``tests/test_scale_worker_isolation.py`` enforces this by AST
inspection). ``ScaleExportRecord.result_object_reference`` stores only a
safe object reference after the Worker "commits" the export -- raw payloads
and object keys never enter a Scale HTTP response; downloads go through the
platform's existing authorized download-intent seam
(``app.platform.download_intents``) instead of a signed URL minted here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.platform.blob_store import CommittedBlobRef
from app.platform.download_intents import create_or_replay_download_intent
from app.platform.jobs import enqueue_in_session
from app.platform.operations import (
    OperationResultRef,
    OperationState,
    TERMINAL_STATES,
    create_operation,
    get_operation,
    transition_operation,
)
from app.platform.tenancy import TenantContext, tenant_scope

from .command_journal import claim_command, command_request_fingerprint
from .models import ScaleEnrollmentRecord, ScaleEpisodeRecord, ScaleExportRecord

__all__ = [
    "ExportError",
    "ExportNotFoundError",
    "ExportNotCompleteError",
    "ExportResult",
    "DownloadIntentResult",
    "create_export",
    "create_export_download_intent",
    "run_scale_export",
]


class ExportError(RuntimeError):
    """Base error for every export lifecycle violation. ``code`` is the
    stable machine-readable reason (matched by callers/tests); ``detail`` is
    optional human context."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class ExportNotFoundError(ExportError):
    def __init__(self, export_id: str) -> None:
        super().__init__("export_not_found", export_id)


class ExportNotCompleteError(ExportError):
    def __init__(self, export_id: str) -> None:
        super().__init__("export_not_complete", export_id)


@dataclass(frozen=True)
class ExportResult:
    export_id: str
    operation_id: str


@dataclass(frozen=True)
class DownloadIntentResult:
    intent_id: str
    expires_at: datetime


def create_export(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    episode_id: str,
    export_kind: str,
    snapshot_id: str | None = None,
    evaluation_id: str | None = None,
    idempotency_key: str,
) -> ExportResult:
    """Idempotently admit one export request as a durable Operation + Job.

    ``create_scale_export`` is not one of ``command_journal``'s five
    lifecycle command types, so the claim below passes
    ``result_resource_type="export"`` explicitly rather than relying on its
    default map.
    """
    enrollment = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=enrollment_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise ExportError("enrollment_not_found", enrollment_id)

    episode = session.execute(
        select(ScaleEpisodeRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=episode_id,
        )
    ).scalar_one_or_none()
    if episode is None or episode.enrollment_id != enrollment_id:
        # Reported identically to a plain missing episode -- this must never
        # let a caller confirm that an episode id exists under some other
        # Enrollment (mirrors app.scale.breakdowns's same guard).
        raise ExportError("episode_not_found", episode_id)

    export_id = new_id("sexp")

    fingerprint = command_request_fingerprint(
        enrollment_id=enrollment_id,
        episode_id=episode_id,
        export_kind=export_kind,
        snapshot_id=snapshot_id,
        evaluation_id=evaluation_id,
    )
    claim = claim_command(
        session, context,
        command_type="create_scale_export",
        deduplication_key=idempotency_key,
        result_id=export_id,
        request_fingerprint=fingerprint,
        result_resource_type="export",
    )
    if claim.replay:
        existing = session.execute(
            select(ScaleExportRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=claim.record.result_resource_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            raise ExportError(
                "idempotency_result_missing", claim.record.result_resource_id
            )
        return ExportResult(export_id=existing.id, operation_id=existing.operation_id)

    operation = create_operation(
        session,
        command_type="scale_export",
        context=context,
        initial_state=OperationState.QUEUED,
        reason="Scale export requested",
    )

    now = utcnow()
    export = ScaleExportRecord(
        id=export_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        operation_id=operation.id,
        enrollment_id=enrollment_id,
        episode_id=episode_id,
        snapshot_id=snapshot_id,
        evaluation_id=evaluation_id,
        export_kind=export_kind,
        requested_by=context.initiator_id or context.executor,
        requested_at=now,
    )
    session.add(export)
    session.flush()

    # ``enqueue_in_session`` (like ``get_operation``/``transition_operation``
    # and the download-intent idempotency helper below) requires the
    # session's bound TenantContext to match the caller's -- callers that
    # already opened the session with ``tenant_session()`` (every production
    # route/worker) already satisfy this; ``tenant_scope`` makes the binding
    # self-sufficient for any other caller (e.g. a test using a bare
    # Session) without forcing every caller to bind it themselves.
    with tenant_scope(session, context):
        job = enqueue_in_session(
            session,
            context=context,
            job_type="scale_export",
            payload={"export_id": export_id},
            idempotency_key=f"scale_export_job:{export_id}",
            resource_type="scale_export",
            resource_id=export_id,
        )
    job.operation_id = operation.id
    session.flush()

    return ExportResult(export_id=export.id, operation_id=operation.id)


def create_export_download_intent(
    session: Session,
    context: TenantContext,
    *,
    export_id: str,
    idempotency_key: str,
) -> DownloadIntentResult:
    """Idempotently mint a DownloadIntent for a completed export.

    Re-resolves the export's row (never the browser's own claim) on every
    call, replays included, so a replay still fails closed if the export
    somehow regressed out of "completed" between requests.
    """
    export = session.execute(
        select(ScaleExportRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=export_id,
        )
    ).scalar_one_or_none()
    if export is None:
        raise ExportNotFoundError(export_id)
    if export.completed_at is None or export.result_object_reference is None:
        raise ExportNotCompleteError(export_id)

    committed_ref = CommittedBlobRef(
        committed_key=export.result_object_reference,
        storage_version_id=export.id,
        size_bytes=0,
        checksum_sha256_b64="",
        content_type_verified="application/octet-stream",
    )

    with tenant_scope(session, context):
        intent = create_or_replay_download_intent(
            session,
            idempotency_key=idempotency_key,
            command_type="scale_export_download",
            fingerprint_hash=command_request_fingerprint(export_id=export_id),
            owner_kind="scale_export",
            owner_id=export_id,
            committed_ref=committed_ref,
        )
    return DownloadIntentResult(
        intent_id=intent.id,
        expires_at=intent.expires_at,
    )


def _build_safe_result_reference(export: ScaleExportRecord) -> str:
    """Produce the opaque object reference recorded on a completed export.

    Placeholder for the atomic object-commit step
    (``BlobStore.commit_generated_artifact``, mirroring
    ``app.generation.export_worker``'s ExportPackage build) a later task
    wires up once the export payload format (spec §16) is defined; this
    Task only owns the Operation/Job/DownloadIntent plumbing around it. The
    reference is never a raw object key a browser can dereference on its
    own -- resolution always goes back through
    :func:`create_export_download_intent`.
    """
    return new_id("sblob")


def run_scale_export(
    session: Session,
    context: TenantContext,
    export_id: str,
) -> None:
    """Worker entry point: assemble the export result and settle the
    Operation. Idempotent under Job redelivery: an export already
    ``completed_at`` is left untouched rather than re-transitioning its
    (already terminal) Operation, which would raise.
    """
    export = session.get(ScaleExportRecord, export_id)
    if export is None or export.workspace_id != context.workspace_id:
        return
    if export.completed_at is not None:
        return

    operation = get_operation(session, export.operation_id)
    if operation.state == OperationState.QUEUED.value:
        transition_operation(
            session,
            export.operation_id,
            OperationState.RUNNING,
            reason="export build started",
            actor=context.executor,
        )

    try:
        result_ref = _build_safe_result_reference(export)
    except Exception as exc:
        operation = get_operation(session, export.operation_id)
        if OperationState(operation.state) not in TERMINAL_STATES:
            transition_operation(
                session,
                export.operation_id,
                OperationState.FAILED,
                reason=str(exc)[:200],
                actor=context.executor,
            )
        raise

    now = utcnow()
    export.result_object_reference = result_ref
    export.completed_at = now
    session.flush()

    transition_operation(
        session,
        export.operation_id,
        OperationState.SUCCEEDED,
        reason="export completed",
        actor=context.executor,
        result_ref=OperationResultRef(resource_type="scale_export", resource_id=export.id),
    )
