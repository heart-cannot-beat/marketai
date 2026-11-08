"""Job types with no external call come back from a Worker crash (#22).

Six Job types used to be left ``uncertain`` / ``paid_call_outcome_unknown``
when their Worker died after ``mark_running``, although none of them makes a
paid or external call. They are now registered ``requeue``
(``app.platform.job_policy``), and each handler is safe to run again:

- it checks for a terminal state first and then only closes the Job;
- its final write happens once, under the resource's or the Operation's row
  lock, so two runs of one Job (a lease that expired mid-run) write once.

Every type runs the same three cases against its own fixture: a crash right
after ``mark_running``; a crash after the work committed but before the Job
closed; and a second Worker that takes the expired lease over while the
first is still inside the handler. This module drives the SQLite recovery
path (``recover_expired_jobs``); ``tests/test_postgres_architecture.py``
drives the same fixtures through ``marketai_claim_next_job_v2`` with the
real Worker role.

The paid control: ``generate_asset``, ``generation.run_item`` and
``launch_provisioning`` still end ``uncertain`` with
``paid_call_outcome_unknown``.
"""
from __future__ import annotations

import asyncio
import itertools
import random
import tempfile
import threading
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select, update

from tests import test_export_worker as export_tests
from tests import test_localization_worker as localization_tests
from tests import test_scale_exports as scale_export_tests
from tests.support.library_fixtures import FUTURE, LibraryFixtures, png_bytes
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_library_reference_commands import _provision_finalized_intent

from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    AuditEventRecord,
    JobRecord,
    LibraryAuditRecord,
    LocalizationBatchRecord,
    LocalizationItemRecord,
    LocalizationQcRecordRecord,
    ReferenceObjectRecord,
    utcnow,
)
from app.generation.export import create_export_package
from app.generation.models import ExportPackageRecord
from app.library import references
from app.library.reference_commands import create_reference_from_upload_intent
from app.platform import jobs
from app.platform.job_outcomes import HandlerOutcomeSignal
from app.platform.operations import (
    OperationRecord,
    OperationState,
    OperationStateEvent,
    transition_operation,
)
from app.platform.tenancy import TenantContext
from app.scale.exports import create_export
from app.scale.models import ScaleExportRecord
from app.scale.worker import ScaleWorkerRuntime
from app.worker_runtime import WorkerRuntime

LEASE_LOST = "任务租约已丢失"


class WorkerDied(BaseException):
    """The Worker process stopped: no ``except Exception`` in a handler sees it."""


