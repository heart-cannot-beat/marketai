"""Legacy Operations whose Job ended without its handler are settled (#22).

A legacy (v0) Operation is moved only by its own domain handler. When the
dispatcher rejects its Job for a stale authorization epoch, recovery leaves
the Job ``uncertain`` after a lease expiry, or the Worker had no handler for
the type, nobody settled the Operation: it stayed ``queued`` or ``running``
forever. ``app.legacy_operations`` is the reconciler for those endings.

Every registered legacy command runs the same cases against its own
fixture, built through the command's real entry point where one exists:

- the authorization epoch changes after the Job was enqueued; the
  dispatcher rejects the Job, the Operation is still ``queued`` (the bug),
  and one reconciler pass settles the Operation ``failed`` with
  ``reason=authorization_epoch_stale`` and the domain resource in its
  terminal state;
- running the reconciler twice appends one Operation event, not two.

Targeted cases cover cross-Workspace isolation, unclear correlation, the
lease-expiry ``uncertain`` path, ``unknown_job_type`` (``scale_export``
before #55), a Launch that may already have written to Meta, and a retried
marketing export. This module drives the SQLite path;
``tests/test_postgres_architecture.py`` drives the same folds under the real
Worker role.
"""
from __future__ import annotations

import ast
import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests import test_job_crash_recovery as crash
from tests import test_library_connection_check_worker as connection_check_tests
from tests import test_library_manual_fact_import_worker as fact_import_tests
from tests import test_marketing_ai_qc as ai_qc_tests
from tests import test_marketing_planning as planning_tests
from tests import test_marketing_workflow as workflow_tests
from tests import test_upload_intents as upload_tests
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.support.storage_limit import grant_storage_limit

from app import legacy_operations
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (
    AuditEventRecord,
    JobRecord,
    ReferenceObjectRecord,
    UploadIntentRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.entitlements.cost_models import SpendReservationRecord
from app.generation.models import ExportPackageRecord
from app.launch.models import (
    LaunchProvisioningRecord,
    LaunchReconciliationRecord,
    MetaLaunchConfirmationRecord,
    MetaLaunchPreviewRecord,
)
from app.launch.recovery import create_recovery_operation
from app.library.contracts import ROLE_OWNER
from app.main import app
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.models import PlanningCallRecord
from app.marketing.recovery import ResolveCall, admit as admit_resolution
from app.marketing.workflow_models import MarketingHandoffRecord, MarketingImageEditRecord
from app.meta.models import MetaConnectionRecord, MetaDataSourceBindingRecord
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.idempotency import create_or_replay_operation
from app.platform.operations import (
    OperationRecord,
    OperationState,
    create_operation,
    transition_operation,
)
from app.platform.tenancy import TenantContext, TenantScopeError
from app.scale.models import ScaleExportRecord

EPOCH_STALE = "authorization_epoch_stale"
FAILED_EVENTS = [(None, "queued"), ("queued", "failed")]


def _current_epoch(workspace_id: str) -> int:
    with platform_session() as session:
        return session.get(WorkspaceRecord, workspace_id).authorization_epoch


def _bump_epoch(workspace_id: str) -> int:
    """What the control plane does when a member's access changes."""
    with platform_session() as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        workspace.authorization_epoch += 1
        return workspace.authorization_epoch


def _with_epoch(context: TenantContext, epoch: int) -> TenantContext:
    try:
        return replace(context, authorization_epoch=epoch)
    except TenantScopeError:
        # A test context that left ``identity_kind`` to be inferred.
        return replace(context, authorization_epoch=epoch, identity_kind=None)


def _job(job_id: str) -> JobRecord:
    with platform_session() as session:
        job = session.get(JobRecord, job_id)
        session.expunge(job)
        return job


def _operation(workspace_id: str, operation_id: str) -> OperationRecord:
    with platform_session() as session:
        operation = session.get(OperationRecord, operation_id)
        assert operation.workspace_id == workspace_id
        session.expunge(operation)
        return operation


def _events(operation_id: str) -> list[tuple[str | None, str, str | None]]:
    from app.platform.operations import OperationStateEvent

    with platform_session() as session:
        return [
            (row.from_state, row.to_state, row.reason)
            for row in session.scalars(
                select(OperationStateEvent)
                .where(OperationStateEvent.operation_id == operation_id)
                .order_by(OperationStateEvent.created_at, OperationStateEvent.id)
            )
        ]


def _transitions(operation_id: str) -> list[tuple[str | None, str]]:
    return [(frm, to) for frm, to, _ in _events(operation_id)]


def _operation_usage(operation_id: str) -> list[tuple[str, str]]:
    """The Operation's Usage rows (#8 S2): ``(metric_key, execution_mode)``."""
    from app.entitlements.usage_models import UsageEventRecord

    with platform_session() as session:
        return sorted(
            (row.metric_key, row.execution_mode)
            for row in session.scalars(select(UsageEventRecord).where(
                UsageEventRecord.source_resource_type == "operation",
                UsageEventRecord.source_resource_id == operation_id,
            ))
        )


def _reconciled_audits(operation_id: str) -> list[dict]:
    with platform_session() as session:
        return [
            dict(row.details_json or {})
            for row in session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.event_type == "operation.legacy_reconciled",
                    AuditEventRecord.resource_id == operation_id,
                )
            )
        ]


