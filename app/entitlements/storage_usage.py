"""A Workspace's stored bytes, recorded once per UTC day (#148, Usage S5).

Contract §9 counts "私有存储字节数" among the first Usage metrics, and §8 caps
Beta private storage at 10 GiB (enforced by #147, not here). This module
writes the ``storage_bytes`` gauge (``app.entitlements.usage.STORAGE_BYTES``):
one ``usage_events`` row per Workspace per UTC day, stating how many bytes the
Workspace's rows say it stores.

**What is counted.** The byte columns the database already holds, never a
BlobStore listing (:data:`STORAGE_BYTE_COLUMNS`):

* ``asset_versions.byte_size`` at the version's blob key -- its committed key,
  or the legacy ``object_key`` when it has none -- and
  ``asset_versions.thumbnail_byte_size`` at ``thumbnail_object_key``;
* ``reference_objects.byte_size`` at ``original_object_key`` until the
  original is purged, and ``derivative_byte_size`` at
  ``derivative_object_key``;
* ``external_creatives.upload_byte_size`` at ``upload_key``, except for a
  ``rejected`` creative: the reference gate deletes a rejected upload from
  storage, but the creative row keeps naming it;
* ``localization_attempt_outputs.byte_size`` at ``object_key``;
* ``export_packages.byte_size`` at ``committed_object_key``.

A row contributes only when it names a key and a positive size, so a purged
original (its key is cleared), an unbuilt export package and a version
without a thumbnail add nothing.

**The same blob is counted once.** A blob is its object key within the
Workspace: rows that name the same key name the same stored object, whatever
table they are in, and the key counts once, at the largest size any of them
records. Rows share keys in practice: a localized AssetVersion names its
Localization output's object; an external creative's upload is the original
of the ReferenceObject the gate quarantined it as; masters and thumbnails are
committed under content-addressed keys, so identical bytes land on one key.
The storage version id is not part of the identity: production's bucket is
versionless (one object per key), and a version id is either that object's
content revision or empty on rows that never recorded one (a localized
version, a Localization output). The sum reads the Workspace's rows only --
under its RLS, and through an explicit ``workspace_id`` filter on every
table -- so a key is never matched against another Workspace's rows.

**When.** The main Worker lane calls :func:`dispatch_storage_usage_jobs` at
most every :data:`DISPATCH_INTERVAL_SECONDS`, starting one interval after
the process starts. On PostgreSQL the Workspaces are FORCE RLS and the Worker
has no tenant to list them under, so it calls the narrow SECURITY DEFINER
``marketai_storage_usage_workspaces_v1(date, limit)``, EXECUTE-able by
``marketai_domain_worker`` alone: the id and current authorization epoch of
each active, unsuspended Workspace that has no Job for that UTC date and
epoch yet -- nothing else. For each, in a TenantContext signed for that
epoch, one ``usage.storage_bytes`` Job is enqueued, keyed
``usage-storage-bytes:{date}:{epoch}``. The epoch is in the key because the
claim fails a Job whose epoch went stale (``authorization_epoch_stale``):
the next dispatch then gives the Workspace a Job under its new epoch instead
of finding the failed one. SQLite has no RLS and lists the same Workspaces
directly.

The Job (:func:`record_storage_bytes`) sums the bytes and appends one fact in
the claimed Job's signed TenantContext, under RLS. Its semantic key is
``usage:v1:storage_bytes:workspace:{workspace_id}:{UTC date}``, and the date
is the measurement's own ``occurred_at``, so every row's key names the day it
was measured on. The first measurement of a day stands: a second Job that
day (a replay, or one under a new epoch) writes a counted duplicate and no
row. A day with no measurement keeps no row: dispatch only ever asks for
today, so missed days are never backfilled.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from sqlalchemy import String, cast, func, literal, select, text, union_all
from sqlalchemy.orm import Session

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    ExternalCreativeRecord,
    JobRecord,
    LocalizationAttemptOutputRecord,
    ReferenceObjectRecord,
    WorkspaceRecord,
)
from app.entitlements.usage import (
    STORAGE_BYTES,
    WORKSPACE,
    UsageFact,
    UsageRecorder,
    UsageWriteResult,
    usage_semantic_key,
)
from app.generation.models import ExportPackageRecord
from app.platform.jobs import enqueue_in_session
from app.platform.runtime_role import WORKER_ROLE, runs_as
from app.platform.tenancy import TenantContext, is_postgres, system_context

logger = logging.getLogger(__name__)

__all__ = [
    "DISPATCH_BATCH_SIZE",
    "DISPATCH_FUNCTION",
    "DISPATCH_INTERVAL_SECONDS",
    "JOB_TYPE",
    "STORAGE_BYTE_COLUMNS",
    "dispatch_storage_usage_jobs",
    "enqueue_storage_usage_job",
    "record_storage_bytes",
    "storage_usage_job_key",
    "workspace_storage_bytes",
]

JOB_TYPE = "usage.storage_bytes"
DISPATCH_FUNCTION = "public.marketai_storage_usage_workspaces_v1"
#: The main Worker lane dispatches at most this often, and first this long
#: after the process starts. Today's Jobs are therefore enqueued within one
#: interval of 00:00 UTC, or of a Worker start.
DISPATCH_INTERVAL_SECONDS = 300.0
#: Workspaces one dispatch enqueues at most; the rest wait for the next
#: interval. The function caps it at 500.
DISPATCH_BATCH_SIZE = 200
_JOB_KEY_PREFIX = "usage-storage-bytes"

#: Every byte column the sum reads, by table. ``tests/test_usage_storage_bytes``
#: fails when a table gains a byte column that is not listed here.
STORAGE_BYTE_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "asset_versions": ("byte_size", "thumbnail_byte_size"),
    "reference_objects": ("byte_size", "derivative_byte_size"),
    "external_creatives": ("upload_byte_size",),
    "localization_attempt_outputs": ("byte_size",),
    "export_packages": ("byte_size",),
})


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def _stored_blobs(workspace_id: str):
    """``(key, size)`` for every blob a row of the Workspace names, one per row."""
    version = AssetVersionRecord
    reference = ReferenceObjectRecord
    creative = ExternalCreativeRecord
    output = LocalizationAttemptOutputRecord
    package = ExportPackageRecord
    return union_all(
        select(
            func.coalesce(func.nullif(version.committed_object_key, ""), version.object_key).label("key"),
            version.byte_size.label("size"),
        ).where(version.workspace_id == workspace_id),
        select(version.thumbnail_object_key, version.thumbnail_byte_size)
        .where(version.workspace_id == workspace_id),
        select(reference.original_object_key, reference.byte_size)
        .where(reference.workspace_id == workspace_id, reference.original_purged.is_(False)),
        select(reference.derivative_object_key, reference.derivative_byte_size)
        .where(reference.workspace_id == workspace_id),
        select(creative.upload_key, creative.upload_byte_size)
        .where(creative.workspace_id == workspace_id, creative.status != "rejected"),
        select(output.object_key, output.byte_size)
        .where(output.workspace_id == workspace_id),
        select(package.committed_object_key, package.byte_size)
        .where(package.workspace_id == workspace_id),
    ).subquery()


def workspace_storage_bytes(session: Session, context: TenantContext) -> int:
    """The bytes the context's Workspace stores, each blob counted once.

    Runs in the caller's tenant transaction. Each key counts once, at the
    largest size a row records for it; a row without a key or with no
    positive size adds nothing (module docstring).
    """
    blobs = _stored_blobs(context.workspace_id)
    per_blob = (
        select(blobs.c.key, func.max(blobs.c.size).label("size"))
        .where(blobs.c.key.is_not(None), blobs.c.key != "", blobs.c.size > 0)
        .group_by(blobs.c.key)
        .subquery()
    )
    return int(session.scalar(select(func.coalesce(func.sum(per_blob.c.size), 0))))


def record_storage_bytes(
    session: Session,
    context: TenantContext,
    *,
    measured_at: datetime | None = None,
) -> UsageWriteResult:
    """State today's ``storage_bytes`` for the context's Workspace, once.

    ``measured_at`` (now, unless a test fixes it) is the fact's
    ``occurred_at``, and its UTC date is the day in the semantic key. A
    second call on the same UTC day sums again but writes nothing: the key
    is already recorded, so it is a counted duplicate. Runs in the caller's
    transaction and never commits.
    """
    measured_at = (measured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stored = workspace_storage_bytes(session, context)
    fact = UsageFact(
        metric_key=STORAGE_BYTES.key,
        quantity=Decimal(stored),
        unit=STORAGE_BYTES.unit,
        execution_mode="not_applicable",
        semantic_key_hash=usage_semantic_key(
            STORAGE_BYTES.key, WORKSPACE, context.workspace_id, measured_at.date().isoformat(),
        ),
        occurred_at=measured_at,
        source_resource_type=WORKSPACE,
        source_resource_id=context.workspace_id,
    )
    return UsageRecorder().record_many(session, context, [fact])


# ---------------------------------------------------------------------------
# Dispatching
# ---------------------------------------------------------------------------

def storage_usage_job_key(usage_date: date, authorization_epoch: int) -> str:
    """The Job key of one Workspace's measurement for ``usage_date``.

    ``marketai_storage_usage_workspaces_v1`` builds the same string.
    """
    return f"{_JOB_KEY_PREFIX}:{usage_date.isoformat()}:{authorization_epoch}"


def enqueue_storage_usage_job(
    session: Session,
    context: TenantContext,
    *,
    usage_date: date,
) -> tuple[JobRecord, bool]:
    """The context's measurement Job for ``usage_date``, and whether this call created it."""
    key = storage_usage_job_key(usage_date, context.authorization_epoch)
    existed = session.scalar(
        select(JobRecord.id).where(
            JobRecord.workspace_id == context.workspace_id, JobRecord.idempotency_key == key,
        )
    ) is not None
    job = enqueue_in_session(
        session,
        context=context,
        job_type=JOB_TYPE,
        payload={"usage_date": usage_date.isoformat()},
        idempotency_key=key,
        resource_type=WORKSPACE,
        resource_id=context.workspace_id,
    )
    return job, not existed