class CrashRecoveryFixture:
    """What each Job type's fixture provides; the cases below use only this.

    ``expire_lease`` and ``recover`` are the two seams the PostgreSQL tests
    replace: there the lease is expired as the schema owner and recovery is
    ``marketai_claim_next_job_v2`` running as the Worker role.
    """

    JOB_TYPE: str
    #: Patched so the first Worker pauses inside the handler -- after its
    #: lock and terminal check, before its first write -- while a second
    #: Worker takes the expired lease over. ``INTERLEAVE_AFTER`` pauses once
    #: the patched call has returned rather than before it runs.
    INTERLEAVE_AT: str
    INTERLEAVE_AFTER = False
    job_id: str

    def seed(self) -> None:
        raise NotImplementedError

    def runtime(self, owner: str):
        raise NotImplementedError

    def run_once(self, runtime) -> bool:
        return asyncio.run(runtime.run_once(job_types=(self.JOB_TYPE,)))

    def observe(self) -> dict:
        """Everything the Job writes, as counts and states."""
        raise NotImplementedError

    def assert_settled_once(self, observed: dict) -> None:
        raise NotImplementedError

    def workspace_id(self) -> str:
        raise NotImplementedError

    def crash_partway(self, claimed: jobs.ClaimedJob) -> None:
        """What the dead Worker committed after ``mark_running``; default nothing."""

    # -- the queue, seen from outside ----------------------------------

    def job_row(self) -> JobRecord:
        with platform_session() as session:
            job = session.get(JobRecord, self.job_id)
            session.expunge(job)
            return job

    def job_events(self) -> list[str]:
        with platform_session() as session:
            return list(session.scalars(
                select(AuditEventRecord.event_type)
                .where(AuditEventRecord.resource_type == "job", AuditEventRecord.resource_id == self.job_id)
            ))

    def expire_lease(self) -> None:
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == self.job_id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )

    def recover(self) -> None:
        jobs.recover_expired_jobs()

    def drain(self, owner: str | None = None) -> int:
        quarantine_foreign_jobs(self.workspace_id())
        runtime = self.runtime(owner or f"crash-drain-{uuid.uuid4().hex[:6]}")
        ran = 0
        while self.run_once(runtime):
            ran += 1
        return ran

    def crash_after_mark_running(self) -> jobs.ClaimedJob:
        """A Worker took the Job, started it and died; its lease has run out."""
        quarantine_foreign_jobs(self.workspace_id())
        claimed = jobs.claim_next("crashed-worker", job_types=(self.JOB_TYPE,))
        assert claimed is not None and claimed.id == self.job_id, claimed
        jobs.mark_running(claimed.context, claimed.id, "crashed-worker")
        self.crash_partway(claimed)
        self.expire_lease()
        return claimed

    def crash_before_closing_the_job(self) -> None:
        """A Worker finished the work, then died before ``jobs.succeed``."""
        quarantine_foreign_jobs(self.workspace_id())
        runtime = self.runtime("dies-before-closing")
        with patch("app.platform.jobs.succeed", side_effect=WorkerDied("killed")):
            try:
                self.run_once(runtime)
            except WorkerDied:
                pass
            else:
                raise AssertionError("the Worker was expected to die before closing the Job")
        self.expire_lease()

    def run_two_workers(self, wait_for_second=None) -> tuple[BaseException | None, BaseException | None]:
        """Worker A pauses mid-handler; its lease runs out and Worker B takes over.

        On SQLite B runs to the end while A waits (A has not written yet).
        On PostgreSQL B blocks on the row lock A holds; ``wait_for_second``
        returns once it is waiting, A finishes, and B then finds the work
        done. Returns what each Worker raised, if anything.
        """
        quarantine_foreign_jobs(self.workspace_id())
        first = self.runtime("first-worker")
        second = self.runtime("second-worker")
        outcome: dict[str, BaseException | None] = {"first": None, "second": None}
        local = threading.local()
        target_module, _, target_name = self.INTERLEAVE_AT.rpartition(".")
        module = __import__(target_module, fromlist=[target_name])
        original = getattr(module, target_name)
        state = {"armed": True, "thread": None}

        def run_second():
            local.passthrough = True
            try:
                self.run_once(second)
            except BaseException as exc:  # noqa: BLE001 -- reported to the test
                outcome["second"] = exc

        def take_over():
            if not state["armed"] or getattr(local, "passthrough", False):
                return
            state["armed"] = False
            self.expire_lease()
            thread = threading.Thread(target=run_second, name="second-worker")
            state["thread"] = thread
            thread.start()
            if wait_for_second is None:
                thread.join(timeout=30)
            else:
                wait_for_second(thread)

        after = self.INTERLEAVE_AFTER
        if asyncio.iscoroutinefunction(original):
            async def interleaved(*args, **kwargs):
                if not after:
                    take_over()
                result = await original(*args, **kwargs)
                if after:
                    take_over()
                return result
        else:
            def interleaved(*args, **kwargs):
                if not after:
                    take_over()
                result = original(*args, **kwargs)
                if after:
                    take_over()
                return result

        with patch.object(module, target_name, interleaved):
            try:
                self.run_once(first)
            except BaseException as exc:  # noqa: BLE001 -- reported to the test
                outcome["first"] = exc
            thread = state["thread"]
            if thread is not None:
                thread.join(timeout=60)
        assert thread is not None, f"{self.INTERLEAVE_AT} was never reached"
        assert not thread.is_alive(), "the second Worker never finished"
        return outcome["first"], outcome["second"]