class LegacyFoldCases:
    """The cases every legacy command runs; a fixture provides the seams.

    ``seed`` creates one Operation and its queued Job through the command's
    entry point and sets ``self.job_id``/``self.operation_id``;
    ``workspace_id`` names the Workspace; ``resource`` reads the domain
    resource's state with the Workspace's current epoch.
    """

    #: The resource as the command left it, and as the fold must leave it.
    RESOURCE_BEFORE: object = None
    RESOURCE_AFTER: object = None
    #: The Usage bucket the settled Operation is counted in (#8 S2).
    USAGE_MODE = "not_applicable"

    def workspace_id(self) -> str:
        raise NotImplementedError

    def seed(self) -> None:
        raise NotImplementedError

    def resource(self):
        return None

    def adopt_epoch(self, epoch: int) -> None:
        """Point the fixture's own contexts at the Workspace's new epoch."""
        for name in ("ctx", "context"):
            value = getattr(self, name, None)
            if isinstance(value, TenantContext):
                setattr(self, name, _with_epoch(value, epoch))

    def change_epoch_and_dispatch(self) -> None:
        self.adopt_epoch(_bump_epoch(self.workspace_id()))
        jobs.recover_expired_jobs()

    def fold(self) -> None:
        legacy_operations.reconcile_legacy_operations()

    def test_an_epoch_change_settles_the_operation_and_its_resource(self):
        self.seed()
        self.assertEqual(self.resource(), self.RESOURCE_BEFORE)
        self.change_epoch_and_dispatch()

        job = _job(self.job_id)
        self.assertEqual((job.state, job.error_code), ("failed", EPOCH_STALE))
        self.assertEqual(
            _operation(self.workspace_id(), self.operation_id).state, "queued",
            "the dispatcher alone leaves a legacy Operation queued",
        )

        self.fold()

        self.assertEqual(_operation(self.workspace_id(), self.operation_id).state, "failed")
        self.assertEqual(_transitions(self.operation_id), FAILED_EVENTS)
        self.assertEqual(_events(self.operation_id)[-1][2], EPOCH_STALE)
        self.assertEqual(self.resource(), self.RESOURCE_AFTER)
        self.assertEqual(
            _reconciled_audits(self.operation_id),
            [{"job_id": self.job_id, "job_type": job.type,
              "job_error_code": EPOCH_STALE, "to_state": "failed"}],
        )

    def test_reconciling_twice_appends_one_event(self):
        self.seed()
        self.change_epoch_and_dispatch()
        self.fold()
        settled = _events(self.operation_id)
        self.fold()
        self.assertEqual(_events(self.operation_id), settled)
        self.assertEqual(sum(1 for _, to, _ in settled if to == "failed"), 1)
        self.assertEqual(len(_reconciled_audits(self.operation_id)), 1)
        # Usage (#8 S2): the fold settles through transition_operation, which
        # counts the Operation once however often the fold runs.
        self.assertEqual(_operation_usage(self.operation_id),
                         [("operations", self.USAGE_MODE), ("processing_seconds", self.USAGE_MODE)])


# ---------------------------------------------------------------------------
# Fixtures that 22a's crash-recovery tests already build
# ---------------------------------------------------------------------------


class TestScaleExportFold(crash.DeployedWorkerScaleExportFixture, LegacyFoldCases):
    # A ScaleExport has no state column: it never completed, and its failed
    # Operation is its terminal state.
    RESOURCE_BEFORE = RESOURCE_AFTER = {"completed": False}

    def resource(self):
        with tenant_session(self.ctx) as session:
            return {"completed": session.get(ScaleExportRecord, self.export_id).completed_at is not None}

    def test_a_job_no_worker_could_run_is_settled_failed(self):
        """``scale_export`` before #55: the deployed Worker had no handler."""
        self.seed()
        quarantine_foreign_jobs(self.workspace_id())
        claimed = jobs.claim_next("pre-55-worker", job_types=(self.JOB_TYPE,))
        self.assertEqual(claimed.id, self.job_id)
        jobs.fail(
            claimed.context, claimed.id, "pre-55-worker",
            code="unknown_job_type", detail=f"未知任务类型: {claimed.type}", retryable=False,
        )
        self.assertEqual(_operation(self.workspace_id(), self.operation_id).state, "queued")

        self.fold()

        self.assertEqual(_transitions(self.operation_id), FAILED_EVENTS)
        self.assertEqual(_events(self.operation_id)[-1][2], "unknown_job_type")
        self.assertEqual(self.resource(), {"completed": False})


class TestReferenceGateFold(crash.ReferenceGateFixture, LegacyFoldCases):
    # Not ``rejected``: that is a content verdict that purges the original
    # and bans the bytes. The gate never ran, so the object stays unreleased
    # with the reason recorded.
    RESOURCE_BEFORE = ("quarantined", None)
    RESOURCE_AFTER = ("quarantined", EPOCH_STALE)

    def resource(self):
        with tenant_session(self.ctx) as session:
            record = session.get(ReferenceObjectRecord, self.object_id)
            failure = (record.checks_json or {}).get("failure") or {}
            return record.state, failure.get("code")


