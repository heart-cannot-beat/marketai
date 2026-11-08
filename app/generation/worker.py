"""Worker execution for Generation RunItems (Issue #21 Task 3).

Drives one RunItem through the ProviderAttempt lifecycle: dispatch ->
response -> output persistence -> QC enqueue. Every RunItem (and
GenerationRun projection) state transition is a CAS
``UPDATE ... WHERE revision = expected`` -- the same pattern
``app.generation.draft`` and ``app.generation.provider_attempt`` use -- so a
crash between two writers can never silently clobber a state change.

Only ``execution_mode == "mock"`` is dispatched by this slice (Issue #21's
scope for Task 3); the mock adapter is inlined here rather than extending
``app.providers.mock`` because it only needs to produce a deterministic,
zero-true-payable output, not the real provider protocol.

``execution_mode == "controlled_mock"`` uses the three-phase
``ExternalCallCoordinator`` from ``app.providers.dispatch``, exercising the
full committed-prepare -> dispatch -> committed-completion seam (Issue #98
Task 1). See ``handle_run_item_coordinated``.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import struct
import tempfile
import uuid
import zlib
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ContextManager

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    JobRecord,
    new_id,
    utcnow,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.usage import record_provider_call
from app.generation.execution_policy import StepAdmission, StepRejection, admit_external_step
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    GenerationRunCommandRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.generation.provider_attempt import append_event, prepare_attempt
from app.generation.run import derive_run_state
from app.generation.run_operations import settle_run_command_operations
from app.generation.thumbnails import CommittedThumbnail, apply_thumbnail
from app.platform.jobs import enqueue_in_session
from app.platform.blob_store import BlobStore, CommittedBlobRef, GeneratedArtifactSpec
from app.platform.tenancy import TenantContext

if TYPE_CHECKING:
    from app.generation.controlled_adapter import GenerationRequest
    from app.providers.adapter import ExternalCallAdapter
    from app.providers.dispatch import ExternalCallCoordinator, ExternalCallEvidenceRecorder

logger = logging.getLogger(__name__)

#: RunItem states from which the Worker is willing to start (or restart)
#: dispatch. Anything else (``running``, ``uncertain``, ``qc_pending`` or a
#: terminal state) means either another attempt is already in flight or the
#: item is already past this stage -- re-entering ``handle_run_item`` on it
#: is a silent no-op rather than a CAS error, so a redelivered job never
#: corrupts an already-advanced RunItem. Recovering a genuinely stuck
#: ``uncertain`` item is ``handle_reconcile``'s job, not this one's.
_DISPATCHABLE_STATES = ("pending", "queued", "paused")

_DEFAULT_ADAPTER_REGISTRY = {"mock": {"version": "v1", "capabilities": {}}}


class StaleRunItemRevisionError(RuntimeError):
    """Raised when a CAS UPDATE on a RunItem (or its GenerationRun) affects no row."""


def handle_run_item(
    session: Session,
    ctx: TenantContext,
    run_item_id: str,
    *,
    adapter_registry: dict | None = None,
    output_override: dict | None = None,
    committed_blob_ref: CommittedBlobRef | None = None,
    committed_thumbnail: CommittedThumbnail | None = None,
) -> None:
    """Process one RunItem end to end: dispatch -> response -> persist -> enqueue QC.

    ``committed_thumbnail`` is the gallery derivative the caller committed
    next to ``committed_blob_ref`` (``app.generation.thumbnails``); ``None``
    -- no blob store, or the derivative could not be produced -- records
    nothing and the AssetVersion is served from its original.
    """
    item = _get_run_item(session, ctx, run_item_id)
    if item.state not in _DISPATCHABLE_STATES:
        return

    registry = adapter_registry if adapter_registry is not None else _DEFAULT_ADAPTER_REGISTRY
    admission = admit_external_step(
        session,
        snapshot_mode=item.execution_mode,
        adapter_registry=registry,
    )
    if isinstance(admission, StepRejection):
        _fail_or_pause_item(session, ctx, item, admission)
        return

    item = _transition_item(session, ctx, item, state="running")

    attempt = _prepare_dispatch(session, ctx, item, admission)
    append_event(
        session,
        workspace_id=ctx.workspace_id,
        provider_call_attempt_id=attempt.id,
        event_type="dispatch_started",
        payload={"adapter_key": admission.adapter_key},
        executor="worker",
    )

    output = output_override if output_override is not None else _call_adapter(item, admission)

    append_event(
        session,
        workspace_id=ctx.workspace_id,
        provider_call_attempt_id=attempt.id,
        event_type="response_received",
        payload={"object_key": output.get("object_key", "")},
        executor="worker",
    )
    append_event(
        session,
        workspace_id=ctx.workspace_id,
        provider_call_attempt_id=attempt.id,
        event_type="usage_reported",
        payload={"usage": output.get("usage", {})},
        executor="worker",
    )

    asset, version = _persist_output(
        session,
        ctx,
        item,
        attempt,
        output,
        committed_blob_ref=committed_blob_ref,
        committed_thumbnail=committed_thumbnail,
    )

    session.add(
        RunItemAssetVersionBindingRecord(
            id=new_id("riavb"),
            workspace_id=ctx.workspace_id,
            run_item_id=item.id,
            asset_version_id=version.id,
            kind="produced",
            producing_provider_attempt_id=attempt.id,
            created_at=utcnow(),
        )
    )
    session.flush()

    # The Mock adapter answered: one call, counted in the ``mock`` bucket.
    record_provider_call(session, ctx, attempt, occurred_at=utcnow())

    item = _transition_item(session, ctx, item, state="qc_pending")

    enqueue_in_session(
        session,
        context=ctx,
        job_type="generation.qc_asset_version",
        resource_type="asset_version",
        resource_id=version.id,
        payload={"asset_version_id": version.id, "run_item_id": item.id},
        idempotency_key=f"qc:{version.id}",
    )

    _sync_run_state_best_effort(session, ctx, item.generation_run_id)


def handle_run_item_dispatch_only(
    session: Session,
    ctx: TenantContext,
    run_item_id: str,
    *,
    adapter_registry: dict | None = None,
) -> None:
    """Test/recovery seam: prepare and dispatch an attempt, then stop.

    Simulates a Worker process crashing between the ``dispatch_started``
    event and the provider's response: the ProviderCallAttempt exists and
    carries a ``dispatched`` projection, but no ``response_received`` event
    ever follows, so the RunItem is left ``uncertain`` -- the same terminal
    ambiguity ``app.platform.jobs.recover_expired_jobs`` assigns a Job whose
    lease expires mid-flight. Reconciling it back to a certain outcome is
    ``handle_reconcile``'s job, out of this task's scope.
    """
    item = _get_run_item(session, ctx, run_item_id)
    if item.state not in _DISPATCHABLE_STATES:
        return

    registry = adapter_registry if adapter_registry is not None else _DEFAULT_ADAPTER_REGISTRY
    admission = admit_external_step(
        session,
        snapshot_mode=item.execution_mode,
        adapter_registry=registry,
    )
    if isinstance(admission, StepRejection):
        _fail_or_pause_item(session, ctx, item, admission)
        return

    item = _transition_item(session, ctx, item, state="running")

    attempt = _prepare_dispatch(session, ctx, item, admission)
    append_event(
        session,
        workspace_id=ctx.workspace_id,
        provider_call_attempt_id=attempt.id,
        event_type="dispatch_started",
        payload={"adapter_key": admission.adapter_key},
        executor="worker",
    )

    # The crash happens here, before any `response_received` event -- the
    # dispatch is of unknown outcome to us, so the item is `uncertain`
    # rather than `failed` (a paid external call may already have happened).
    _transition_item(session, ctx, item, state="uncertain")
    _sync_run_state_best_effort(session, ctx, item.generation_run_id)


# ---------------------------------------------------------------------
# ProviderAttempt dispatch helpers
# ---------------------------------------------------------------------


def _prepare_dispatch(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    admission: StepAdmission,
) -> ProviderCallAttemptRecord:
    operation_id = _get_operation_id(session, ctx, item)
    job_id = _get_job_id(session, ctx, item)
    reservation_id = _get_or_create_reservation_id(session, ctx, item, operation_id, job_id)
    call_semantic_key_hash = _generation_semantic_key_hash(item.semantic_fingerprint)

    return prepare_attempt(
        session,
        workspace_id=ctx.workspace_id,
        operation_id=operation_id,
        job_id=job_id,
        spend_reservation_id=reservation_id,
        run_item_id=item.id,
        step_kind="generation",
        call_semantic_key_hash=call_semantic_key_hash,
        attempt_no=_next_attempt_no(session, ctx, call_semantic_key_hash),
        execution_mode=item.execution_mode,
        provider=admission.adapter_key,
        chargeability="legacy_reserved",
        provider_idempotency_key=str(uuid.uuid4()),
    )


def _call_adapter(item: RunItemRecord, admission: StepAdmission) -> dict:
    """Inline mock dispatch (Task 3 ruling): deterministic, zero true payable.

    The synthetic output bytes derive from the RunItem's
    ``semantic_fingerprint`` (the frozen recipe hash plus the Run's
    submission nonce and the item's combination/variant position -- see
    ``app.generation.submission._semantic_fingerprint``), not from a random
    value: the same RunItem dispatched twice (a retried Job that never
    advanced the RunItem's state, or a reconciliation replay) must produce
    the same mock content, matching the spec's requirement that mock output
    bytes be deterministic per semantic input. A second Run of the same
    recipe is a second intent and gets its own bytes, like a fresh seed.

    Real (BYOK/sandbox) dispatch is out of scope for this Worker slice --
    every RunItem submitted through Task 2's Atomic Submission in this slice
    carries ``execution_mode == "mock"``, and ``admit_external_step`` is what
    gates any other mode before this function is ever reached.
    """
    if item.execution_mode == "mock":
        return build_mock_output(item.semantic_fingerprint)
    raise NotImplementedError(
        f"execution_mode {item.execution_mode!r} has no dispatch implementation in this Worker slice"
    )


def build_mock_output(semantic_fingerprint: str) -> dict:
    """Build deterministic, valid PNG bytes for one controlled-Mock output.

    ``content_sha256`` is the hex digest every Generation/Library record
    stores; ``content_sha256_b64`` is the same digest in the base64 form the
    ``BlobStore`` contract (``*_sha256_b64``) verifies on commit.
    """
    content = _mock_png_bytes(semantic_fingerprint)
    digest = hashlib.sha256(content)
    content_sha256 = digest.hexdigest()
    return {
        "object_key": f"mock/{content_sha256}.png",
        "content_sha256": content_sha256,
        "content_sha256_b64": base64.b64encode(digest.digest()).decode("ascii"),
        "byte_size": len(content),
        "mime_type": "image/png",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "content_bytes": content,
    }


async def materialize_mock_output(
    blob_store: BlobStore,
    ctx: TenantContext,
    semantic_fingerprint: str,
) -> tuple[dict, CommittedBlobRef]:
    """Commit controlled-Mock bytes before their AssetVersion is persisted.

    The deterministic key makes a retry overwrite/reuse the same synthetic
    artifact instead of leaking a second object after a process failure.

    ``expected_sha256_b64`` must be the base64 digest: the Railway Bucket
    Adapter re-hashes the file and refuses the commit on a mismatch, so the
    hex ``content_sha256`` that was handed over here before failed every
    production Mock item with "generated artifact checksum changed before
    commit" (the dev ``LocalFileBlobStore`` echoes whatever it is given and
    never caught it).
    """
    output = build_mock_output(semantic_fingerprint)
    committed_key = (
        f"committed/{ctx.workspace_id}/generation_output/"
        f"{output['content_sha256']}.png"
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "mock-output.png"
        source.write_bytes(output["content_bytes"])
        committed = await blob_store.commit_generated_artifact(
            source,
            GeneratedArtifactSpec(
                committed_key=committed_key,
                content_type=output["mime_type"],
                expected_sha256_b64=output["content_sha256_b64"],
            ),
        )
    return output, committed


def _mock_png_bytes(semantic_fingerprint: str) -> bytes:
    """Return a one-pixel RGB PNG whose colour is bound to the semantic input."""
    rgb = hashlib.sha256(semantic_fingerprint.encode("utf-8")).digest()[:3]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00" + rgb, level=9))
        + chunk(b"IEND", b"")
    )


def _persist_output(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    attempt: ProviderCallAttemptRecord,
    output: dict,
    *,
    committed_blob_ref: CommittedBlobRef | None = None,
    committed_thumbnail: CommittedThumbnail | None = None,
) -> tuple[AssetRecord, AssetVersionRecord]:
    run = _get_run(session, ctx, item.generation_run_id)

    source = session.scalar(select(AssetVersionRecord).where(
        AssetVersionRecord.workspace_id == ctx.workspace_id,
        AssetVersionRecord.id == item.source_asset_version_id)) if item.source_asset_version_id else None
    asset = session.scalar(select(AssetRecord).where(AssetRecord.workspace_id == ctx.workspace_id,
        AssetRecord.id == source.asset_id, AssetRecord.product_id == run.product_id)) if source else None
    if item.source_asset_version_id and asset is None:
        raise ValueError("regeneration_source_unavailable")
    if asset is None:
        asset = AssetRecord(
            id=new_id("asset"),
            workspace_id=ctx.workspace_id,
            product_id=run.product_id,
            run_id=item.generation_run_id,
            combo_id=item.combination_fingerprint,
            combo_code=item.combination_fingerprint,
            tags_json=item.tags_json,
            filename=output.get("object_key", ""),
            provider=attempt.provider,
            model=attempt.model or "",
            prompt="",
            fingerprint=item.semantic_fingerprint,
            status="ok",
        )
        session.add(asset)
        session.flush()

    truth_dependency = dict(
        (session.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id).canonical_json or {}).get(
            "truth_dependency"
        )
        or {}
    )
    truth_evidence = truth_dependency.get("evidence")
    if not isinstance(truth_evidence, dict):
        raise RuntimeError("Generation recipe is missing frozen truth-dependency evidence")

    version = AssetVersionRecord(
        id=new_id("av"),
        workspace_id=ctx.workspace_id,
        asset_id=asset.id,
        parent_version_id=source.id if source else None,
        kind="generated",
        aspect_ratio=output.get("aspect_ratio", run.aspect_ratio),
        object_key=output.get("object_key", ""),
        committed_object_key=(
            committed_blob_ref.committed_key if committed_blob_ref is not None else None
        ),
        committed_storage_version_id=(
            committed_blob_ref.storage_version_id if committed_blob_ref is not None else None
        ),
        # The record keeps the hex digest like every other `content_sha256`
        # column (Library references, Localization outputs); the committed
        # ref's `checksum_sha256_b64` is the same digest in base64.
        content_sha256=output.get("content_sha256", ""),
        byte_size=(
            committed_blob_ref.size_bytes
            if committed_blob_ref is not None
            else output.get("byte_size", 0)
        ),
        mime_type=(
            committed_blob_ref.content_type_verified
            if committed_blob_ref is not None
            else output.get("mime_type", "")
        ),
        producing_run_item_id=item.id,
        provider_attempt_id=attempt.id,
        origin_kind="generation",
        execution_mode=item.execution_mode,
        effective_seed=item.effective_seed,
        requested_seed=item.requested_seed,
        seed_unavailable_reason=item.seed_unavailable_reason,
        truth_package_id=truth_dependency.get("truth_package_id"),
        truth_dependency_expected=truth_dependency.get("expected"),
        truth_dependency_policy_version=truth_dependency.get("policy_version"),
        truth_dependency_evidence_json=truth_evidence,
        truth_dependency_evidence_hash=_canonical_hash(truth_evidence),
    )
    apply_thumbnail(version, committed_thumbnail)
    session.add(version)
    session.flush()
    return asset, version


def _canonical_hash(value: object) -> str:
    from ..platform.canonical import canonical_hash
    return canonical_hash(value)


# ---------------------------------------------------------------------
# RunItem / GenerationRun CAS transitions
# ---------------------------------------------------------------------


def _transition_item(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    *,
    state: str,
    error_summary: str | None = None,
    same_snapshot_resumable: bool | None = None,
) -> RunItemRecord:
    values: dict = {"state": state, "revision": item.revision + 1, "updated_at": utcnow()}
    if error_summary is not None:
        values["error_summary"] = error_summary
    if same_snapshot_resumable is not None:
        values["same_snapshot_resumable"] = same_snapshot_resumable
    elif state == "queued":
        # Re-queuing (Recovery preview's re-run/re-queue path) always starts
        # a fresh attempt against the item's current snapshot -- any earlier
        # "resume this exact snapshot" offer is stale once the item leaves
        # `paused`, so it is cleared here rather than left for the caller to
        # remember.
        values["same_snapshot_resumable"] = False

    result = session.execute(
        update(RunItemRecord)
        .where(
            RunItemRecord.id == item.id,
            RunItemRecord.workspace_id == ctx.workspace_id,
            RunItemRecord.revision == item.revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise StaleRunItemRevisionError(
            f"RunItem {item.id} expected revision {item.revision}, was concurrently modified"
        )
    session.flush()
    session.refresh(item)
    return item


_HANDLER_FAULT_STATES = ("pending", "queued", "running")


def fail_run_item_after_handler_fault(
    session: Session,
    ctx: TenantContext,
    run_item_id: str,
    *,
    error_summary: str,
) -> None:
    """Mark a RunItem ``failed`` after the Worker could not run its handler.

    Everything that raises before ``handle_run_item`` reaches a terminal
    transition -- reading the item, materializing the Mock output and
    committing it to the object store, a stale-revision CAS -- used to settle
    only the Job (``failed``) and leave the RunItem at ``pending``. Nothing
    user-facing reads Jobs, so the Run sat at ``running`` with every item
    pending forever and Recovery had nothing to retry. Projecting the fault
    onto the item makes it visible on the Run page and lets
    ``retry_failed_items`` re-enqueue it.

    Only an item the handler never settled is touched: ``pending``/``queued``
    (never started) or ``running`` (interrupted before a terminal state). An
    item that already reached ``paused`` or a terminal state keeps what its
    handler chose.
    """
    item = _get_run_item(session, ctx, run_item_id)
    if item.state not in _HANDLER_FAULT_STATES:
        return
    item = _transition_item(
        session,
        ctx,
        item,
        state="failed",
        error_summary=error_summary,
        same_snapshot_resumable=False,
    )
    _sync_run_state_best_effort(session, ctx, item.generation_run_id)


def _fail_or_pause_item(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    rejection: StepRejection,
) -> None:
    state = "paused" if rejection.recoverable else "failed"
    item = _transition_item(
        session,
        ctx,
        item,
        state=state,
        error_summary=f"{rejection.code}: {rejection.detail}",
        same_snapshot_resumable=rejection.recoverable,
    )
    _sync_run_state_best_effort(session, ctx, item.generation_run_id)


def _sync_run_state(session: Session, ctx: TenantContext, generation_run_id: str) -> None:
    """Recompute the GenerationRun's ``status``/``done``/``failed`` projection.

    ``derive_run_state`` treats ``paused`` as still-active (worker action is
    still owed on it), so a Run with a paused item stays ``running`` rather
    than settling early -- the divergence flagged in Task 2 review is
    resolved by leaning on ``derive_run_state`` exactly as written, not by
    special-casing ``paused`` here.
    """
    run = session.execute(
        select(GenerationRunRecord).where(
            GenerationRunRecord.id == generation_run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if run is None:
        return

    states = list(
        session.execute(
            select(RunItemRecord.state).where(
                RunItemRecord.generation_run_id == generation_run_id,
                RunItemRecord.workspace_id == ctx.workspace_id,
            )
        ).scalars()
    )

    new_status = derive_run_state(states)
    done = sum(1 for s in states if s in ("succeeded", "reused"))
    failed = sum(1 for s in states if s == "failed")

    result = session.execute(
        update(GenerationRunRecord)
        .where(
            GenerationRunRecord.id == run.id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
            GenerationRunRecord.revision == run.revision,
        )
        .values(
            status=new_status,
            done=done,
            failed=failed,
            revision=run.revision + 1,
            updated_at=utcnow(),
        )
    )
    if result.rowcount != 1:
        raise StaleRunItemRevisionError(
            f"GenerationRun {run.id} expected revision {run.revision}, was concurrently modified"
        )
    session.flush()

    # The Run's item-driving command Operations (submit / retry / resume)
    # are ``admission_contract_version`` 0, so the generic Job aggregator
    # never folds them: the Run projection just recomputed is what settles
    # them. Only the CAS winner reaches here, so two Workers finishing the
    # last two RunItems at once cannot both append the terminal event.
    settle_run_command_operations(
        session,
        ctx,
        run.id,
        new_status,
        actor=ctx.initiator_id or ctx.executor,
    )


def _sync_run_state_best_effort(session: Session, ctx: TenantContext, generation_run_id: str) -> None:
    """Best-effort wrapper around ``_sync_run_state`` for RunItem-completion callers.

    The GenerationRun's status/done/failed columns are a derived projection,
    recomputed from every RunItem in the Run -- multiple RunItems of the same
    Run are routinely processed concurrently via durable jobs, so they race
    on the Run's ``revision``. A CAS loss here must never roll back the
    caller's own already-committed-in-this-transaction work (the RunItem
    transition, its AssetVersion, and the QC enqueue): the next RunItem
    completion (or a future reconciliation pass) recomputes the same
    projection and corrects it.
    """
    try:
        _sync_run_state(session, ctx, generation_run_id)
    except StaleRunItemRevisionError:
        logger.warning(
            "generation_run_state_sync_conflict",
            extra={"workspace_id": ctx.workspace_id, "generation_run_id": generation_run_id},
        )


# ---------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------


def _get_run_item(session: Session, ctx: TenantContext, run_item_id: str) -> RunItemRecord:
    return session.execute(
        select(RunItemRecord).where(
            RunItemRecord.id == run_item_id,
            RunItemRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()


def _get_run(session: Session, ctx: TenantContext, generation_run_id: str) -> GenerationRunRecord:
    return session.execute(
        select(GenerationRunRecord).where(
            GenerationRunRecord.id == generation_run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()


def _get_operation_id(session: Session, ctx: TenantContext, item: RunItemRecord) -> str:
    """The Operation that admitted this RunItem's Run (Task 2's ``submit`` command).

    Every RunItem created by ``app.generation.submission.submit_generation``
    has a sibling ``GenerationRunCommandRecord`` in the same transaction, so
    this always resolves for a RunItem reachable through that path; the
    lookup is strict (``scalar_one``) rather than falling back to a
    fabricated id, since a fabricated Operation id would fail the
    ``provider_call_attempts`` FK at flush anyway.
    """
    command = session.execute(
        select(GenerationRunCommandRecord).where(
            GenerationRunCommandRecord.workspace_id == ctx.workspace_id,
            GenerationRunCommandRecord.generation_run_id == item.generation_run_id,
            GenerationRunCommandRecord.command_kind == "submit",
        )
    ).scalars().first()
    if command is None:
        raise ValueError(
            f"no 'submit' GenerationRunCommand found for GenerationRun {item.generation_run_id}"
        )
    return command.operation_id


def _get_job_id(session: Session, ctx: TenantContext, item: RunItemRecord) -> str:
    """The durable Job currently driving this RunItem.

    Atomic Submission enqueues the first one; every Recovery command
    (``retry_failed_items`` / ``resume_paused_items``) enqueues a fresh Job
    for the same RunItem, so the newest Job is the one whose dispatch this
    attempt belongs to.
    """
    job = session.execute(
        select(JobRecord)
        .where(
            JobRecord.workspace_id == ctx.workspace_id,
            JobRecord.resource_type == "run_item",
            JobRecord.resource_id == item.id,
        )
        .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
    ).scalars().first()
    if job is None:
        raise ValueError(f"no enqueued Job found for RunItem {item.id}")
    return job.id


def _next_attempt_no(session: Session, ctx: TenantContext, call_semantic_key_hash: str) -> int:
    """The next ``attempt_no`` under ``call_semantic_key_hash``.

    ``provider_call_attempts`` is unique on ``(workspace_id,
    call_semantic_key_hash, attempt_no)`` and the semantic key is the
    RunItem's stable identity, so a retried RunItem appends attempt ``n+1``
    under the same key rather than colliding with its first dispatch.
    """
    latest = session.execute(
        select(func.max(ProviderCallAttemptRecord.attempt_no)).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.call_semantic_key_hash == call_semantic_key_hash,
        )
    ).scalar()
    return 0 if latest is None else int(latest) + 1


def _get_or_create_reservation_id(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    operation_id: str,
    job_id: str,
) -> str:
    """The SpendReservation backing this RunItem's ProviderAttempt(s).

    One reservation per RunItem (Task 1's ``run_item_id`` column), reused
    across retries of the same item. Mock mode reserves zero of everything --
    the true-payable amount is always zero, so there is nothing to guard.
    """
    existing = session.execute(
        select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.run_item_id == item.id,
        )
    ).scalars().first()
    if existing is not None:
        return existing.id

    now = utcnow()
    reservation = SpendReservationRecord(
        id=new_id("sr"),
        workspace_id=ctx.workspace_id,
        operation_id=operation_id,
        job_id=job_id,
        semantic_key_hash=hashlib.sha256(f"sr:gen:{item.id}".encode()).hexdigest(),
        state="reserved",
        provider="mock",
        execution_mode=item.execution_mode,
        provider_currency="USD",
        maximum_provider_amount=Decimal("0"),
        budget_currency="USD",
        maximum_budget_amount=Decimal("0"),
        outstanding_budget_amount=Decimal("0"),
        entitlement_version=0,
        lease_sequence=0,
        guardrail_revision=0,
        platform_policy_version=0,
        created_at=now,
        updated_at=now,
        run_item_id=item.id,
    )
    session.add(reservation)
    session.flush()
    return reservation.id


# ---------------------------------------------------------------------
# Coordinated dispatch path (Issue #98 Task 1)
# ---------------------------------------------------------------------


async def handle_run_item_coordinated(
    coordinator: ExternalCallCoordinator,
    ctx: TenantContext,
    run_item_id: str,
    *,
    adapter: ExternalCallAdapter,
    evidence_recorder: ExternalCallEvidenceRecorder,
    session_factory: Callable[[TenantContext], ContextManager[Session]],
) -> None:
    """Three-phase coordinated dispatch for a generation RunItem.

    Uses ``ExternalCallCoordinator`` for SIGKILL-safe dispatch: the
    attempt record survives process death and enables reconciliation
    without re-dispatching.

    Phase 1 (prepare): creates the ``ProviderCallAttemptRecord`` and
    transitions the RunItem to ``running`` (via the evidence recorder's
    ``link_prepared_attempt``), all in one committed transaction.

    Phase 2 (dispatch): calls the adapter outside any DB transaction.
    The attempt is in ``dispatching`` state; a crash here leaves it
    recoverable via ``reconcile()``.

    Phase 3 (complete): persists the ``AssetVersionRecord``, binding, and
    RunItem -> ``qc_pending`` transition (via the evidence recorder's
    ``persist_result``), all in one committed transaction with the
    attempt's terminal ``succeeded`` state.
    """
    from app.generation.controlled_adapter import GenerationRequest

    # Read RunItem metadata in a short-lived session to avoid holding a
    # long transaction open across the network call.
    with session_factory(ctx) as session:
        item = _get_run_item(session, ctx, run_item_id)
        if item.state not in _DISPATCHABLE_STATES:
            return

        operation_id = _get_operation_id(session, ctx, item)
        job_id = _get_job_id(session, ctx, item)
        semantic_key = _generation_semantic_key_hash(item.semantic_fingerprint)
        semantic_fingerprint = item.semantic_fingerprint
        execution_mode = item.execution_mode

    request = GenerationRequest(
        semantic_fingerprint=semantic_fingerprint,
        execution_mode=execution_mode,
    )

    result = await coordinator.execute(
        ctx,
        job_id=job_id,
        operation_id=operation_id,
        call_semantic_key_hash=semantic_key,
        step_kind="generation",
        execution_mode=execution_mode,
        chargeability="free_sandbox",
        provider=adapter.provider,
        request=request,
        adapter=adapter,
        evidence_recorder=evidence_recorder,
        credential=None,
        spend_reservation_id=None,
        run_item_id=run_item_id,
    )

    if result.success:
        logger.info(
            "generation_run_item_coordinated_success",
            extra={
                "workspace_id": ctx.workspace_id,
                "run_item_id": run_item_id,
                "attempt_id": result.attempt_id,
            },
        )
    elif result.dispatch_certainty == "uncertain":
        logger.warning(
            "generation_run_item_coordinated_uncertain",
            extra={
                "workspace_id": ctx.workspace_id,
                "run_item_id": run_item_id,
                "attempt_id": result.attempt_id,
                "error_detail": result.error_detail,
            },
        )
    else:
        logger.error(
            "generation_run_item_coordinated_failed",
            extra={
                "workspace_id": ctx.workspace_id,
                "run_item_id": run_item_id,
                "attempt_id": result.attempt_id,
                "error_code": result.error_code,
                "error_detail": result.error_detail,
            },
        )


def _generation_semantic_key_hash(semantic_fingerprint: str) -> str:
    """Hash the namespaced provider-call identity into its 64-char DB contract."""
    return hashlib.sha256(
        f"generation:{semantic_fingerprint}".encode("utf-8")
    ).hexdigest()


def _spend_reservation_semantic_key_hash(semantic_fingerprint: str) -> str:
    """Hash the namespaced reservation identity into its 64-char DB contract."""
    return hashlib.sha256(
        f"spend-reservation:generation:{semantic_fingerprint}".encode("utf-8")
    ).hexdigest()