class CrashRecoveryCases(CrashRecoveryFixture):
    """The three cases every requeued Job type must pass (SQLite path)."""

    def test_the_job_is_enqueued_requeue_safe(self):
        self.seed()
        self.assertTrue(self.job_row().safe_to_retry_after_crash)

    def test_a_crash_after_mark_running_is_requeued_and_the_rerun_settles_once(self):
        self.seed()
        self.crash_after_mark_running()
        self.recover()
        job = self.job_row()
        self.assertEqual((job.state, job.error_code), ("queued", "lease_expired"))
        self.assertNotIn("job.uncertain", self.job_events())
        self.assertIn("job.requeued", self.job_events())

        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.job_row().state, "succeeded")
        self.assert_settled_once(self.observe())

    def test_a_crash_after_the_work_committed_only_closes_the_job(self):
        self.seed()
        self.crash_before_closing_the_job()
        settled = self.observe()
        self.assert_settled_once(settled)
        self.recover()
        job = self.job_row()
        self.assertEqual((job.state, job.error_code), ("queued", "lease_expired"))

        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.job_row().state, "succeeded")
        self.assertEqual(self.observe(), settled, "the rerun wrote nothing")

    def test_two_workers_on_one_job_write_once(self):
        self.seed()
        first, second = self.run_two_workers()
        self.assertIsNone(second, "the Worker holding the live lease closes the Job")
        # The first Worker lost its lease to the second; it may finish its
        # own transaction, but it cannot close a Job it no longer holds.
        self.assertIsInstance(first, RuntimeError)
        self.assertIn(LEASE_LOST, str(first))
        self.assertEqual(self.job_row().state, "succeeded")
        self.assert_settled_once(self.observe())


def _operation_events(ctx: TenantContext, operation_id: str) -> list[tuple[str | None, str]]:
    with tenant_session(ctx) as session:
        return [
            (row.from_state, row.to_state)
            for row in session.scalars(
                select(OperationStateEvent)
                .where(OperationStateEvent.operation_id == operation_id)
                .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
            )
        ]


# ---------------------------------------------------------------------------
# library.reference_gate
# ---------------------------------------------------------------------------


class ReferenceGateFixture(LibraryFixtures, CrashRecoveryFixture):
    JOB_TYPE = "library.reference_gate"
    INTERLEAVE_AT = "app.library.reference_gate_worker._read_original"

    def setUp(self):
        self.ctx = self._make_context(workspace_id=f"ws_crash_gate_{uuid.uuid4().hex}")
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        root = Path(tempfile.mkdtemp(prefix="crash-gate-"))
        self.store = LocalFileBlobStore(
            root=root, public_base_url="http://localhost:9000", signing_secret=b"test-secret",
        )
        self.derivative_commits = 0

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        raw = png_bytes(colour=(uuid.uuid4().int % 255, 40, 90))
        key = f"committed/{self.ctx.workspace_id}/reference_asset/2026/11/07/{uuid.uuid4().hex}.png"
        (self.store.root / key).parent.mkdir(parents=True, exist_ok=True)
        (self.store.root / key).write_bytes(raw)
        intent_id, _ = _provision_finalized_intent(
            self.ctx, committed_key=key, committed_version="1:2:3:4", raw=raw, write_file=False,
        )
        accepted = create_reference_from_upload_intent(
            self.ctx, upload_intent_id=intent_id,
            rights={"scope": "owned_product_photo", "valid_to": FUTURE},
            idempotency_key=f"ci-only-crash-{uuid.uuid4().hex}",
        )
        self.operation_id = accepted["operation_id"]
        with tenant_session(self.ctx) as session:
            job = session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == self.JOB_TYPE,
                )
            ).one()
            self.job_id = job.id
            self.object_id = job.payload_json["object_id"]

    def runtime(self, owner: str):
        runtime = WorkerRuntime(owner=owner)
        runtime.blob_store = self.store
        return runtime

    def crash_partway(self, claimed: jobs.ClaimedJob) -> None:
        # The handler moves the Operation to running in its own transaction
        # first; a rerun finds it running and must carry on.
        with tenant_session(claimed.context) as session:
            transition_operation(
                session, self.operation_id, OperationState.RUNNING,
                reason=None, actor=claimed.context.executor,
            )

    def observe(self) -> dict:
        with tenant_session(self.ctx) as session:
            record = session.get(ReferenceObjectRecord, self.object_id)
            released_audits = session.scalar(
                select(func.count()).select_from(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == self.ctx.workspace_id,
                    LibraryAuditRecord.command == "reference_gate",
                )
            )
            return {
                "object": record.state,
                "derivative": record.derivative_object_key,
                "gate_audits": released_audits,
                "operation": session.get(OperationRecord, self.operation_id).state,
                "operation_events": _operation_events(self.ctx, self.operation_id),
            }

    def assert_settled_once(self, observed: dict) -> None:
        self.assertEqual(observed["object"], references.STATE_RELEASED)
        self.assertEqual(observed["gate_audits"], 1, "the object was decided once")
        self.assertEqual(observed["operation"], "succeeded")
        self.assertEqual(
            observed["operation_events"],
            [(None, "queued"), ("queued", "running"), ("running", "succeeded")],
        )