class TestExportPackageFold(crash.ExportPackageFixture, LegacyFoldCases):
    RESOURCE_BEFORE = ("pending", None)
    RESOURCE_AFTER = ("failed", EPOCH_STALE)
    USAGE_MODE = "mock"  # the Mock-mode resource this command acted on

    def resource(self):
        import json

        with tenant_session(self.ctx) as session:
            package = session.get(ExportPackageRecord, self.package_id)
            evidence = json.loads(package.failure_evidence) if package.failure_evidence else {}
            return package.state, evidence.get("code")


# ---------------------------------------------------------------------------
# Library commands
# ---------------------------------------------------------------------------


class TestConnectionCheckFold(connection_check_tests.ConnectionCheckFixtures, LegacyFoldCases):
    """No resource state: a check that never ran recorded no evidence."""

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        response = self.check(self.data_source())
        self.assertEqual(response.status_code, 202, response.text)
        self.operation_id = response.json()["operation_id"]
        self.job_id = _only_job(self.ctx.workspace_id, "library.connection_check")


class TestManualFactImportFold(fact_import_tests.ManualFactImportFixtures, LegacyFoldCases):
    """No resource state: the draft is only written when the import runs."""

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        data_source_id = self.data_source()
        self.upload_id = self.upload(data_source_id)
        self.data_source_id = data_source_id
        response = self.import_(data_source_id, self.upload_id)
        self.assertEqual(response.status_code, 202, response.text)
        self.operation_id = response.json()["operation_id"]
        self.job_id = _only_job(self.ctx.workspace_id, "library.manual_fact_import")

    def test_the_upload_can_be_imported_again(self):
        self.seed()
        self.change_epoch_and_dispatch()
        self.fold()
        self.act_as(self.ctx)
        again = self.import_(self.data_source_id, self.upload_id)
        self.assertEqual(again.status_code, 202, again.text)


def _only_job(workspace_id: str, job_type: str) -> str:
    with platform_session() as session:
        return session.scalars(
            select(JobRecord.id).where(
                JobRecord.workspace_id == workspace_id, JobRecord.type == job_type,
            )
        ).one()


# ---------------------------------------------------------------------------
# finalize_upload
# ---------------------------------------------------------------------------


class TestFinalizeUploadFold(LibraryFixtures, LegacyFoldCases):
    """The intent ends ``rejected`` the way the handler's own epoch check
    rejects it, and its staging object is scheduled for cleanup."""

    RESOURCE_BEFORE = ("pending", None)
    RESOURCE_AFTER = ("rejected", EPOCH_STALE)

    def setUp(self):
        self.ctx = self._make_context(role=ROLE_OWNER)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=True)
        self.addCleanup(self.client.close)

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        with platform_session() as session:  # upload admission reads the limit (#158)
            grant_storage_limit(session, self.ctx.workspace_id)
        headers = {"Idempotency-Key": f"ci-only-{uuid.uuid4().hex}"}
        created = self.client.post("/api/v1/upload-intents", json=upload_tests._upload_body(), headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.intent_id = created.json()["intent_id"]
        finalized = self.client.post(
            f"/api/v1/upload-intents/{self.intent_id}/finalize",
            headers={"Idempotency-Key": f"ci-only-{uuid.uuid4().hex}"},
        )
        self.assertEqual(finalized.status_code, 202, finalized.text)
        self.operation_id = finalized.json()["operation_id"]
        self.job_id = _only_job(self.ctx.workspace_id, "verify_upload_intent")

    def resource(self):
        with tenant_session(self.ctx) as session:
            intent = session.get(UploadIntentRecord, self.intent_id)
            return intent.state, intent.rejection_code

    def test_the_staging_object_is_cleaned_up_under_the_current_epoch(self):
        self.seed()
        self.change_epoch_and_dispatch()
        self.fold()
        with platform_session() as session:
            cleanup = session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id,
                    JobRecord.type == "cleanup_staging_upload",
                )
            ).one()
            self.assertEqual(cleanup.state, "queued")
            self.assertEqual(cleanup.authorization_epoch, _current_epoch(self.ctx.workspace_id))


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


META_MODE = "customer_sandbox"


