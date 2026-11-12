"""Usage ``storage_bytes``: one gauge row per Workspace per UTC day (#148, Usage S5).

On SQLite, each test in its own database:

* the registry: ``storage_bytes`` is a gauge, ``set``, in ``byte``, folded by
  its latest value, and recorded as ``not_applicable`` only;
* the sum reads every byte column of the schema -- a table that gains one
  fails here until it is classified -- and each column adds its bytes;
* a blob several rows name (a localized version and its Localization output,
  an external creative's upload and its ReferenceObject, versions sharing a
  content-addressed master or thumbnail) counts once, at its largest size;
  rows that name no stored blob (a purged original, a rejected creative's
  upload, an unbuilt export package) add nothing; another Workspace's rows,
  even under the same key, never count;
* recording: the semantic key is
  ``usage:v1:storage_bytes:workspace:{workspace_id}:{UTC date}``; a second
  recording on the same UTC day is a counted duplicate and leaves one row,
  even when the bytes changed; the next day gets its own row;
* dispatch: one ``usage.storage_bytes`` Job per active, unsuspended
  Workspace and day, keyed by date and authorization epoch; dispatching again
  creates nothing, an epoch change gets a new Job, a missed day is never
  backfilled, and one Workspace that fails does not stop the others;
* the Worker: the main lane first dispatches one interval after it starts,
  then runs each Job in its own Workspace, so each row holds that
  Workspace's bytes only; a second pass the same day adds no row; a failing
  measurement fails its Job with ``usage_storage_bytes_failed``, retryably;
* ``/usage`` and ``UsageQuery``: a gauge's ``total`` is its latest real event
  in the window, never a sum; delta metrics still sum.

The PostgreSQL side -- the narrow function, its privileges, RLS between
Workspaces, and the migration -- is in ``tests/test_usage_storage_bytes_pg.py``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

import app.main  # noqa: F401 -- every model is in the metadata
from app.adapters.persistence.database import Base, tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    AuditEventRecord,
    ExternalCreativeRecord,
    JobRecord,
    LocalizationAttemptOutputRecord,
    ReferenceObjectRecord,
    WorkspaceRecord,
)
from app.entitlements import storage_usage, usage
from app.entitlements.storage_usage import (
    JOB_TYPE,
    STORAGE_BYTE_COLUMNS,
    dispatch_storage_usage_jobs,
    record_storage_bytes,
    storage_usage_job_key,
    workspace_storage_bytes,
)
from app.entitlements.usage import (
    METRICS,
    STORAGE_BYTES,
    UsageFact,
    UsageFactError,
    UsageMetric,
    UsageQuery,
    UsageRecorder,
    usage_semantic_key,
)
from app.entitlements.usage_models import UsageEventRecord
from app.generation.models import ExportPackageRecord
from app.main import app as api
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext, system_context
from app.worker_runtime import WorkerRuntime

DAY = datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)
A, B = "ws_storage_a", "ws_storage_b"
SUSPENDED, PROVISIONING = "ws_storage_suspended", "ws_storage_provisioning"


def observed(test: unittest.TestCase, name: str) -> str:
    """``name``'s logger, switched back on for ``assertLogs``: an Alembic
    migration earlier in the process runs ``fileConfig`` with
    ``disable_existing_loggers`` (see ``tests.test_asset_thumbnails``)."""
    logger = logging.getLogger(name)
    test.addCleanup(setattr, logger, "disabled", logger.disabled)
    logger.disabled = False
    return name


def owner(workspace_id: str) -> TenantContext:
    return TenantContext(workspace_id=workspace_id, role="owner", initiator_id=f"owner_{workspace_id}",
                         request_id="req-storage", trace_id="trace-storage")


def worker(workspace_id: str, epoch: int = 0) -> TenantContext:
    return system_context(workspace_id, initiator_id=None, authorization_epoch=epoch)


class StorageTestCase(unittest.TestCase):
    """A database of its own: the dispatcher lists every Workspace there is."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="marketai-storage-usage-")
        self.addCleanup(temp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(temp.name) / 'storage.db'}")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        for target, value in (("app.adapters.persistence.database.SessionLocal", sessions),
                              ("app.platform.jobs.engine", self.engine)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with Session(self.engine) as session, session.begin():
            session.add_all([
                WorkspaceRecord(id=A, name="Acme Charge"),
                WorkspaceRecord(id=B, name="Brightvolt"),
                WorkspaceRecord(id=SUSPENDED, name="Northpeak", suspended=True),
                WorkspaceRecord(id=PROVISIONING, name="Kestrel Power", lifecycle="provisioning"),
            ])
        self.serial = 0

    # -- seeding ------------------------------------------------------------

    @contextmanager
    def _unchecked(self):
        """A session without foreign keys: the sum reads byte columns, not parents."""
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            try:
                with Session(bind=connection) as session:
                    yield session
                    session.commit()
                connection.commit()
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()

    def _id(self, prefix: str) -> str:
        self.serial += 1
        return f"{prefix}_{self.serial}"

    def version(self, workspace_id: str, size: int, *, key: str | None = None, committed: str | None = None,
                thumbnail: tuple[str | None, int | None] | None = None) -> None:
        version_id = self._id("av")
        with self._unchecked() as session:
            session.add(AssetVersionRecord(
                id=version_id, workspace_id=workspace_id, asset_id=self._id("asset"), kind="generated",
                aspect_ratio="1:1", object_key=key if key is not None else f"outputs/{version_id}.png",
                committed_object_key=committed, content_sha256=f"{self.serial:064x}", byte_size=size,
                mime_type="image/png", origin_kind="generation", execution_mode="mock",
                thumbnail_object_key=thumbnail[0] if thumbnail else None,
                thumbnail_byte_size=thumbnail[1] if thumbnail else None,
            ))

    def reference(self, workspace_id: str, size: int, *, original: str, derivative: str = "",
                  derivative_size: int = 0, purged: bool = False, state: str = "released") -> None:
        with self._unchecked() as session:
            session.add(ReferenceObjectRecord(
                id=self._id("refobj"), workspace_id=workspace_id, content_sha256=f"{self.serial:064x}",
                original_object_key=original, declared_mime="image/png", byte_size=size, state=state,
                original_purged=purged, derivative_object_key=derivative, derivative_byte_size=derivative_size,
            ))

    def creative(self, workspace_id: str, size: int, *, key: str, status: str = "ready") -> None:
        with self._unchecked() as session:
            session.add(ExternalCreativeRecord(
                id=self._id("xc"), workspace_id=workspace_id, product_id="prod_acme", status=status,
                upload_intent_id=self._id("ui"), upload_key=key, upload_sha256=f"{self.serial:064x}",
                upload_mime_type="image/png", upload_byte_size=size, made_by="self", execution_mode="mock",
                vocabulary_hash="0" * 64, batch_key_hash="0" * 64, request_fingerprint="0" * 64,
            ))

    def output(self, workspace_id: str, size: int, *, key: str) -> None:
        with self._unchecked() as session:
            session.add(LocalizationAttemptOutputRecord(
                id=self._id("lao"), workspace_id=workspace_id, attempt_id=self._id("la"), item_id="li_1",
                batch_id="lb_1", object_key=key, content_sha256=f"{self.serial:064x}", byte_size=size,
                mime_type="image/png", output_evidence_hash="0" * 64,
            ))

    def package(self, workspace_id: str, size: int, *, key: str, state: str = "ready") -> None:
        with self._unchecked() as session:
            session.add(ExportPackageRecord(
                id=self._id("exp"), workspace_id=workspace_id, operation_id=self._id("op"), actor_id="owner",
                selector_json={}, selected_versions_json={}, execution_mode="mock", state=state,
                committed_object_key=key, byte_size=size,
            ))

    # -- reading ------------------------------------------------------------

    def stored(self, workspace_id: str) -> int:
        context = worker(workspace_id)
        with tenant_session(context) as session:
            return workspace_storage_bytes(session, context)

    def rows(self, workspace_id: str) -> list[UsageEventRecord]:
        with Session(self.engine) as session:
            rows = list(session.scalars(
                select(UsageEventRecord)
                .where(UsageEventRecord.workspace_id == workspace_id,
                       UsageEventRecord.metric_key == "storage_bytes")
                .order_by(UsageEventRecord.occurred_at)))
            for row in rows:
                session.expunge(row)
            return rows

    def storage_jobs(self, workspace_id: str | None = None) -> list[JobRecord]:
        with Session(self.engine) as session:
            query = select(JobRecord).where(JobRecord.type == JOB_TYPE).order_by(JobRecord.created_at, JobRecord.id)
            if workspace_id is not None:
                query = query.where(JobRecord.workspace_id == workspace_id)
            rows = list(session.scalars(query))
            for row in rows:
                session.expunge(row)
            return rows

    def record(self, workspace_id: str, measured_at: datetime, epoch: int = 0):
        context = worker(workspace_id, epoch)
        with tenant_session(context) as session:
            return record_storage_bytes(session, context, measured_at=measured_at)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class RegistryTests(unittest.TestCase):
    def test_storage_bytes_is_a_latest_valued_gauge_of_bytes_with_no_execution_mode(self):
        self.assertIs(METRICS["storage_bytes"], STORAGE_BYTES)
        self.assertEqual(
            (STORAGE_BYTES.measurement_kind, STORAGE_BYTES.direction, STORAGE_BYTES.unit,
             STORAGE_BYTES.aggregation, STORAGE_BYTES.execution_modes),
            ("gauge", "set", "byte", "latest", ("not_applicable",)))
        for key, metric in METRICS.items():  # the delta metrics keep every bucket
            if key != "storage_bytes":
                self.assertEqual(metric.execution_modes, usage.USAGE_EXECUTION_MODES, key)

    def test_the_registry_refuses_storage_bytes_in_another_bucket_or_unit(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine, tables=[WorkspaceRecord.__table__, UsageEventRecord.__table__])
        context = owner(A)
        with Session(engine) as session:
            session.add(WorkspaceRecord(id=A, name="Acme Charge"))
            session.commit()

            def fact(**overrides) -> UsageFact:
                values = dict(metric_key="storage_bytes", quantity=Decimal("10"), unit="byte",
                              execution_mode="not_applicable", occurred_at=DAY,
                              semantic_key_hash=usage_semantic_key("storage_bytes", "workspace", A, "2026-11-12"),
                              source_resource_type="workspace", source_resource_id=A)
                values.update(overrides)
                return UsageFact(**values)

            for overrides, code in (({"execution_mode": "byok"}, "usage_execution_mode_mismatch"),
                                    ({"execution_mode": "mock"}, "usage_execution_mode_mismatch"),
                                    ({"unit": "gibibyte"}, "usage_unit_mismatch"),
                                    ({"quantity": Decimal("-1")}, "usage_quantity_negative")):
                with self.subTest(**{k: str(v) for k, v in overrides.items()}):
                    with self.assertRaises(UsageFactError) as caught:
                        UsageRecorder().record_many(session, context, [fact(**overrides)])
                    self.assertEqual(caught.exception.code, code)
            self.assertEqual(UsageRecorder().record_many(session, context, [fact()]).written, 1)


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