class TestReferenceGateCrashRecovery(ReferenceGateFixture, CrashRecoveryCases):
    def test_a_rerun_that_finds_the_operation_failed_only_fails_the_job(self):
        self.seed()
        with tenant_session(self.ctx) as session:
            for to_state in (OperationState.RUNNING, OperationState.FAILED):
                transition_operation(
                    session, self.operation_id, to_state,
                    reason="ci-only-settled-elsewhere", actor="system:worker",
                )
        self.drain()
        job = self.job_row()
        self.assertEqual((job.state, job.error_code), ("failed", "reference_gate_failed"))
        self.assertEqual(self.observe()["object"], references.STATE_QUARANTINED, "nothing was decided")


# ---------------------------------------------------------------------------
# generation.export_package
# ---------------------------------------------------------------------------


class ExportPackageFixture(LibraryFixtures, CrashRecoveryFixture):
    JOB_TYPE = "generation.export_package"
    INTERLEAVE_AT = "app.generation.export_worker._lock_package"
    INTERLEAVE_AFTER = True

    _seed_committed_version = export_tests.TestExportWorker._seed_committed_version

    def setUp(self):
        self.ctx = self._make_context(workspace_id=f"ws_crash_export_{uuid.uuid4().hex}")
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        self.blob_store = LocalFileBlobStore(
            root=Path(tempfile.mkdtemp(prefix="crash-export-")),
            public_base_url="http://localhost:9000", signing_secret=b"top-secret",
        )
        self.zip_commits = 0
        original = self.blob_store.commit_generated_artifact

        async def counting_commit(*args, **kwargs):
            self.zip_commits += 1
            return await original(*args, **kwargs)

        self.blob_store.commit_generated_artifact = counting_commit

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        with tenant_session(self.ctx) as session:
            version_ids = [
                self._seed_committed_version(session, content=f"crash-{uuid.uuid4().hex}".encode(), seq=i)
                for i in range(2)
            ]
            created = create_export_package(
                session, self.ctx, selector={"kind": "explicit", "version_ids": version_ids},
            )
            self.package_id = created["package_id"]
            self.operation_id = created["operation_id"]
            self.job_id = session.scalars(
                select(JobRecord.id).where(
                    JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == self.JOB_TYPE,
                )
            ).one()

    def runtime(self, owner: str):
        runtime = WorkerRuntime(owner=owner)
        runtime.blob_store = self.blob_store
        return runtime

    def observe(self) -> dict:
        with tenant_session(self.ctx) as session:
            package = session.get(ExportPackageRecord, self.package_id)
            return {
                "package": package.state,
                "zip_hash": package.zip_hash,
                "zip_commits": self.zip_commits,
                "operation": session.get(OperationRecord, self.operation_id).state,
                "operation_events": _operation_events(self.ctx, self.operation_id),
            }

    def assert_settled_once(self, observed: dict) -> None:
        self.assertEqual(observed["package"], "ready")
        self.assertTrue(observed["zip_hash"])
        self.assertEqual(observed["zip_commits"], 1, "the ZIP was built and committed once")
        self.assertEqual(observed["operation"], "succeeded")
        self.assertEqual(
            observed["operation_events"],
            [(None, "queued"), ("queued", "running"), ("running", "succeeded")],
        )