@runs_as(WORKER_ROLE)
def dispatch_storage_usage_jobs(
    *,
    limit: int = DISPATCH_BATCH_SIZE,
    now: datetime | None = None,
) -> int:
    """Enqueue today's measurement Job for each Workspace that has none; return how many.

    Today is the UTC date of ``now``. Each Workspace's Job is enqueued in its
    own transaction, in a TenantContext signed for the epoch the listing
    returned. A Workspace that fails (its epoch moved since the listing, say)
    is logged and skipped; it is listed again on the next interval, since it
    still has no Job. Replays and concurrent dispatchers converge on one Job
    per Workspace, date and epoch.
    """
    usage_date = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    with platform_session() as session:
        workspaces = _workspaces_without_a_job(session, usage_date, limit)
    created = 0
    for workspace_id, authorization_epoch in workspaces:
        context = system_context(workspace_id, initiator_id=None, authorization_epoch=authorization_epoch)
        try:
            with tenant_session(context) as session:
                _job, new = enqueue_storage_usage_job(session, context, usage_date=usage_date)
        except Exception as exc:  # noqa: BLE001 -- the next interval lists it again
            logger.warning(
                "usage_storage_dispatch_failed",
                extra={"workspace_id": workspace_id, "error": type(exc).__name__},
            )
            continue
        created += int(new)
    return created