def _launch_env(ctx: TenantContext) -> tuple[str, str]:
    """A confirmed preview on an active connection; returns (connection, confirmation)."""
    with tenant_session(ctx) as session:
        connection = MetaConnectionRecord(
            id=new_id("mcon"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
            ad_account_id="act_ci_only",
        )
        session.add(connection)
        session.flush()
        binding = MetaDataSourceBindingRecord(
            id=new_id("mbind"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
            meta_connection_id=connection.id, ad_account_id="act_ci_only",
        )
        session.add(binding)
        session.flush()
        preview = MetaLaunchPreviewRecord(
            id=new_id("lprev"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
            meta_connection_id=connection.id, meta_binding_id=binding.id,
            experiment_id="exp_ci_only", experiment_version_id="expv_ci_only",
            candidate_count=1, campaign_config={}, daily_budget_minor=500, currency="USD",
            preview_hash="ph_ci_only", frozen_snapshot={}, state="confirmed", confirmed_at=utcnow(),
        )
        session.add(preview)
        session.flush()
        confirmation = MetaLaunchConfirmationRecord(
            id=new_id("lconf"), workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
            preview_id=preview.id, preview_hash=preview.preview_hash, context_hash="c" * 64,
            token_hash=new_id("th"), actor_id=ctx.initiator_id or "owner_a",
            expires_at=utcnow() + timedelta(minutes=5), consumed_at=utcnow(),
        )
        session.add(confirmation)
        session.flush()
        return connection.id, preview.id, confirmation.id


class LaunchFixture(LibraryFixtures):
    META_MODE = META_MODE

    def setUp(self):
        workspace_id = f"ws_fold_launch_{uuid.uuid4().hex}"
        self.provision_workspace(workspace_id)
        self.ctx = TenantContext(
            workspace_id=workspace_id, role=ROLE_OWNER, initiator_id="owner_a",
            request_id="test-req", trace_id="trace-test", meta_mode=self.META_MODE,
        )
        self.addCleanup(cancel_workspace_jobs, workspace_id)

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def admit_provisioning(self) -> tuple[str, str, str]:
        """``POST /meta/launch`` after its preview checks (``app.launch.api``)."""
        connection_id, preview_id, confirmation_id = _launch_env(self.ctx)
        with tenant_session(self.ctx) as session:
            operation = create_or_replay_operation(
                session, idempotency_key=f"ci-only-{uuid.uuid4().hex}",
                command_type="launch_provisioning", fingerprint_hash=uuid.uuid4().hex,
                actor=self.ctx.executor,
            )
            provisioning = LaunchProvisioningRecord(
                id=new_id("lprov"), workspace_id=self.ctx.workspace_id, meta_mode=self.META_MODE,
                meta_connection_id=connection_id, preview_id=preview_id,
                confirmation_id=confirmation_id, operation_id=operation.id,
                campaign_idempotency_key=f"campaign:{preview_id}", provisioning_state="pending",
            )
            session.add(provisioning)
            session.flush()
            job = jobs.enqueue_in_session(
                session, context=self.ctx, job_type="launch_provisioning",
                payload={"operation_id": operation.id, "provisioning_id": provisioning.id,
                         "meta_mode": self.META_MODE},
                idempotency_key=f"launch-provisioning:{operation.id}",
                resource_type="launch_provisioning", resource_id=provisioning.id,
                safe_to_retry_after_crash=False, max_attempts=1,
            )
            return operation.id, provisioning.id, job.id


class TestLaunchProvisioningFold(LaunchFixture, LegacyFoldCases):
    RESOURCE_BEFORE = "pending"
    RESOURCE_AFTER = "failed"

    def seed(self) -> None:
        self.operation_id, self.provisioning_id, self.job_id = self.admit_provisioning()

    def resource(self):
        with tenant_session(self.ctx) as session:
            return session.get(LaunchProvisioningRecord, self.provisioning_id).provisioning_state

    def test_a_launch_that_may_have_written_to_meta_is_uncertain_not_failed(self):
        """Steps past ``pending`` may have reached Meta: Launch recovery, not
        this reconciler, decides what happened."""
        self.seed()
        with tenant_session(self.ctx) as session:
            transition_operation(
                session, self.operation_id, OperationState.RUNNING,
                reason="launch provisioning worker started", actor="system:worker",
            )
            session.execute(
                update(LaunchProvisioningRecord)
                .where(LaunchProvisioningRecord.id == self.provisioning_id)
                .values(provisioning_state="provisioning")
            )
        self.change_epoch_and_dispatch()
        self.fold()
        self.assertEqual(
            _transitions(self.operation_id),
            [(None, "queued"), ("queued", "running"), ("running", "uncertain")],
        )
        self.assertEqual(self.resource(), "provisioning", "the saga's evidence is not rewritten")
        self.assertEqual(_operation_usage(self.operation_id), [], "uncertain is not terminal: not counted yet")


class TestLaunchRecoveryFold(LaunchFixture, LegacyFoldCases):
    """The recovery Operation fails; its reconciliation stays ``open``,
    because its only terminal state records the original Launch's outcome,
    which nobody has read back."""

    RESOURCE_BEFORE = RESOURCE_AFTER = "open"

    def seed(self) -> None:
        original_id, _, original_job_id = self.admit_provisioning()
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == original_job_id)
                .values(state="uncertain", error_code="launch_outcome_uncertain")
            )
        with tenant_session(self.ctx) as session:
            for to_state in (OperationState.RUNNING, OperationState.UNCERTAIN):
                transition_operation(
                    session, original_id, to_state, reason="ci-only", actor="system:worker",
                )
            recovery_operation = create_or_replay_operation(
                session, idempotency_key=f"ci-only-{uuid.uuid4().hex}",
                command_type="launch_recovery", fingerprint_hash=uuid.uuid4().hex,
                actor=self.ctx.executor,
            )
            reconciliation = create_recovery_operation(
                session, context=self.ctx, original_operation_id=original_id,
                recovery_operation=recovery_operation,
            )
            job = jobs.enqueue_in_session(
                session, context=self.ctx, job_type="launch_recovery",
                payload={"reconciliation_id": reconciliation.id, "meta_mode": self.META_MODE},
                idempotency_key=f"launch-recovery:{recovery_operation.id}",
                resource_type="launch_reconciliation", resource_id=reconciliation.id,
                safe_to_retry_after_crash=True, max_attempts=3,
            )
            self.original_id = original_id
            self.operation_id = recovery_operation.id
            self.reconciliation_id = reconciliation.id
            self.job_id = job.id

    def resource(self):
        with tenant_session(self.ctx) as session:
            return session.get(LaunchReconciliationRecord, self.reconciliation_id).state


# ---------------------------------------------------------------------------
# The same Meta commands under controlled_mock: Mock Usage (#146)
# ---------------------------------------------------------------------------
# A Meta Operation records its mode only in Meta-mode tables; the fold passes
# the Meta mode its Job's payload names, and controlled_mock is a Mock
# execution, kept out of Usage's ``total``.


class TestControlledMockScaleExportFold(crash.ControlledMockScaleExportFixture, TestScaleExportFold):
    USAGE_MODE = "mock"


class TestControlledMockLaunchProvisioningFold(TestLaunchProvisioningFold):
    META_MODE = "controlled_mock"
    USAGE_MODE = "mock"


class TestControlledMockLaunchRecoveryFold(TestLaunchRecoveryFold):
    META_MODE = "controlled_mock"
    USAGE_MODE = "mock"


# ---------------------------------------------------------------------------
# Marketing
# ---------------------------------------------------------------------------


class MarketingFold(LegacyFoldCases):
    def workspace_id(self) -> str:
        return self.context.workspace_id

    def tearDown(self):
        cancel_workspace_jobs(self.context.workspace_id)
        super().tearDown()


class TestPlanningCallFold(planning_tests.MarketingPlanningFixtures, MarketingFold):
    RESOURCE_BEFORE = ("queued", None, None)
    RESOURCE_AFTER = ("failed", EPOCH_STALE, None)
    USAGE_MODE = "mock"  # the Mock-mode resource this command acted on

    def seed(self, byok: bool = False) -> None:
        if byok:
            self.paid_setup()
        project = self.create()
        if byok:
            with patch.dict("os.environ", {
                "MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test",
                "MARKETAI_PLANNING_MAX_COST_USD": "0.10",
            }):
                cap = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
                call = self.plan_call(project, mode="byok", quote_hash=cap["quote_hash"])
        else:
            call = self.plan_call(project)
        self.project = project
        self.call_id = call["id"]
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, self.call_id)
            self.operation_id, self.job_id = row.operation_id, row.job_id

    def resource(self):
        with tenant_session(self.context) as session:
            call = session.get(PlanningCallRecord, self.call_id)
            reservation = session.scalar(
                select(SpendReservationRecord.state).where(SpendReservationRecord.job_id == self.job_id)
            )
            return call.state, call.error_code, reservation

    def test_a_paid_call_releases_its_reservation_and_the_project_can_plan_again(self):
        self.seed(byok=True)
        self.assertEqual(self.resource(), ("queued", None, "reserved"))
        self.change_epoch_and_dispatch()
        self.fold()
        self.assertEqual(self.resource(), ("failed", EPOCH_STALE, "released"))
        # Usage (#8 S2): the reservation records BYOK, so the Operation is real use.
        self.assertEqual(_operation_usage(self.operation_id),
                         [("operations", "byok"), ("processing_seconds", "byok")])
        # ``advance`` refuses while the latest call is queued/running/uncertain.
        again = self.plan_call(self.read(self.project))
        self.assertTrue(again["id"])

    def test_a_call_already_sent_is_uncertain_not_failed(self):
        self.seed()
        with tenant_session(self.context) as session:
            transition_operation(
                session, self.operation_id, OperationState.RUNNING,
                reason="planning_started", actor="system:worker",
            )
            session.get(PlanningCallRecord, self.call_id).state = "running"
        self.change_epoch_and_dispatch()
        self.fold()
        self.assertEqual(
            _transitions(self.operation_id),
            [(None, "queued"), ("queued", "running"), ("running", "uncertain")],
        )
        self.assertEqual(self.resource()[:2], ("uncertain", "planner_outcome_unknown"))
        self.assertEqual(_operation_usage(self.operation_id), [], "uncertain is not terminal: not counted yet")