class TestExportPackageCrashRecovery(ExportPackageFixture, CrashRecoveryCases):
    def test_a_rerun_of_a_failed_package_fails_the_job_and_builds_nothing(self):
        self.seed()
        with tenant_session(self.ctx) as session:
            session.execute(
                update(ExportPackageRecord).where(ExportPackageRecord.id == self.package_id)
                .values(state="failed")
            )
        self.drain()
        job = self.job_row()
        self.assertEqual((job.state, job.error_code), ("failed", "generation_export_package_failed"))
        self.assertEqual(self.zip_commits, 0)


# ---------------------------------------------------------------------------
# scale_export
# ---------------------------------------------------------------------------


class ScaleExportFixture(LibraryFixtures, CrashRecoveryFixture):
    JOB_TYPE = "scale_export"
    INTERLEAVE_AT = "app.scale.exports.get_operation"

    def setUp(self):
        workspace_id = f"ws_crash_scale_{uuid.uuid4().hex}"
        self.provision_workspace(workspace_id)
        self.ctx = scale_export_tests._owner_context(workspace_id)
        self.addCleanup(cancel_workspace_jobs, workspace_id)

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        with tenant_session(self.ctx) as session:
            enrollment_id, episode_id = scale_export_tests._seed_enrollment_and_episode(
                session, self.ctx.workspace_id,
            )
            result = create_export(
                session, self.ctx, enrollment_id=enrollment_id, episode_id=episode_id,
                export_kind="evidence_summary", idempotency_key=f"ci-only-crash-{uuid.uuid4().hex}",
            )
            self.export_id = result.export_id
            self.operation_id = result.operation_id
            self.job_id = session.scalars(
                select(JobRecord.id).where(
                    JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == self.JOB_TYPE,
                )
            ).one()

    def runtime(self, owner: str):
        return ScaleWorkerRuntime(owner=owner)

    def run_once(self, runtime) -> bool:
        # The Scale Worker claims without a lane filter; every other
        # Workspace's open Job is deferred by ``drain``/the cases first.
        return runtime.run_once()

    def observe(self) -> dict:
        with tenant_session(self.ctx) as session:
            export = session.get(ScaleExportRecord, self.export_id)
            return {
                "completed": export.completed_at is not None,
                "result": export.result_object_reference,
                "operation": session.get(OperationRecord, self.operation_id).state,
                "operation_events": _operation_events(self.ctx, self.operation_id),
            }

    def assert_settled_once(self, observed: dict) -> None:
        self.assertTrue(observed["completed"])
        self.assertTrue(observed["result"])
        self.assertEqual(observed["operation"], "succeeded")
        self.assertEqual(
            observed["operation_events"],
            [(None, "queued"), ("queued", "running"), ("running", "succeeded")],
        )