class MeasureTests(StorageTestCase):
    def test_every_byte_column_of_the_schema_is_classified(self):
        """A table that gains a byte column fails here until the sum reads it (or says why not)."""
        found: dict[str, set[str]] = {}
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if re.search(r"bytes?(_|$)|byte_size|size_bytes", column.name):
                    found.setdefault(table.name, set()).add(column.name)
        self.assertEqual(found, {table: set(columns) for table, columns in STORAGE_BYTE_COLUMNS.items()})

    def test_each_byte_column_adds_its_bytes(self):
        steps = (
            ("asset_versions.byte_size", 1_000,
             lambda: self.version(A, 1_000, committed=f"committed/{A}/asset/master-1.png")),
            ("asset_versions.thumbnail_byte_size", 30,
             lambda: self.version(A, 2_000, committed=f"committed/{A}/asset/master-2.png",
                                  thumbnail=(f"committed/{A}/thumb/t-2.jpg", 30))),
            ("reference_objects.byte_size", 400,
             lambda: self.reference(A, 400, original=f"committed/{A}/reference_asset/r-1.png")),
            ("reference_objects.derivative_byte_size", 50 + 70,
             lambda: self.reference(A, 70, original=f"committed/{A}/reference_asset/r-2.png",
                                    derivative=f"committed/{A}/derivatives/d-2.jpg", derivative_size=50)),
            ("external_creatives.upload_byte_size", 5_000,
             lambda: self.creative(A, 5_000, key=f"committed/{A}/external_creative/x-1.png")),
            ("localization_attempt_outputs.byte_size", 600,
             lambda: self.output(A, 600, key=f"committed/{A}/localization/o-1.png")),
            ("export_packages.byte_size", 9_000,
             lambda: self.package(A, 9_000, key=f"committed/{A}/export/p-1.zip")),
        )
        total = 0
        self.assertEqual(self.stored(A), 0)
        for column, added, seed in steps:
            with self.subTest(column=column):
                seed()
                if column == "asset_versions.thumbnail_byte_size":
                    added += 2_000  # the version that carries the thumbnail
                total += added
                self.assertEqual(self.stored(A), total)
        covered = {step[0] for step in steps}
        self.assertEqual(covered, {f"{t}.{c}" for t, cs in STORAGE_BYTE_COLUMNS.items() for c in cs})

    def test_a_blob_several_rows_name_counts_once(self):
        master = f"committed/{A}/asset/sha-master.png"
        thumb = f"committed/{A}/thumb/sha-thumb.jpg"
        upload = f"committed/{A}/external_creative/upload-1.png"
        localized = f"committed/{A}/localization/out-1.png"
        # Two versions on one content-addressed master and thumbnail (one has
        # object_key == committed_object_key, as uploads and edits write it).
        self.version(A, 1_000, key=master, committed=master, thumbnail=(thumb, 40))
        self.version(A, 1_000, committed=master, thumbnail=(thumb, 40))
        # An external creative's upload is the original of its ReferenceObject.
        self.creative(A, 700, key=upload)
        self.reference(A, 700, original=upload)
        # A localized version names its Localization output's object.
        self.output(A, 300, key=localized)
        self.version(A, 300, key=localized)
        self.assertEqual(self.stored(A), 1_000 + 40 + 700 + 300)

    def test_a_key_recorded_at_two_sizes_counts_at_the_largest(self):
        shared = f"committed/{A}/asset/shared.png"
        self.version(A, 900, committed=shared)
        self.output(A, 1_200, key=shared)
        self.assertEqual(self.stored(A), 1_200)

    def test_rows_that_name_no_stored_blob_add_nothing(self):
        self.reference(A, 500, original="", purged=True, state="rejected")             # purge cleared the key
        self.reference(A, 600, original=f"committed/{A}/reference_asset/flagged.png",  # flagged, key kept
                       purged=True, state="rejected")
        self.creative(A, 800, key=f"committed/{A}/external_creative/rejected.png", status="rejected")
        self.package(A, 0, key="", state="pending")                                     # not built yet
        self.package(A, 0, key="", state="failed")
        self.version(A, 100, committed=f"committed/{A}/asset/no-thumb.png", thumbnail=(None, None))
        self.version(A, 200, committed=f"committed/{A}/asset/thumb-no-size.png",
                     thumbnail=(f"committed/{A}/thumb/x.jpg", None))
        self.assertEqual(self.stored(A), 100 + 200)
        # A rejected original whose purge failed is still stored, and counted.
        self.reference(A, 650, original=f"committed/{A}/reference_asset/stuck.png", state="rejected")
        self.assertEqual(self.stored(A), 100 + 200 + 650)

    def test_a_legacy_version_counts_at_its_object_key(self):
        self.version(A, 123, key="outputs/legacy/one.png")
        self.version(A, 123, key="outputs/legacy/one.png", committed="")  # an empty committed key is none
        self.assertEqual(self.stored(A), 123)

    def test_another_workspace_s_rows_never_count_even_under_the_same_key(self):
        shared = "committed/shared/asset/same.png"
        self.version(A, 100, committed=shared)
        self.version(B, 7_000, committed=shared)
        self.reference(B, 8_000, original="committed/b/reference_asset/r.png")
        self.package(B, 9_000, key="committed/b/export/p.zip")
        self.assertEqual(self.stored(A), 100)
        self.assertEqual(self.stored(B), 7_000 + 8_000 + 9_000)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