class TestResolveCallFold(planning_tests.MarketingPlanningFixtures, MarketingFold):
    """The Owner's reconciliation Operation fails; the uncertain call it was
    resolving is left as it is, like an exhausted resolution attempt."""

    RESOURCE_BEFORE = RESOURCE_AFTER = "uncertain"

    def seed(self) -> None:
        call = self.plan_call(self.create())
        self.call_id = call["id"]
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, self.call_id)
            row.state, row.error_code = "uncertain", "planner_outcome_unknown"
            transition_operation(session, row.operation_id, OperationState.RUNNING, "ci-only", "system:worker")
            transition_operation(session, row.operation_id, OperationState.UNCERTAIN, "ci-only", "system:worker")
            session.get(JobRecord, row.job_id).state = "succeeded"
            maximum_cost = Decimal(row.maximum_cost)
        owner = self.owner_ctx(self.context)
        with tenant_session(owner) as session:
            operation = admit_resolution(session, owner, self.call_id, ResolveCall(
                kind="planning", maximum_cost=maximum_cost, acknowledge_estimated_cost=True,
                note="ci-only: provider console shows no charge",
            ))
            self.operation_id = operation.id
        self.job_id = _only_job(self.context.workspace_id, "marketing.resolve_call")

    def resource(self):
        with tenant_session(self.context) as session:
            return session.get(PlanningCallRecord, self.call_id).state


