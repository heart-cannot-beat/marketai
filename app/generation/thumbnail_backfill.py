"""One-off thumbnail backfill for AssetVersions that predate Issue #159.

Every AssetVersion committed before the Worker started deriving gallery
thumbnails (``app.generation.thumbnails``) has a committed original and
null ``thumbnail_*`` columns, so its gallery card and Run tile still load
the full-size file. This module derives and records those thumbnails after
the fact, one Workspace at a time:

* **Idempotent** -- only versions whose ``thumbnail_object_key`` is null and
  whose committed original ref is complete are candidates; a version that
  already carries a thumbnail is never re-derived, and a second run over a
  finished Workspace writes nothing. Thumbnail keys are content-addressed,
  so re-deriving identical bytes re-commits the identical object.
* **Resumable** -- each version is read, derived, committed and recorded in
  its own transaction. A run interrupted at version *n* has versions
  ``1..n-1`` done; restarting selects only what is still null. Within one
  run the keyset cursor (``created_at, id``) also moves past versions that
  failed, so a stubborn one cannot stall the pass; it stays null and is
  retried by the next run.
* **Workspace-scoped** -- the caller names the Workspace (the registry is
  itself under RLS on PostgreSQL, so there is no "all Workspaces" read for a
  runtime role), and every statement runs inside a ``tenant_session`` bound
  to a system context for it. A crafted version id from another Workspace
  is simply not a candidate.
* **Never destructive** -- originals are only read; a decode or storage
  failure leaves that version's columns null and is counted and logged,
  exactly like the Worker's best-effort path.

``scripts/backfill_asset_thumbnails.py`` is the CLI entry point; inside the
Railway api/worker images (which do not ship ``scripts/``) run
``python -m app.generation.thumbnail_backfill`` with the same arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AssetVersionRecord
from app.generation.thumbnails import (
    THUMBNAIL_LONG_EDGE,
    CommittedThumbnail,
    apply_thumbnail,
    commit_thumbnail,
)
from app.platform.blob_store import BlobStore, CommittedBlobRef
from app.platform.blob_store_factory import build_blob_store
from app.platform.tenancy import TenantContext, is_postgres, system_context

logger = logging.getLogger(__name__)

BACKFILL_EXECUTOR = "system:asset-thumbnail-backfill"
#: Bound on the per-version failure details a report carries, so a huge
#: Workspace with a systematic problem still yields a readable summary.
_MAX_REPORTED_FAILURES = 50


@dataclass
class BackfillReport:
    workspace_id: str
    dry_run: bool
    #: Candidates seen by this run (null thumbnail, complete original ref).
    examined: int = 0
    #: Thumbnails derived, committed and recorded by this run.
    derived: int = 0
    #: Candidates whose thumbnail was recorded by someone else between the
    #: scan and the write (a Worker retry, a concurrent backfill) -- the
    #: freshly committed object is left in place and nothing is overwritten.
    already_filled: int = 0
    #: Candidates left null: undecodable bytes, a missing object, a storage
    #: error. Logged individually; retried by the next run.
    failed: int = 0
    #: Versions in the Workspace with no complete committed original ref --
    #: legacy ``object_key``-only rows. Never candidates: there is no
    #: verified blob to derive from.
    without_original: int = 0
    #: Versions in the Workspace that already carry a thumbnail (before this
    #: run). Reported so an operator can see a re-run converge.
    with_thumbnail_before: int = 0
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Candidate:
    version_id: str
    created_at: datetime
    original: CommittedBlobRef


def _candidate_filters(ctx: TenantContext):
    return (
        AssetVersionRecord.workspace_id == ctx.workspace_id,
        AssetVersionRecord.thumbnail_object_key.is_(None),
        AssetVersionRecord.committed_object_key.isnot(None),
        AssetVersionRecord.committed_storage_version_id.isnot(None),
    )


def _count_versions(session: Session, ctx: TenantContext) -> tuple[int, int]:
    """``(with_thumbnail, without_complete_original)`` for the Workspace."""
    rows = session.execute(
        select(
            AssetVersionRecord.thumbnail_object_key,
            AssetVersionRecord.committed_object_key,
            AssetVersionRecord.committed_storage_version_id,
        ).where(AssetVersionRecord.workspace_id == ctx.workspace_id)
    ).all()
    with_thumbnail = sum(1 for thumb, _key, _version in rows if thumb)
    without_original = sum(
        1 for thumb, key, version in rows if not thumb and not (key and version)
    )
    return with_thumbnail, without_original


def _next_batch(
    session: Session,
    ctx: TenantContext,
    *,
    after: tuple[datetime, str] | None,
    batch_size: int,
) -> list[_Candidate]:
    query = (
        select(AssetVersionRecord)
        .where(*_candidate_filters(ctx))
        .order_by(AssetVersionRecord.created_at.asc(), AssetVersionRecord.id.asc())
        .limit(batch_size)
    )
    if after is not None:
        last_created_at, last_id = after
        query = query.where(
            or_(
                AssetVersionRecord.created_at > last_created_at,
                and_(
                    AssetVersionRecord.created_at == last_created_at,
                    AssetVersionRecord.id > last_id,
                ),
            )
        )
    candidates: list[_Candidate] = []
    for version in session.execute(query).scalars():
        candidates.append(
            _Candidate(
                version_id=version.id,
                created_at=version.created_at,
                original=CommittedBlobRef(
                    committed_key=version.committed_object_key or "",
                    storage_version_id=version.committed_storage_version_id or "",
                    size_bytes=version.byte_size or 0,
                    checksum_sha256_b64="",
                    content_type_verified=version.mime_type or "application/octet-stream",
                ),
            )
        )
    return candidates


async def _read_original(blob_store: BlobStore, ref: CommittedBlobRef) -> bytes:
    payload = bytearray()
    async for chunk in blob_store.open_committed(ref):
        payload.extend(chunk)
    return bytes(payload)


def _record(
    session: Session, ctx: TenantContext, version_id: str, thumbnail: CommittedThumbnail
) -> bool:
    """Write the thumbnail onto the version unless someone else already did."""
    version = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if version is None or version.thumbnail_object_key:
        return False
    apply_thumbnail(version, thumbnail)
    session.flush()
    return True


async def backfill_workspace_thumbnails(
    blob_store: BlobStore,
    ctx: TenantContext,
    *,
    batch_size: int = 50,
    limit: int | None = None,
    dry_run: bool = False,
    long_edge: int = THUMBNAIL_LONG_EDGE,
) -> BackfillReport:
    """Derive and record thumbnails for every candidate version in ``ctx``'s Workspace.

    ``limit`` caps how many candidates this call processes (a cautious first
    pass on a big Workspace); the next call continues from the rest.
    ``dry_run`` only counts candidates and writes nothing.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    report = BackfillReport(workspace_id=ctx.workspace_id, dry_run=dry_run)
    with tenant_session(ctx) as session:
        report.with_thumbnail_before, report.without_original = _count_versions(session, ctx)

    after: tuple[datetime, str] | None = None
    while limit is None or report.examined < limit:
        size = batch_size if limit is None else min(batch_size, limit - report.examined)
        with tenant_session(ctx) as session:
            batch = _next_batch(session, ctx, after=after, batch_size=size)
        if not batch:
            break
        for candidate in batch:
            report.examined += 1
            after = (candidate.created_at, candidate.version_id)
            if dry_run:
                continue
            try:
                original = await _read_original(blob_store, candidate.original)
                thumbnail = await commit_thumbnail(
                    blob_store, ctx.workspace_id, original, long_edge=long_edge
                )
                with tenant_session(ctx) as session:
                    recorded = _record(session, ctx, candidate.version_id, thumbnail)
            except Exception as exc:  # noqa: BLE001 -- counted, logged, retried next run
                report.failed += 1
                failure = {
                    "asset_version_id": candidate.version_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }
                if len(report.failures) < _MAX_REPORTED_FAILURES:
                    report.failures.append(failure)
                logger.warning(
                    "asset_thumbnail_backfill_failed",
                    extra={"workspace_id": ctx.workspace_id, **failure},
                )
                continue
            if recorded:
                report.derived += 1
            else:
                report.already_filled += 1
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _workspace_authorization_epoch(workspace_id: str) -> int:
    """The Workspace's current ``authorization_epoch``, or raise if it is unknown.

    The ``workspaces`` registry is under RLS on PostgreSQL and no tenant
    scope exists yet, so the pre-tenant admission function (``SECURITY
    DEFINER``, granted to the api and worker roles) is the only read a
    runtime role has; SQLite has no RLS and reads the row directly.
    """
    with platform_session() as session:
        if is_postgres(session):
            row = session.execute(
                text("SELECT * FROM marketai_workspace_admission(:ws_id)"),
                {"ws_id": workspace_id},
            ).mappings().first()
            epoch = None if row is None else row["authorization_epoch"]
        else:
            epoch = session.scalar(
                text("SELECT authorization_epoch FROM workspaces WHERE id = :ws_id"),
                {"ws_id": workspace_id},
            )
    if epoch is None:
        raise SystemExit(f"Workspace {workspace_id!r} is not provisioned in the domain database")
    return int(epoch)


def backfill_context(workspace_id: str) -> TenantContext:
    """A system context for the backfill, bound to the Workspace's current epoch."""
    return system_context(
        workspace_id,
        initiator_id=None,
        executor=BACKFILL_EXECUTOR,
        authorization_epoch=_workspace_authorization_epoch(workspace_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Derive and record gallery thumbnails for one Workspace's existing "
            "AssetVersions (Issue #159). Idempotent and resumable: re-running "
            "converges, interrupted runs continue where they stopped."
        )
    )
    parser.add_argument(
        "--workspace-id", required=True, action="append", dest="workspace_ids",
        help="Workspace to backfill; repeat the flag to process several in sequence.",
    )
    parser.add_argument("--batch-size", type=int, default=50, help="Versions selected per scan (default 50).")
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many candidates per Workspace.")
    parser.add_argument("--dry-run", action="store_true", help="Count candidates only; write nothing.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    blob_store = build_blob_store()
    reports = []
    for workspace_id in args.workspace_ids:
        ctx = backfill_context(workspace_id)
        report = asyncio.run(
            backfill_workspace_thumbnails(
                blob_store,
                ctx,
                batch_size=args.batch_size,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
        reports.append(report.to_dict())
    json.dump(reports, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if any(report["failed"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