class RecordTests(StorageTestCase):
    def test_a_measurement_is_one_gauge_row_keyed_by_workspace_and_utc_day(self):
        self.reference(A, 4_096, original=f"committed/{A}/reference_asset/r.png")
        result = self.record(A, DAY)
        self.assertEqual((result.written, result.duplicates), (1, 0))
        row, = self.rows(A)
        self.assertEqual(
            (row.metric_key, row.measurement_kind, row.direction, row.unit, row.execution_mode,
             row.source_resource_type, row.source_resource_id, Decimal(row.quantity)),
            ("storage_bytes", "gauge", "set", "byte", "not_applicable", "workspace", A, Decimal("4096")))
        self.assertEqual(row.semantic_key_hash, usage_semantic_key("storage_bytes", "workspace", A, "2026-11-12"))
        self.assertEqual(row.occurred_at.replace(tzinfo=timezone.utc), DAY)

    def test_a_second_measurement_the_same_utc_day_is_a_duplicate_even_when_bytes_changed(self):
        self.reference(A, 100, original=f"committed/{A}/reference_asset/r1.png")
        self.record(A, DAY)
        self.reference(A, 900, original=f"committed/{A}/reference_asset/r2.png")
        for at in (DAY + timedelta(hours=1), DAY.replace(hour=23, minute=59, second=59)):
            with self.subTest(at=at.isoformat()):
                again = self.record(A, at)
                self.assertEqual((again.written, again.duplicates), (0, 1))
        row, = self.rows(A)
        self.assertEqual(Decimal(row.quantity), Decimal("100"), "the first measurement of the day stands")

        tomorrow = self.record(A, DAY.replace(hour=0, minute=0, second=0) + timedelta(days=1))
        self.assertEqual(tomorrow.written, 1)
        self.assertEqual([Decimal(r.quantity) for r in self.rows(A)], [Decimal("100"), Decimal("1000")])

    def test_the_day_is_the_measurement_s_utc_date(self):
        # 2026-11-12 23:30 in UTC-05:00 is 2026-11-13 04:30 UTC.
        self.record(A, datetime(2026, 11, 12, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
        row, = self.rows(A)
        self.assertEqual(row.semantic_key_hash, usage_semantic_key("storage_bytes", "workspace", A, "2026-11-13"))

    def test_an_empty_workspace_records_zero(self):
        self.record(A, DAY)
        row, = self.rows(A)
        self.assertEqual(Decimal(row.quantity), Decimal("0"))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class DispatchTests(StorageTestCase):
    def test_one_job_per_active_workspace_and_day(self):
        self.assertEqual(dispatch_storage_usage_jobs(now=DAY), 2)
        jobs_ = self.storage_jobs()
        self.assertEqual(sorted(job.workspace_id for job in jobs_), [A, B], "suspended and provisioning are skipped")
        for job in jobs_:
            with self.subTest(workspace=job.workspace_id):
                self.assertEqual(
                    (job.state, job.idempotency_key, job.payload_json, job.resource_type, job.resource_id,
                     job.authorization_epoch, job.initiator_role, job.safe_to_retry_after_crash),
                    ("queued", "usage-storage-bytes:2026-11-12:0", {"usage_date": "2026-11-12"}, "workspace",
                     job.workspace_id, 0, "system", True))
        with Session(self.engine) as session:
            enqueued = session.scalars(select(AuditEventRecord.resource_id).where(
                AuditEventRecord.event_type == "job.enqueued")).all()
        self.assertEqual(sorted(enqueued), sorted(job.id for job in jobs_))

        self.assertEqual(dispatch_storage_usage_jobs(now=DAY + timedelta(hours=5)), 0, "today is dispatched")
        self.assertEqual(len(self.storage_jobs()), 2)

    def test_the_next_day_gets_its_own_jobs_and_a_missed_day_is_never_backfilled(self):
        dispatch_storage_usage_jobs(now=DAY)
        self.assertEqual(dispatch_storage_usage_jobs(now=DAY + timedelta(days=2)), 2)  # the 13th is missed
        self.assertEqual(sorted({job.idempotency_key for job in self.storage_jobs(A)}),
                         ["usage-storage-bytes:2026-11-12:0", "usage-storage-bytes:2026-11-14:0"])

    def test_an_epoch_change_gets_the_workspace_a_job_under_its_new_epoch(self):
        dispatch_storage_usage_jobs(now=DAY)
        with Session(self.engine) as session, session.begin():
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == A).values(authorization_epoch=3))
        self.assertEqual(dispatch_storage_usage_jobs(now=DAY), 1)
        self.assertEqual([(job.idempotency_key, job.authorization_epoch) for job in self.storage_jobs(A)],
                         [("usage-storage-bytes:2026-11-12:0", 0), ("usage-storage-bytes:2026-11-12:3", 3)])

    def test_the_limit_bounds_one_dispatch_and_the_rest_wait_for_the_next(self):
        self.assertEqual(dispatch_storage_usage_jobs(now=DAY, limit=1), 1)
        self.assertEqual([job.workspace_id for job in self.storage_jobs()], [A])  # id order
        self.assertEqual(dispatch_storage_usage_jobs(now=DAY, limit=1), 1)
        self.assertEqual(sorted(job.workspace_id for job in self.storage_jobs()), [A, B])

    def test_a_workspace_that_fails_does_not_stop_the_others(self):
        real = storage_usage.enqueue_storage_usage_job

        def flaky(session, context, *, usage_date):
            if context.workspace_id == A:
                raise RuntimeError("database hiccup")
            return real(session, context, usage_date=usage_date)

        with patch.object(storage_usage, "enqueue_storage_usage_job", flaky), \
             self.assertLogs(observed(self, "app.entitlements.storage_usage"), "WARNING") as logs:
            self.assertEqual(dispatch_storage_usage_jobs(now=DAY), 1)
        self.assertIn("usage_storage_dispatch_failed", logs.output[0])
        self.assertEqual([job.workspace_id for job in self.storage_jobs()], [B])
        self.assertEqual(dispatch_storage_usage_jobs(now=DAY), 1, "A is listed again")
        self.assertEqual(sorted(job.workspace_id for job in self.storage_jobs()), [A, B])

    def test_the_job_key_is_the_one_the_function_builds(self):
        self.assertEqual(storage_usage_job_key(date(2026, 1, 5), 12), "usage-storage-bytes:2026-01-05:12")