class TestAIQCCallFold(ai_qc_tests.MarketingAIQCFixtures, MarketingFold):
    RESOURCE_BEFORE = ("queued", None)
    RESOURCE_AFTER = ("failed", EPOCH_STALE)
    USAGE_MODE = "mock"  # the Mock-mode resource this command acted on

    def seed(self) -> None:
        _, row = self.setup_round()
        quote = self.quote(row, "single")
        self.command(row, "ai-qc", {"quote_hash": quote["quote_hash"], "kind": "single"}, 202)
        with tenant_session(self.context) as session:
            call = session.scalars(
                select(MarketingAIQCRecord).where(
                    MarketingAIQCRecord.workspace_id == self.context.workspace_id,
                    MarketingAIQCRecord.round_id == row["id"],
                ).order_by(MarketingAIQCRecord.sequence)
            ).first()
            self.call_id, self.operation_id, self.job_id = call.id, call.operation_id, call.job_id

    def resource(self):
        with tenant_session(self.context) as session:
            call = session.get(MarketingAIQCRecord, self.call_id)
            return call.state, call.error_code


class TestImageEditFold(workflow_tests.MarketingWorkflowFixtures, MarketingFold):
    RESOURCE_BEFORE = ("queued", None)
    RESOURCE_AFTER = ("failed", EPOCH_STALE)
    USAGE_MODE = "mock"  # the Mock-mode resource this command acted on

    def seed(self) -> None:
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        source = row["assets"][0]
        row = self.command(row, "image-edits", {
            "asset_version_id": source["asset_version_id"],
            "crop": {"x": 0, "y": 0, "width": .5, "height": 1}, "rotation": 90,
        }, 202)
        self.edit_id = row["image_edits"][0]["id"]
        with tenant_session(self.context) as session:
            edit = session.get(MarketingImageEditRecord, self.edit_id)
            self.operation_id = edit.operation_id
        self.job_id = _only_job(self.context.workspace_id, "marketing.image_edit")

    def resource(self):
        with tenant_session(self.context) as session:
            edit = session.get(MarketingImageEditRecord, self.edit_id)
            return edit.state, edit.error_code


class TestHandoffFold(workflow_tests.MarketingWorkflowFixtures, MarketingFold):
    RESOURCE_BEFORE = ("queued", None)
    RESOURCE_AFTER = ("failed", EPOCH_STALE)
    USAGE_MODE = "mock"  # the Mock-mode resource this command acted on

    def seed(self) -> None:
        _, self.row = self.queued_handoff()
        self.handoff_id = self.row["handoff"]["id"]
        with tenant_session(self.context) as session:
            self.operation_id = session.get(MarketingHandoffRecord, self.handoff_id).operation_id
        self.job_id = _only_job(self.context.workspace_id, "marketing.export")

    def resource(self):
        with tenant_session(self.context) as session:
            handoff = session.get(MarketingHandoffRecord, self.handoff_id)
            return handoff.state, handoff.error_code

    def test_a_retried_export_is_not_settled_by_the_old_job(self):
        self.seed()
        self.change_epoch_and_dispatch()
        self.fold()
        current = self.client.get(f"/api/v1/marketing/rounds/{self.row['id']}").json()["data"]
        self.command(current, "handoff/retry", status=202)
        with tenant_session(self.context) as session:
            retried = session.get(MarketingHandoffRecord, self.handoff_id)
            self.assertEqual(retried.state, "queued")
            new_operation_id = retried.operation_id
        self.assertNotEqual(new_operation_id, self.operation_id)

        self.fold()

        self.assertEqual(_transitions(new_operation_id), [(None, "queued")])
        self.assertEqual(self.resource(), ("queued", None))


# ---------------------------------------------------------------------------
# Generation Runs: the Run projection settles the command Operation
# ---------------------------------------------------------------------------