class TestScaleExportCrashRecovery(ScaleExportFixture, CrashRecoveryCases):
    def test_a_rerun_that_finds_the_operation_failed_writes_nothing(self):
        self.seed()
        with tenant_session(self.ctx) as session:
            for to_state in (OperationState.RUNNING, OperationState.FAILED):
                transition_operation(
                    session, self.operation_id, to_state,
                    reason="ci-only-settled-elsewhere", actor="system:worker",
                )
        self.drain()
        job = self.job_row()
        self.assertEqual((job.state, job.error_code), ("failed", "export_operation_settled"))
        self.assertFalse(self.observe()["completed"])


class DeployedWorkerScaleExportFixture(ScaleExportFixture):
    """``scale_export`` run by ``WorkerRuntime``, the only Worker deployed (#55)."""

    def runtime(self, owner: str):
        return WorkerRuntime(owner=owner)

    run_once = CrashRecoveryFixture.run_once


class TestScaleExportCrashRecoveryOnTheDeployedWorker(
    DeployedWorkerScaleExportFixture, CrashRecoveryCases,
):
    test_a_rerun_that_finds_the_operation_failed_writes_nothing = (
        TestScaleExportCrashRecovery.test_a_rerun_that_finds_the_operation_failed_writes_nothing
    )


# ---------------------------------------------------------------------------
# localization.run_deterministic_qc / finalize_item / fold_batch
# ---------------------------------------------------------------------------


#: ``tests.test_localization_worker`` numbers its market ids from 1; this
#: module's batches draw from a disjoint, per-process range so they can share
#: a database (PostgreSQL included) with that suite and with an earlier run.
_MARKET_IDS = itertools.count(random.randrange(100_000, 900_000))


class LocalizationFixture(unittest.TestCase, CrashRecoveryFixture):
    """Borrows ``tests.test_localization_worker``'s batch builder.

    That builder works in its module's one Workspace (``WS_ID``/``CTX``).
    Each test here gets a Workspace of its own instead -- the builder's
    globals are pointed at it while it runs -- so no two batches share a
    product (PostgreSQL sets a product's profile once) or a Job queue.
    """

    _build_ready_batch = localization_tests.WorkerTestBase._build_ready_batch
    _truth_command_for = localization_tests.WorkerTestBase._truth_command_for

    @classmethod
    def setUpClass(cls) -> None:
        localization_tests.WorkerTestBase.setUpClass()

    def setUp(self) -> None:
        localization_tests.WorkerTestBase.setUp(self)
        workspace_id = f"ws_crash_l10n_{uuid.uuid4().hex[:16]}"
        self.ctx = TenantContext(
            workspace_id=workspace_id, role="owner", initiator_id="user_1", initiator_role="owner",
        )
        now = utcnow()
        with tenant_session(self.ctx) as session:
            localization_tests._ensure_workspace(session, workspace_id)
            session.add(localization_tests.EntitlementProjectionRecord(
                workspace_id=workspace_id, entitlement_version=1, lease_sequence=1,
                projection_version=1, schema_version=2, semantic_hash="h" * 8,
                valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                resolved_entitlements_json={},
            ))
            session.add(localization_tests.SpendGuardrailRecord(
                workspace_id=workspace_id, revision=1, budget_currency="USD",
                per_operation_limit=Decimal("100.00"), rolling_24h_limit=Decimal("500.00"),
                period_limit=Decimal("5000.00"), outstanding_exposure_limit=Decimal("1000.00"),
                created_at=now, updated_at=now, updated_by="user_1",
            ))
        self.addCleanup(cancel_workspace_jobs, workspace_id)

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def _job_for(self, job_type: str, **payload) -> str:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id,
                    JobRecord.type == job_type,
                    JobRecord.state == "queued",
                )
            ).all()
            matching = [
                row.id for row in rows
                if all((row.payload_json or {}).get(k) == v for k, v in payload.items())
            ]
        assert len(matching) == 1, (job_type, payload, matching)
        return matching[0]

    def _build_edited_item(self) -> None:
        with patch.object(localization_tests, "WS_ID", self.ctx.workspace_id), \
             patch.object(localization_tests, "CTX", self.ctx), \
             patch.object(localization_tests, "_MARKET_ID_COUNTER", _MARKET_IDS):
            built = self._build_ready_batch(f"crash{uuid.uuid4().hex[:10]}")
        self.batch_id, self.item_id = built["batch_id"], built["item_id"]
        self.source_version_id = built["asset_version_id"]
        try:
            asyncio.run(localization_tests.worker.handle_edit_attempt(
                self.ctx, batch_id=self.batch_id, item_id=self.item_id, attempt_number=1,
                session_factory=tenant_session, coordinator=self.coordinator,
            ))
        except HandlerOutcomeSignal as signal:  # pragma: no cover - fixture guard
            raise AssertionError(f"the edit did not complete: {signal}")
        with tenant_session(self.ctx) as session:
            self.attempt_id = localization_tests.worker._load_attempt_by_number(
                session, self.ctx, self.item_id, self.batch_id, 1,
            ).id

    def runtime(self, owner: str):
        return WorkerRuntime(owner=owner)

    def _count(self, statement) -> int:
        with tenant_session(self.ctx) as session:
            return session.scalar(statement)

    def _jobs_of(self, job_type: str, **payload) -> int:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == job_type,
                )
            ).all()
        return len([
            row for row in rows
            if all((row.payload_json or {}).get(k) == v for k, v in payload.items())
        ])

    def _item_and_batch(self) -> tuple[LocalizationItemRecord, LocalizationBatchRecord]:
        with tenant_session(self.ctx) as session:
            item = session.get(LocalizationItemRecord, self.item_id)
            batch = session.get(LocalizationBatchRecord, self.batch_id)
            session.expunge_all()
            return item, batch

    def _children(self) -> int:
        return self._count(
            select(func.count()).select_from(AssetVersionRecord).where(
                AssetVersionRecord.workspace_id == self.ctx.workspace_id,
                AssetVersionRecord.origin_kind == "localized",
                AssetVersionRecord.parent_version_id == self.source_version_id,
            )
        )