# ---------------------------------------------------------------------------
# The Worker
# ---------------------------------------------------------------------------

class WorkerPathTests(StorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runtime = WorkerRuntime(owner="ci-only-storage-usage")

    def drain(self) -> int:
        handled = 0
        for _ in range(20):
            if not asyncio.run(self.runtime.run_once()):
                return handled
            handled += 1
        self.fail("the Worker did not go idle")

    def due(self) -> None:
        """Move the runtime's next dispatch to now."""
        self.runtime._next_storage_usage_dispatch_at = 0.0

    def test_the_main_lane_first_dispatches_one_interval_after_it_starts(self):
        start = self.runtime._next_storage_usage_dispatch_at
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.storage_jobs(), [], "nothing before the first interval")
        later = SimpleNamespace(monotonic=lambda: start + 1)  # the runtime's clock, one interval on
        with patch("app.worker_runtime.time", later):
            self.assertEqual(self.drain(), 2)
        self.assertEqual(len(self.rows(A)) + len(self.rows(B)), 2)
        self.assertGreater(self.runtime._next_storage_usage_dispatch_at, start)

    def test_each_job_records_its_own_workspace_s_bytes_once_a_day(self):
        self.reference(A, 1_111, original=f"committed/{A}/reference_asset/a.png")
        self.package(B, 2_222, key=f"committed/{B}/export/b.zip")
        self.due()
        self.assertEqual(self.drain(), 2)
        self.assertEqual([Decimal(r.quantity) for r in self.rows(A)], [Decimal("1111")])
        self.assertEqual([Decimal(r.quantity) for r in self.rows(B)], [Decimal("2222")])
        self.assertEqual({job.state for job in self.storage_jobs()}, {"succeeded"})
        self.assertEqual(self.rows(SUSPENDED) + self.rows(PROVISIONING), [])

        self.due()  # the next interval, the same day: nothing new
        self.assertEqual(self.drain(), 0)
        self.assertEqual(len(self.storage_jobs()), 2)

        # Another Job for A the same day (one under a new epoch, say) runs, and
        # its fact is a duplicate, not a second row.
        context = worker(A)
        with tenant_session(context) as session:
            jobs.enqueue_in_session(session, context=context, job_type=JOB_TYPE,
                                    payload={"usage_date": "replay"}, idempotency_key="usage-storage-bytes:replay",
                                    resource_type="workspace", resource_id=A)
        with self.assertLogs(observed(self, "app.entitlements.usage"), "INFO") as logs:
            self.assertEqual(self.drain(), 1)
        self.assertTrue(any("usage_event_duplicate" in line for line in logs.output))
        self.assertEqual(len(self.rows(A)), 1)
        self.assertEqual({job.state for job in self.storage_jobs()}, {"succeeded"})

    def test_a_measurement_that_fails_fails_its_job_retryably(self):
        self.due()
        with patch("app.entitlements.storage_usage.workspace_storage_bytes",
                   side_effect=RuntimeError("disk on fire")):
            asyncio.run(self.runtime.run_once())  # dispatch, then the first Job
        failed = [job for job in self.storage_jobs() if job.state != "queued"]
        self.assertEqual([(job.state, job.error_code, job.attempt) for job in failed],
                         [("retry_wait", "usage_storage_bytes_failed", 1)])
        self.assertIn("RuntimeError: disk on fire", failed[0].error_detail)
        self.assertEqual(self.rows(failed[0].workspace_id), [])

    def test_a_dispatch_failure_is_reported_and_the_lane_still_claims_jobs(self):
        self.due()
        with patch("app.entitlements.storage_usage.dispatch_storage_usage_jobs",
                   side_effect=RuntimeError("no database")), \
             patch("app.platform.ops_alerts.emit") as emit:
            self.assertFalse(asyncio.run(self.runtime.run_once()))
        self.assertIn("Storage usage dispatch failed; retrying next interval",
                      [call.args[2] for call in emit.call_args_list])
        self.assertEqual(self.storage_jobs(), [])


