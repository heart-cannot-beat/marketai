"""Export package Worker handler (Issue #49 Task 9).

Builds a deterministic ZIP from the frozen selection of committed
``AssetVersion`` blobs an ``ExportPackageRecord`` carries, commits it
through ``BlobStore.commit_generated_artifact``, and transitions both the
package and its authorizing Operation to a terminal state.

Deterministic ZIP (spec §0.4): every entry -- the deduplicated asset
payloads and ``manifest.json`` -- carries the fixed ``2020-01-01T00:00:00``
timestamp and ``0o644`` permissions, so two builds of the same frozen
selection produce byte-identical archives regardless of wall-clock time.

Content-hash dedup: multiple AssetVersions sharing the same
``content_sha256`` (e.g. a reused/unchanged Localization output) are written
to the ZIP once, keyed by that hash under ``assets/``; ``manifest.json``
still carries one entry per *logical* AssetVersion, so a consumer maps a
provenance row to its (possibly shared) archive member via
``content_sha256``.

Failure handling: any AssetVersion whose committed blob cannot be resolved
or streamed fails the *whole* package (spec: a partially-built export is
never handed out as ready) -- the package moves to ``failed`` with
``asset_blob_unavailable`` recorded in ``failure_evidence``, and the
Operation moves to ``failed`` alongside it. Any other unexpected error
fails the package with ``export_build_failed`` instead of leaving it stuck
``building`` forever.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.generation.asset_queries import AssetBlobError, resolve_asset_blob_ref
from app.generation.export import _get_package, _load_versions_in_selector_order, build_manifest
from app.generation.models import ExportPackageRecord
from app.platform.blob_store import (
    BlobStore,
    BlobStoreError,
    CommittedBlobRef,
    GeneratedArtifactSpec,
    extension_for_mime,
)
from app.platform.operations import (
    OperationResultRef,
    OperationState,
    get_operation,
    transition_operation,
)
from app.platform.tenancy import current_context
from app.platform.runtime_role import WORKER_ROLE, runs_as

__all__ = ["handle_export_package"]

#: Fixed ZIP entry timestamp/permissions (spec §0.4) -- every entry this
#: Worker writes carries these exact values so two builds of the same
#: frozen selection are byte-identical.
FIXED_DATE_TIME = (2020, 1, 1, 0, 0, 0)
FIXED_EXTERNAL_ATTR = 0o644 << 16

#: Package states from which the Worker is willing to (re)start a build.
#: Anything else (``ready``/``failed``/``expired``) is already terminal.
#: ``building`` is only ever flushed inside the build's own transaction, so
#: no other run can observe it: the row lock below makes a second run wait
#: for the first to commit or roll back.
_BUILDABLE_STATES = ("pending",)


@runs_as(WORKER_ROLE)
async def handle_export_package(
    session: Session,
    blob_store: BlobStore,
    package_id: str,
) -> str:
    """Process one ExportPackage end to end: build ZIP -> commit -> ready.

    Returns the state the package is in when this call leaves it: ``ready``
    or ``failed`` from this build, or whatever terminal state an earlier
    run already committed -- in which case nothing is written (#22).

    The whole build is one transaction, taken under the package's row lock.
    A Worker that dies mid-build leaves the package ``pending`` and its
    Operation ``queued`` (nothing committed), so the requeued Job builds it
    from scratch; a second run of the same Job waits on the lock and then
    finds the package already settled.
    """
    ctx = current_context(session)
    package = _lock_package(session, ctx, package_id)

    if package.state not in _BUILDABLE_STATES:
        return package.state

    # Compare-and-set as well as the lock: SQLite ignores ``FOR UPDATE`` and
    # reads outside its write transaction, so there a second run can still
    # be holding a ``pending`` it read before the first run committed.
    claimed = session.execute(
        update(ExportPackageRecord)
        .where(
            ExportPackageRecord.id == package.id,
            ExportPackageRecord.workspace_id == ctx.workspace_id,
            ExportPackageRecord.state.in_(_BUILDABLE_STATES),
        )
        .values(state="building")
    )
    session.refresh(package)
    if claimed.rowcount != 1:
        return package.state
    _sync_operation_running(session, package)

    try:
        zip_hash, zip_size, committed_ref, manifest_bytes, counts = await _build_and_commit(
            session, ctx, blob_store, package
        )
    except (AssetBlobError, BlobStoreError) as exc:
        code = getattr(exc, "code", "asset_blob_unavailable")
        _fail_package(session, package, code=code or "asset_blob_unavailable", detail=str(exc))
        return package.state
    except Exception as exc:  # noqa: BLE001 -- fail closed, never leave "building" stuck
        _fail_package(session, package, code="export_build_failed", detail=str(exc))
        return package.state

    package.state = "ready"
    package.committed_object_key = committed_ref.committed_key
    package.committed_storage_version_id = committed_ref.storage_version_id
    package.byte_size = zip_size
    package.zip_hash = zip_hash
    package.manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    package.counts_json = counts
    session.flush()

    operation = get_operation(session, package.operation_id)
    if operation.state != OperationState.SUCCEEDED.value:
        transition_operation(
            session,
            package.operation_id,
            OperationState.SUCCEEDED,
            reason=None,
            actor=ctx.executor,
            result_ref=OperationResultRef(resource_type="export_package", resource_id=package.id),
        )
    return package.state


def _lock_package(session: Session, ctx, package_id: str) -> ExportPackageRecord:
    """``_get_package`` under ``SELECT ... FOR UPDATE``, as committed now.

    SQLite ignores the clause and serializes writers itself.
    """
    package = session.scalars(
        select(ExportPackageRecord)
        .where(
            ExportPackageRecord.id == package_id,
            ExportPackageRecord.workspace_id == ctx.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if package is None:
        # Reported exactly as the unlocked read reports it.
        return _get_package(session, ctx, package_id)
    return package


async def _build_and_commit(
    session: Session,
    ctx,
    blob_store: BlobStore,
    package: ExportPackageRecord,
) -> tuple[str, int, CommittedBlobRef, bytes, dict]:
    version_ids = package.selected_versions_json.get("version_ids", [])
    versions = _load_versions_in_selector_order(session, ctx, version_ids)

    logical_count = len(versions)
    seen_hash_to_arcname: dict[str, str] = {}

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{package.id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for version in versions:
                content_hash = version.content_sha256
                if content_hash not in seen_hash_to_arcname:
                    ref = resolve_asset_blob_ref(session, ctx, version.id)
                    ext = extension_for_mime(ref.content_type_verified)
                    arcname = f"assets/{content_hash}.{ext}"

                    payload = bytearray()
                    async for chunk in blob_store.open_committed(ref):
                        payload.extend(chunk)

                    _write_deterministic_entry(zf, arcname, bytes(payload))
                    seen_hash_to_arcname[content_hash] = arcname

            manifest = build_manifest(session, ctx, package.id)
            manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
            _write_deterministic_entry(zf, "manifest.json", manifest_bytes)

        zip_bytes = zip_path.read_bytes()
        zip_digest = hashlib.sha256(zip_bytes)
        zip_hash = zip_digest.hexdigest()
        zip_size = len(zip_bytes)

        committed_key = _export_committed_key(ctx.workspace_id, package.id)
        spec = GeneratedArtifactSpec(
            committed_key=committed_key,
            content_type="application/zip",
            # `zip_hash` (hex) is what the package record and manifest carry;
            # the BlobStore contract's `expected_sha256_b64` is the base64
            # digest, which the Railway Bucket Adapter re-derives from the
            # file and compares before it uploads anything.
            expected_sha256_b64=base64.b64encode(zip_digest.digest()).decode("ascii"),
        )
        committed_ref = await blob_store.commit_generated_artifact(zip_path, spec)

    unique_blob_count = len(seen_hash_to_arcname)
    counts = {
        "logical_count": logical_count,
        "unique_blob_count": unique_blob_count,
        "dedup_count": logical_count - unique_blob_count,
    }
    return zip_hash, zip_size, committed_ref, manifest_bytes, counts


def _write_deterministic_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
    info.external_attr = FIXED_EXTERNAL_ATTR
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def _export_committed_key(workspace_id: str, package_id: str) -> str:
    from app.adapters.persistence.models import utcnow

    now = utcnow()
    return f"committed/{workspace_id}/exports/{now:%Y}/{now:%m}/{now:%d}/{package_id}.zip"


def _sync_operation_running(session: Session, package: ExportPackageRecord) -> None:
    ctx = current_context(session)
    operation = get_operation(session, package.operation_id)
    if operation.state == OperationState.QUEUED.value:
        transition_operation(
            session, package.operation_id, OperationState.RUNNING, reason=None, actor=ctx.executor,
        )


def _fail_package(session: Session, package: ExportPackageRecord, *, code: str, detail: str) -> None:
    ctx = current_context(session)
    package.state = "failed"
    package.failure_evidence = json.dumps({"code": code, "detail": detail[:2000]})
    session.flush()

    operation = get_operation(session, package.operation_id)
    if operation.state not in (OperationState.FAILED.value,):
        # `transition_operation` only admits queued/running/uncertain as the
        # `from_state` for a `-> failed` move -- a package that never left
        # `pending` still has its Operation `queued`, so this covers both
        # the "failed before dispatch" and "failed mid-build" cases.
        if operation.state == OperationState.QUEUED.value:
            transition_operation(
                session, package.operation_id, OperationState.RUNNING, reason=None, actor=ctx.executor,
            )
        transition_operation(
            session, package.operation_id, OperationState.FAILED, reason=code, actor=ctx.executor,
        )