class DeterministicQcFixture(LocalizationFixture):
    JOB_TYPE = "localization.run_deterministic_qc"
    INTERLEAVE_AT = "app.localization.qc.run_deterministic_qc"

    def seed(self) -> None:
        self._build_edited_item()
        self.job_id = self._job_for(self.JOB_TYPE, item_id=self.item_id)

    def observe(self) -> dict:
        item, _batch = self._item_and_batch()
        return {
            "item": (item.state, item.revision),
            "qc_records": self._count(
                select(func.count()).select_from(LocalizationQcRecordRecord).where(
                    LocalizationQcRecordRecord.workspace_id == self.ctx.workspace_id,
                    LocalizationQcRecordRecord.attempt_id == self.attempt_id,
                )
            ),
            "finalize_jobs": self._jobs_of("localization.finalize_item", item_id=self.item_id),
        }

    def assert_settled_once(self, observed: dict) -> None:
        # A passing deterministic QC with no external steps leaves the Item
        # running and enqueues its finalize exactly once.
        self.assertEqual(observed["item"][0], "running")
        self.assertGreater(observed["qc_records"], 0)
        self.assertEqual(observed["finalize_jobs"], 1)


class TestDeterministicQcCrashRecovery(DeterministicQcFixture, CrashRecoveryCases):
    pass


class FinalizeItemFixture(LocalizationFixture):
    JOB_TYPE = "localization.finalize_item"
    INTERLEAVE_AT = "app.localization.publication.publish_localized_output"

    def seed(self) -> None:
        self._build_edited_item()
        with tenant_session(self.ctx) as session:
            localization_tests.worker.handle_run_deterministic_qc(
                session, self.ctx, batch_id=self.batch_id, item_id=self.item_id, attempt_id=self.attempt_id,
            )
        self.job_id = self._job_for(self.JOB_TYPE, item_id=self.item_id)

    def observe(self) -> dict:
        item, _batch = self._item_and_batch()
        return {
            "item": (item.state, item.revision),
            "children": self._children(),
            "fold_jobs": self._jobs_of("localization.fold_batch", batch_id=self.batch_id),
        }

    def assert_settled_once(self, observed: dict) -> None:
        self.assertEqual(observed["item"][0], "succeeded")
        self.assertEqual(observed["children"], 1, "one localized child AssetVersion")
        self.assertEqual(observed["fold_jobs"], 1)