class TestGenerationRunFold(LibraryFixtures, LegacyFoldCases):
    """Every item whose Job was rejected fails the way a handler fault fails
    it; the Run's own projection then settles its ``generation.submit``
    Operation (``app.generation.run_operations``), which was ``running``."""

    def setUp(self):
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=True)
        self.addCleanup(self.client.close)

    def workspace_id(self) -> str:
        return self.ctx.workspace_id

    def seed(self) -> None:
        draft = self.client.post("/api/v1/generation/drafts", json={
            "product_id": self._product_id(self.ctx), "provider": "mock", "model": "mock-v1",
            "execution_mode": "mock", "combination_strategy": {"kind": "full", "planner_version": "v1"},
        }, headers={"Idempotency-Key": new_id("ik")})
        self.assertEqual(draft.status_code, 201, draft.text)
        preview = self.client.post(
            f"/api/v1/generation/drafts/{draft.json()['id']}/previews",
            headers={"Idempotency-Key": new_id("ik"), "If-Match": '"1"'},
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        run = self.client.post("/api/v1/generation/runs", json={
            "preview_id": preview.json()["id"], "preview_hash": preview.json()["preview_hash"],
        }, headers={"Idempotency-Key": new_id("ik"), "If-Match": '"1"'})
        self.assertEqual(run.status_code, 202, run.text)
        self.operation_id = run.json()["operation_id"]
        self.run_id = run.json()["generation_run_id"]
        with platform_session() as session:
            self.item_jobs = session.scalars(
                select(JobRecord.id).where(
                    JobRecord.workspace_id == self.ctx.workspace_id,
                    JobRecord.type == "generation.run_item",
                )
            ).all()
        self.assertTrue(self.item_jobs)
        self.job_id = self.item_jobs[0]

    def resource(self):
        from app.adapters.persistence.models import GenerationRunRecord
        from app.generation.models import RunItemRecord

        with tenant_session(self.ctx) as session:
            items = sorted(set(session.scalars(
                select(RunItemRecord.state).where(RunItemRecord.generation_run_id == self.run_id)
            )))
            return session.get(GenerationRunRecord, self.run_id).status, items

    def test_an_epoch_change_settles_the_operation_and_its_resource(self):
        self.seed()
        before_status, before_items = self.resource()
        self.assertEqual(before_items, ["pending"])
        self.change_epoch_and_dispatch()
        self.assertEqual(_operation(self.workspace_id(), self.operation_id).state, "running")

        self.fold()

        self.assertEqual(self.resource(), ("failed", ["failed"]))
        self.assertEqual(_operation(self.workspace_id(), self.operation_id).state, "failed")
        self.assertEqual(
            _transitions(self.operation_id), [(None, "queued"), ("queued", "running"), ("running", "failed")],
        )
        audits = _reconciled_audits(self.operation_id)
        self.assertEqual(len(audits), len(self.item_jobs), "one per item whose Job was rejected")
        self.assertEqual({audit["job_error_code"] for audit in audits}, {EPOCH_STALE})

    def test_reconciling_twice_appends_one_event(self):
        self.seed()
        self.change_epoch_and_dispatch()
        self.fold()
        settled = _events(self.operation_id)
        audits = _reconciled_audits(self.operation_id)
        self.fold()
        self.assertEqual(_events(self.operation_id), settled)
        self.assertEqual(sum(1 for _, to, _ in settled if to == "failed"), 1)
        self.assertEqual(_reconciled_audits(self.operation_id), audits)
        # Usage (#8 S2): the Mock Run's Operation is counted once, in the Mock
        # bucket, and each item the fold failed is one failed generation.
        self.assertEqual(_operation_usage(self.operation_id),
                         [("operations", "mock"), ("processing_seconds", "mock")])
        with tenant_session(self.ctx) as session:
            from app.entitlements.usage_models import UsageEventRecord
            failed = session.scalars(select(UsageEventRecord.execution_mode).where(
                UsageEventRecord.workspace_id == self.ctx.workspace_id,
                UsageEventRecord.metric_key == "generation_items_failed",
            )).all()
        self.assertEqual(failed, ["mock"] * len(self.item_jobs))


# ---------------------------------------------------------------------------
# What the reconciler must leave alone
# ---------------------------------------------------------------------------


class TestWhatIsLeftAlone(crash.ReferenceGateFixture):
    def gate(self, ctx: TenantContext) -> tuple[str, str]:
        self.ctx = ctx
        self.seed()
        return self.operation_id, self.job_id

    def test_an_epoch_change_in_another_workspace_folds_nothing_here(self):
        workspace_a = self.ctx
        operation_a, job_a = self.gate(workspace_a)
        workspace_b = self._make_context(workspace_id=f"ws_fold_b_{uuid.uuid4().hex}")
        self.addCleanup(cancel_workspace_jobs, workspace_b.workspace_id)
        operation_b, _ = self.gate(workspace_b)

        _bump_epoch(workspace_b.workspace_id)
        jobs.recover_expired_jobs()
        legacy_operations.reconcile_legacy_operations()

        self.assertEqual(_job(job_a).state, "queued")
        self.assertEqual(_transitions(operation_a), [(None, "queued")])
        self.assertEqual(_reconciled_audits(operation_a), [])
        self.assertEqual(_operation(workspace_b.workspace_id, operation_b).state, "failed")

    def test_a_job_whose_correlation_does_not_check_out_is_left_alone(self):
        operation_id, job_id = self.gate(self.ctx)
        with tenant_session(self.ctx) as session:
            other = create_operation(session, command_type="connection_check").id
            aggregated = create_operation(
                session, command_type="reference_gate", admission_contract_version=1,
                initial_state=OperationState.ADMITTED,
            ).id
        cases = {
            "no operation id": {"object_id": self.object_id},
            "another command's Operation": {"object_id": self.object_id, "operation_id": other},
            "an aggregated Operation": {"object_id": self.object_id, "operation_id": aggregated},
            "an unknown Operation": {"object_id": self.object_id, "operation_id": "op_missing"},
            "an unknown object": {"object_id": "refobj_missing", "operation_id": operation_id},
        }
        _bump_epoch(self.ctx.workspace_id)
        jobs.recover_expired_jobs()
        for label, payload in cases.items():
            with self.subTest(label):
                with platform_session() as session:
                    session.execute(update(JobRecord).where(JobRecord.id == job_id).values(payload_json=payload))
                self.assertIsNone(legacy_operations.fold_legacy_job(
                    job_id, workspace_id=self.ctx.workspace_id,
                    authorization_epoch=_current_epoch(self.ctx.workspace_id),
                ))
        for target in (operation_id, other):
            self.assertEqual(_transitions(target), [(None, "queued")])
        self.assertEqual(_transitions(aggregated), [(None, "admitted")])

    def test_a_job_its_handler_failed_is_left_to_its_handler(self):
        operation_id, job_id = self.gate(self.ctx)
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == job_id)
                .values(state="failed", error_code="reference_gate_failed")
            )
        legacy_operations.reconcile_legacy_operations()
        self.assertEqual(_transitions(operation_id), [(None, "queued")])

    def test_a_job_left_uncertain_by_lease_expiry_makes_its_operation_uncertain(self):
        """A Job enqueued before 22a still carries ``safe_to_retry_after_crash=False``."""
        operation_id, job_id = self.gate(self.ctx)
        with platform_session() as session:
            session.execute(update(JobRecord).where(JobRecord.id == job_id).values(safe_to_retry_after_crash=False))
        quarantine_foreign_jobs(self.ctx.workspace_id)
        claimed = jobs.claim_next("crashed-worker", job_types=("library.reference_gate",))
        self.assertEqual(claimed.id, job_id)
        jobs.mark_running(claimed.context, claimed.id, "crashed-worker")
        with platform_session() as session:
            session.execute(
                update(JobRecord).where(JobRecord.id == job_id)
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
        jobs.recover_expired_jobs()
        self.assertEqual(
            (_job(job_id).state, _job(job_id).error_code), ("uncertain", "paid_call_outcome_unknown"),
        )

        legacy_operations.reconcile_legacy_operations()
        legacy_operations.reconcile_legacy_operations()

        self.assertEqual(
            _events(operation_id),
            [(None, "queued", "Operation created"),
             ("queued", "running", "dispatch observed while reconciling a legacy Operation"),
             ("running", "uncertain", "paid_call_outcome_unknown")],
        )
        with tenant_session(self.ctx) as session:
            record = session.get(ReferenceObjectRecord, self.object_id)
            self.assertEqual(record.state, "quarantined")
            self.assertNotIn("failure", record.checks_json or {}, "an unknown outcome decides nothing")


class TestTheSweepIsPaced(LibraryFixtures):
    def test_the_idle_tick_sweeps_at_most_once_per_interval(self):
        with patch.object(legacy_operations, "reconcile_legacy_operations", return_value=0) as sweep, \
                patch.object(legacy_operations, "_last_sweep", None):
            legacy_operations.reconcile_legacy_operations_if_due(now=1000.0)
            legacy_operations.reconcile_legacy_operations_if_due(now=1030.0)
            legacy_operations.reconcile_legacy_operations_if_due(now=1061.0)
        self.assertEqual(sweep.call_count, 2)


# ---------------------------------------------------------------------------
# The registry covers every legacy command that enqueues a Job
# ---------------------------------------------------------------------------


APP = Path(__file__).resolve().parents[1] / "app"


def _legacy_enqueued_types() -> dict[str, set[str]]:
    """Job types enqueued in each module that also creates a legacy Operation.

    A legacy Operation is one created by ``create_or_replay_operation`` or by
    ``create_operation`` without an ``admission_contract_version``.
    """
    found: dict[str, set[str]] = {}
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        creates_legacy = False
        enqueued: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            keywords = {kw.arg: kw.value for kw in node.keywords}
            if name == "create_or_replay_operation" or (
                name == "create_operation" and "admission_contract_version" not in keywords
            ):
                creates_legacy = True
            if name == "enqueue_in_session":
                job_type = keywords.get("job_type")
                if isinstance(job_type, ast.Constant) and isinstance(job_type.value, str):
                    enqueued.add(job_type.value)
        if creates_legacy and enqueued:
            found[str(path.relative_to(APP.parent))] = enqueued
    return found


class TestTheRegistryIsComplete(LibraryFixtures):
    def test_every_legacy_command_with_a_job_has_a_resolver(self):
        found = _legacy_enqueued_types()
        self.assertTrue(found, "the scan found no legacy command at all")
        for module, job_types in found.items():
            for job_type in job_types:
                with self.subTest(module=module, job_type=job_type):
                    self.assertIn(job_type, legacy_operations.LEGACY_COMMANDS)

    def test_every_registered_type_has_a_crash_policy(self):
        from app.platform.job_policy import JOB_POLICIES

        self.assertLessEqual(set(legacy_operations.LEGACY_COMMANDS), set(JOB_POLICIES))

    def test_the_postgresql_candidate_function_lists_the_same_endings(self):
        """The endings are fixed in SQL because the claim function writes them."""
        migration = next((APP.parent / "migrations" / "versions").glob("zza0d1e2f3a4_*.py")).read_text()
        body = migration.split("marketai_legacy_operation_candidates_v1(\n", 1)[1].split("$$", 2)[1]
        for state, code in legacy_operations.TRIGGERS:
            with self.subTest(state=state, code=code):
                self.assertIn(f"job.state = '{state}'", body)
                self.assertIn(f"'{code}'", body)