def _workspaces_without_a_job(session: Session, usage_date: date, limit: int) -> list[tuple[str, int]]:
    """``(workspace_id, current authorization epoch)`` still owed ``usage_date``'s Job.

    Both branches list the same Workspaces: active, not suspended, and with
    no Job keyed for that date and their current epoch, in id order.
    """
    if is_postgres(session):
        rows = session.execute(
            text(
                "SELECT workspace_id, authorization_epoch "
                f"FROM {DISPATCH_FUNCTION}(CAST(:usage_date AS date), CAST(:dispatch_limit AS integer))"
            ),
            {"usage_date": usage_date, "dispatch_limit": limit},
        ).all()
    else:
        key = (
            literal(f"{_JOB_KEY_PREFIX}:{usage_date.isoformat()}:", String)
            + cast(WorkspaceRecord.authorization_epoch, String)
        )
        dispatched = (
            select(JobRecord.id)
            .where(JobRecord.workspace_id == WorkspaceRecord.id, JobRecord.idempotency_key == key)
            .exists()
        )
        rows = session.execute(
            select(WorkspaceRecord.id, WorkspaceRecord.authorization_epoch)
            .where(
                WorkspaceRecord.lifecycle == "active",
                WorkspaceRecord.suspended.is_(False),
                ~dispatched,
            )
            .order_by(WorkspaceRecord.id)
            .limit(max(0, min(limit, 500)))
        ).all()
    return [(str(workspace_id), int(epoch)) for workspace_id, epoch in rows]