class TestFinalizeItemCrashRecovery(FinalizeItemFixture, CrashRecoveryCases):
    pass


class FoldBatchFixture(LocalizationFixture):
    JOB_TYPE = "localization.fold_batch"
    INTERLEAVE_AT = "app.localization.batch.derive_batch_outcome"

    def seed(self) -> None:
        self._build_edited_item()
        with tenant_session(self.ctx) as session:
            localization_tests.worker.handle_run_deterministic_qc(
                session, self.ctx, batch_id=self.batch_id, item_id=self.item_id, attempt_id=self.attempt_id,
            )
        with tenant_session(self.ctx) as session:
            localization_tests.worker.handle_finalize_item(
                session, self.ctx, batch_id=self.batch_id, item_id=self.item_id, attempt_id=self.attempt_id,
            )
        self.job_id = self._job_for(self.JOB_TYPE, batch_id=self.batch_id)
        self.revision_before_fold = self._item_and_batch()[1].revision

    def observe(self) -> dict:
        _item, batch = self._item_and_batch()
        return {"batch": (batch.phase, batch.outcome, batch.revision)}

    def assert_settled_once(self, observed: dict) -> None:
        self.assertEqual(
            observed["batch"], ("closed", "succeeded", self.revision_before_fold + 1),
            "the Batch was folded once",
        )


class TestFoldBatchCrashRecovery(FoldBatchFixture, CrashRecoveryCases):
    pass


# ---------------------------------------------------------------------------
# The paid control: these stay uncertain.
# ---------------------------------------------------------------------------


class PaidJobFixture(LibraryFixtures):
    """A paid or external-write Job that dies after ``mark_running``.

    Payloads are minimal: recovery decides from the Job row alone, and none
    of these Jobs is ever run here.
    """

    PAID_TYPES = ("generate_asset", "generation.run_item", "launch_provisioning")

    def setUp(self):
        self.ctx = self._make_context(workspace_id=f"ws_crash_paid_{uuid.uuid4().hex}")
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def crash(self, job_type: str, expire_lease=None) -> str:
        with tenant_session(self.ctx) as session:
            job_id = jobs.enqueue_in_session(
                session, context=self.ctx, job_type=job_type, payload={},
                idempotency_key=f"ci-only-paid-crash:{job_type}:{uuid.uuid4().hex}",
            ).id
        quarantine_foreign_jobs(self.ctx.workspace_id)
        claimed = jobs.claim_next("paid-crashed-worker", job_types=(job_type,))
        assert claimed is not None and claimed.id == job_id, claimed
        jobs.mark_running(claimed.context, claimed.id, "paid-crashed-worker")
        if expire_lease is None:
            with platform_session() as session:
                session.execute(
                    update(JobRecord).where(JobRecord.id == job_id)
                    .values(lease_expires_at=utcnow() - timedelta(seconds=1))
                )
        else:
            expire_lease(job_id)
        return job_id

    def assert_uncertain(self, job_id: str) -> None:
        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            self.assertFalse(job.safe_to_retry_after_crash)
            self.assertEqual((job.state, job.error_code), ("uncertain", "paid_call_outcome_unknown"))


class TestPaidJobsStayUncertain(PaidJobFixture):
    def test_a_crashed_paid_job_is_still_left_uncertain(self):
        for job_type in self.PAID_TYPES:
            with self.subTest(job_type=job_type):
                job_id = self.crash(job_type)
                jobs.recover_expired_jobs()
                self.assert_uncertain(job_id)


if __name__ == "__main__":
    unittest.main()