# ---------------------------------------------------------------------------
# Reading: UsageQuery and GET /api/v1/usage
# ---------------------------------------------------------------------------

class LatestValueTests(StorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        for day, size in ((10, 100), (11, 50), (12, 70)):
            self.reference(A, size, original=f"committed/{A}/reference_asset/day-{day}.png")
            measured = datetime(2026, 11, day, 6, tzinfo=timezone.utc)
            # Each day measures what is stored then: drop the earlier rows' bytes.
            with Session(self.engine) as session, session.begin():
                session.execute(update(ReferenceObjectRecord)
                                .where(ReferenceObjectRecord.workspace_id == A,
                                       ReferenceObjectRecord.original_object_key != f"committed/{A}/reference_asset/day-{day}.png")
                                .values(original_object_key="", original_purged=True))
            self.record(A, measured)
        self.record(B, datetime(2026, 11, 12, 7, tzinfo=timezone.utc))  # B stores nothing

    def summarize(self, start: datetime, end: datetime, workspace_id: str = A):
        context = owner(workspace_id)
        with tenant_session(context) as session:
            return UsageQuery().summarize(session, context, metric_key="storage_bytes",
                                          window_start=start, window_end=end)

    def test_the_total_is_the_latest_value_in_the_window_never_a_sum(self):
        self.assertEqual([Decimal(r.quantity) for r in self.rows(A)], [100, 50, 70])
        for start, end, expected in (
            (datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 30, tzinfo=timezone.utc), 70),
            (datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 12, 6, tzinfo=timezone.utc), 50),
            (datetime(2026, 11, 10, 6, tzinfo=timezone.utc), datetime(2026, 11, 10, 7, tzinfo=timezone.utc), 100),
            (datetime(2026, 11, 13, tzinfo=timezone.utc), datetime(2026, 11, 14, tzinfo=timezone.utc), 0),
        ):
            with self.subTest(start=start.isoformat(), end=end.isoformat()):
                summary = self.summarize(start, end)
                self.assertEqual(summary.total, Decimal(expected))
                self.assertEqual(dict(summary.by_execution_mode), {
                    "byok": 0, "sandbox": 0, "mock": 0, "not_applicable": Decimal(expected)})

    def test_get_usage_returns_the_latest_storage_bytes(self):
        current = {"context": owner(A)}
        api.dependency_overrides[request_tenant_context] = lambda: current["context"]
        self.addCleanup(api.dependency_overrides.pop, request_tenant_context, None)
        client = TestClient(api)
        self.addCleanup(client.close)
        window = {"metric_key": "storage_bytes", "window_start": "2026-11-01T00:00:00+00:00",
                  "window_end": "2026-11-30T00:00:00+00:00"}
        for context, expected in ((owner(A), "70"), (owner(B), "0"),
                                  (TenantContext(workspace_id=A, role="member", initiator_id="member_a",
                                                 request_id="req", trace_id="trace"), "70")):
            with self.subTest(workspace=context.workspace_id, role=context.role):
                current["context"] = context
                response = client.get("/api/v1/usage", params=window)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual((body["metric_key"], body["workspace_id"]), ("storage_bytes", context.workspace_id))
                self.assertEqual(Decimal(body["total"]), Decimal(expected))
                self.assertEqual({k: Decimal(v) for k, v in body["by_execution_mode"].items()},
                                 {"byok": 0, "sandbox": 0, "mock": 0, "not_applicable": Decimal(expected)})

    def test_a_delta_metric_still_sums(self):
        context = owner(A)
        facts = [UsageFact(metric_key="provider_calls", quantity=Decimal("1"), unit="call", execution_mode="byok",
                           semantic_key_hash=usage_semantic_key("provider_calls", "provider_call_attempt", f"pca_{i}"),
                           occurred_at=DAY, source_resource_type="provider_call_attempt",
                           source_resource_id=f"pca_{i}") for i in range(3)]
        with tenant_session(context) as session:
            UsageRecorder().record_many(session, context, facts)
            summary = UsageQuery().summarize(session, context, metric_key="provider_calls",
                                             window_start=DAY - timedelta(hours=1), window_end=DAY + timedelta(hours=1))
        self.assertEqual(summary.total, Decimal("3"))

    def test_a_gauge_takes_each_bucket_s_latest_and_keeps_mock_out_of_the_total(self):
        """The fold ``latest`` serves any gauge: ``storage_bytes`` records one bucket only."""
        level = UsageMetric(key="probe_level", measurement_kind="gauge", direction="set", unit="seat",
                            aggregation="latest")
        context = owner(A)
        rows = (("byok", 5, 1), ("byok", 7, 2), ("sandbox", 3, 3), ("mock", 99, 4), ("not_applicable", 4, 0))
        with patch.object(usage, "METRICS", MappingProxyType({**METRICS, level.key: level})):
            with tenant_session(context) as session:
                UsageRecorder().record_many(session, context, [
                    UsageFact(metric_key=level.key, quantity=Decimal(quantity), unit="seat", execution_mode=mode,
                              semantic_key_hash=usage_semantic_key(level.key, "probe", f"{mode}_{hour}"),
                              occurred_at=DAY + timedelta(hours=hour))
                    for mode, quantity, hour in rows])
                summary = UsageQuery().summarize(session, context, metric_key=level.key,
                                                 window_start=DAY, window_end=DAY + timedelta(days=1))
        self.assertEqual(dict(summary.by_execution_mode),
                         {"byok": 7, "sandbox": 3, "mock": 99, "not_applicable": 4})
        self.assertEqual(summary.total, Decimal("3"), "the latest real event; the later mock one is left out")


if __name__ == "__main__":
    unittest.main()
