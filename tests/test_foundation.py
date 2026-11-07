from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


_CONFIGURED_TEST_ROOT = os.environ.get("MARKETAI_TEST_ROOT")
TEST_ROOT = Path(
    _CONFIGURED_TEST_ROOT or tempfile.mkdtemp(prefix="marketai-foundation-")
)
_OWNS_TEST_ROOT = _CONFIGURED_TEST_ROOT is None
os.environ["MARKETAI_TEST_ROOT"] = str(TEST_ROOT)
os.environ["MARKETAI_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["MARKETAI_DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'marketai.db'}"
os.environ["MARKETAI_WORKSPACE_ID"] = "ws_test"
os.environ["MAX_COST_PER_RUN"] = "5"
os.environ["MAX_COST_PER_DAY"] = "20"
os.environ["JOB_LEASE_SECONDS"] = "1"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import func, select, text, update  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from app import store  # noqa: E402
from app import config  # noqa: E402
from app.adapters.meta.client import MetaClient  # noqa: E402
from app.adapters.persistence.database import engine, platform_session, tenant_session  # noqa: E402
from app.adapters.persistence.json_importer import import_json_tree  # noqa: E402
from app.adapters.persistence.models import (  # noqa: E402
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    AuditEventRecord,
    ExperimentDecisionRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
    GenerationRunRecord,
    JobRecord,
    ImportBatchRecord,
    JudgementRecord,
    LaunchConfirmationRecord,
    LaunchAuditRecord,
    LaunchOperationRecord,
    LaunchPreviewRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
    MetaEntityStatusEventRecord,
    MetaIngestionRecord,
    MetaSyncScheduleRecord,
    MetaSyncRunRecord,
    OperationRecord,
    OperationStateEventRecord,
    WorkspaceRecord,
    utcnow,
)
from app.meta import service as meta_service  # noqa: E402
from app.meta.contracts import contract_hash, metric_contract, request_snapshot  # noqa: E402
from app.experiments import service as experiment_service  # noqa: E402
from app.experiments.judge import judge as pure_judge  # noqa: E402
from app.launch import service as launch_service  # noqa: E402
from app.adapters.meta.fake_write import FakeMetaWriteAdapter  # noqa: E402
from app.pipeline import manager  # noqa: E402
from app.platform import jobs  # noqa: E402
from app.platform.audit import append_event  # noqa: E402
from app.platform.job_policy import CrashPolicy, SideEffect  # noqa: E402
from app.schemas import (  # noqa: E402
    AngleSchema,
    Combo,
    Dimension,
    DimensionValue,
    GenRequest,
    Plan,
    Product,
    Run,
)
from app.worker_runtime import WorkerRuntime  # noqa: E402
from app.platform.tenancy import TenantContext  # noqa: E402
from tests.support.job_policies import probe_job_types  # noqa: E402


TEST_CONTEXT = TenantContext(
    workspace_id="ws_test",
    role="owner",
    initiator_id="user_test",
    request_id="req_test",
    trace_id="trace_test",
)


class FoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()
        if _OWNS_TEST_ROOT:
            shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def _seed_product_plan(
        self, suffix: str, context: TenantContext = TEST_CONTEXT
    ) -> tuple[Product, AngleSchema, Plan]:
        product = Product(id=f"prod_{suffix}", name=f"Product {suffix}")
        store.save_product(context, product)
        angles = AngleSchema(
            product_id=product.id,
            dimensions=[
                Dimension(
                    id=f"dim_{suffix}",
                    label="场景",
                    values=[DimensionValue(id=f"value_{suffix}", label="车库", prompt_fragment="garage")],
                )
            ],
            source="manual",
        )
        store.save_angles(context, angles)
        plan = Plan(
            id=f"plan_{suffix}",
            product_id=product.id,
            mode="full",
            cap=0,
            total_full=1,
            combos=[
                Combo(
                    id="C01",
                    code="A1",
                    values={f"dim_{suffix}": f"value_{suffix}"},
                    tags={"场景": "车库"},
                )
            ],
        )
        store.save_plan(context, plan)
        return product, angles, plan

    def test_cross_tenant_api_contract_is_identical_for_every_current_aggregate(
        self,
    ) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        other = TenantContext(
            workspace_id="ws_api_matrix_other",
            role="member",
            initiator_id="user_api_matrix_other",
            request_id="req_api_matrix_other",
            trace_id="trace_api_matrix_other",
        )
        product, _angles, plan = self._seed_product_plan("api_matrix_other", other)
        run_id = "run_api_matrix_other"
        asset_id = "asset_api_matrix_other"
        version_id = "version_api_matrix_other"
        account_id = "mact_api_matrix_other"
        sync_id = "msync_api_matrix_other"
        experiment_id = "exp_api_matrix_other"
        entry_id = "entry_api_matrix_other"
        preview_id = "preview_api_matrix_other"
        confirmation_id = "confirm_api_matrix_other"
        operation_id = "launch_api_matrix_other"
        now = utcnow()
        with tenant_session(other) as session:
            session.add_all(
                [
                    GenerationRunRecord(
                        id=run_id,
                        workspace_id=other.workspace_id,
                        product_id=product.id,
                        plan_id=plan.id,
                        provider="mock",
                        model="mock",
                        aspect_ratio="1:1",
                        image_size="1024",
                        variants=1,
                        use_ref=False,
                        status="done",
                        total=1,
                        done=1,
                        failed=0,
                        est_cost=Decimal("0"),
                        currency="USD",
                    ),
                    MetaAccountRecord(
                        id=account_id,
                        workspace_id=other.workspace_id,
                        ad_account_id="act_api_matrix_other",
                        currency="USD",
                        timezone_name="UTC",
                        api_version="v23.0",
                    ),
                ]
            )
            session.flush()
            session.add(
                AssetRecord(
                    id=asset_id,
                    workspace_id=other.workspace_id,
                    product_id=product.id,
                    run_id=run_id,
                    combo_id="C01",
                    combo_code="A1",
                    tags_json={},
                    filename="matrix.png",
                    provider="mock",
                    model="mock",
                    prompt="matrix",
                    fingerprint="api-matrix-fingerprint",
                    status="ok",
                )
            )
            session.flush()
            session.add(
                AssetVersionRecord(
                    id=version_id,
                    workspace_id=other.workspace_id,
                    asset_id=asset_id,
                    kind="generated",
                    aspect_ratio="1:1",
                    language="en",
                    object_key="matrix.png",
                    content_sha256="a" * 64,
                    byte_size=1,
                    mime_type="image/png",
                    currency="USD",
                )
            )
            session.add(
                MetaSyncRunRecord(
                    id=sync_id,
                    workspace_id=other.workspace_id,
                    account_id=account_id,
                    state="completed",
                    date_start=date(2026, 8, 1),
                    date_stop=date(2026, 8, 1),
                    contract_hash="b" * 64,
                    contract_json={},
                    api_version="v23.0",
                    account_currency="USD",
                    account_timezone="UTC",
                    idempotency_key="api-matrix-sync",
                )
            )
            session.add(
                ExperimentRecord(
                    id=experiment_id,
                    workspace_id=other.workspace_id,
                    product_id=product.id,
                    name="Other tenant experiment",
                    state="draft",
                    revision=1,
                    config_json={"primary_metric": "ctr"},
                    metric_contract_hash="c" * 64,
                )
            )
            session.flush()
            session.add(
                ExperimentEntryRecord(
                    id=entry_id,
                    workspace_id=other.workspace_id,
                    experiment_id=experiment_id,
                    asset_version_id=version_id,
                    marketing_vars_json={},
                    position=0,
                )
            )
            session.flush()
            session.add(
                LaunchPreviewRecord(
                    id=preview_id,
                    workspace_id=other.workspace_id,
                    experiment_id=experiment_id,
                    experiment_entry_id=entry_id,
                    asset_version_id=version_id,
                    ad_account_id="act_api_matrix_other",
                    preview_json={},
                    preview_hash="d" * 64,
                    expires_at=now + timedelta(hours=1),
                    created_by=other.initiator_id,
                )
            )
            session.flush()
            session.add(
                LaunchConfirmationRecord(
                    id=confirmation_id,
                    preview_id=preview_id,
                    preview_hash="d" * 64,
                    token_hash="e" * 64,
                    actor_id=other.initiator_id,
                    expires_at=now + timedelta(hours=1),
                )
            )
            session.flush()
            session.add(
                LaunchOperationRecord(
                    id=operation_id,
                    workspace_id=other.workspace_id,
                    preview_id=preview_id,
                    confirmation_id=confirmation_id,
                    experiment_id=experiment_id,
                    state="queued",
                    current_step="planned",
                    marker="MAI|api-matrix-other",
                )
            )

        app.dependency_overrides[request_tenant_context] = lambda: TEST_CONTEXT
        try:
            with TestClient(app) as client:
                not_found_pairs = (
                    (
                        f"/api/products/{product.id}",
                        "/api/products/prod_never_issued",
                    ),
                    (
                        f"/api/runs/{run_id}?pid={product.id}",
                        "/api/runs/run_never_issued?pid=prod_never_issued",
                    ),
                    (
                        f"/api/meta/syncs/{sync_id}",
                        "/api/meta/syncs/msync_never_issued",
                    ),
                    (
                        f"/api/experiments/{experiment_id}",
                        "/api/experiments/exp_never_issued",
                    ),
                    (
                        f"/api/launch/operations/{operation_id}",
                        "/api/launch/operations/launch_never_issued",
                    ),
                )
                for known_other, never_issued in not_found_pairs:
                    with self.subTest(path=known_other):
                        hidden = client.get(known_other)
                        absent = client.get(never_issued)
                        self.assertEqual(hidden.status_code, 404)
                        self.assertEqual(hidden.status_code, absent.status_code)
                        hidden_problem = hidden.json()
                        absent_problem = absent.json()
                        # Trace IDs are request correlation identities and must
                        # differ across requests.  Compare the observable error
                        # contract while still requiring both responses to carry
                        # a non-empty trace for support and audit correlation.
                        self.assertTrue(hidden_problem.pop("trace_id", ""))
                        self.assertTrue(absent_problem.pop("trace_id", ""))
                        self.assertEqual(hidden_problem, absent_problem)

                collection_pairs = (
                    (
                        f"/api/products/{product.id}/angles",
                        "/api/products/prod_never_issued/angles",
                    ),
                    (
                        f"/api/products/{product.id}/plan",
                        "/api/products/prod_never_issued/plan",
                    ),
                    (
                        f"/api/products/{product.id}/runs",
                        "/api/products/prod_never_issued/runs",
                    ),
                    (
                        f"/api/products/{product.id}/assets",
                        "/api/products/prod_never_issued/assets",
                    ),
                )
                for known_other, never_issued in collection_pairs:
                    with self.subTest(path=known_other):
                        hidden = client.get(known_other)
                        absent = client.get(never_issued)
                        self.assertEqual(hidden.status_code, 200)
                        self.assertEqual(hidden.json(), absent.json())

                self.assertIsNone(store.get_product(TEST_CONTEXT, product.id))
                self.assertIsNone(store.get_angles(TEST_CONTEXT, product.id))
                self.assertIsNone(store.get_plan(TEST_CONTEXT, product.id))
                self.assertIsNone(store.get_run(TEST_CONTEXT, product.id, run_id))
                self.assertEqual(store.list_runs(TEST_CONTEXT, product.id), [])
                self.assertIsNone(store.get_asset(TEST_CONTEXT, asset_id))
                self.assertEqual(store.get_quality(TEST_CONTEXT, product.id), {})
                self.assertEqual(store.get_verdicts(TEST_CONTEXT, product.id), {})
                with patch.object(
                    config,
                    "META_CREDENTIAL_WORKSPACE_ID",
                    TEST_CONTEXT.workspace_id,
                ):
                    self.assertIsNone(
                        meta_service.sync_status(TEST_CONTEXT, sync_id)["sync"]
                    )
                with self.assertRaises(experiment_service.ExperimentNotFound):
                    experiment_service.get_experiment(TEST_CONTEXT, experiment_id)
                with self.assertRaises(launch_service.LaunchNotFound):
                    launch_service.get_operation(TEST_CONTEXT, operation_id)
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)

    def test_explicit_local_sqlite_identity_serves_the_browser_without_an_override(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        product, _angles, _plan = self._seed_product_plan("local_browser")
        self.assertNotIn(request_tenant_context, app.dependency_overrides)
        with (
            patch.object(config, "LOCAL_IDENTITY_ENABLED", True),
            patch.object(config, "LOCAL_WORKSPACE_ID", TEST_CONTEXT.workspace_id),
            patch.object(config, "LOCAL_INITIATOR_ID", TEST_CONTEXT.initiator_id),
            TestClient(app) as client,
        ):
            response = client.get("/api/products")

        self.assertEqual(response.status_code, 200)
        self.assertIn(product.id, {row["id"] for row in response.json()})

    def test_worker_job_kinds_do_not_follow_foreign_workspace_identifiers(self) -> None:
        self._seed_product_plan("worker_matrix_home")
        other = TenantContext(
            workspace_id="ws_worker_matrix_other",
            role="owner",
            initiator_id="user_worker_matrix_other",
            request_id="req_worker_matrix_other",
            trace_id="trace_worker_matrix_other",
        )
        product, _angles, plan = self._seed_product_plan("worker_matrix_other", other)
        run_id = "run_worker_matrix_other"
        asset_id = "asset_worker_matrix_other"
        version_id = "version_worker_matrix_other"
        account_id = "mact_worker_matrix_other"
        sync_id = "msync_worker_matrix_other"
        ingestion_id = "ming_worker_matrix_other"
        schedule_id = "msched_worker_matrix_other"
        experiment_id = "exp_worker_matrix_other"
        entry_id = "entry_worker_matrix_other"
        preview_id = "preview_worker_matrix_other"
        confirmation_id = "confirm_worker_matrix_other"
        operation_id = "launch_worker_matrix_other"
        original_schedule_time = utcnow() - timedelta(minutes=5)
        with tenant_session(other) as session:
            session.add_all(
                [
                    GenerationRunRecord(
                        id=run_id,
                        workspace_id=other.workspace_id,
                        product_id=product.id,
                        plan_id=plan.id,
                        provider="mock",
                        model="mock",
                        aspect_ratio="1:1",
                        image_size="1024",
                        variants=1,
                        use_ref=False,
                        status="done",
                        total=1,
                        done=1,
                        failed=0,
                        est_cost=Decimal("0"),
                        currency="USD",
                    ),
                    MetaAccountRecord(
                        id=account_id,
                        workspace_id=other.workspace_id,
                        ad_account_id="act_worker_matrix_other",
                        currency="USD",
                        timezone_name="UTC",
                        api_version="v23.0",
                    ),
                ]
            )
            session.flush()
            session.add(
                AssetRecord(
                    id=asset_id,
                    workspace_id=other.workspace_id,
                    product_id=product.id,
                    run_id=run_id,
                    combo_id="C01",
                    combo_code="A1",
                    tags_json={},
                    filename="worker-matrix.png",
                    provider="mock",
                    model="mock",
                    prompt="worker matrix",
                    fingerprint="worker-matrix-fingerprint",
                    status="ok",
                )
            )
            session.flush()
            session.add_all(
                [
                    AssetVersionRecord(
                        id=version_id,
                        workspace_id=other.workspace_id,
                        asset_id=asset_id,
                        kind="generated",
                        aspect_ratio="1:1",
                        language="en",
                        object_key="worker-matrix.png",
                        content_sha256="f" * 64,
                        byte_size=1,
                        mime_type="image/png",
                        currency="USD",
                    ),
                    MetaSyncRunRecord(
                        id=sync_id,
                        workspace_id=other.workspace_id,
                        account_id=account_id,
                        state="queued",
                        date_start=date(2026, 8, 1),
                        date_stop=date(2026, 8, 1),
                        contract_hash="a" * 64,
                        contract_json={},
                        api_version="v23.0",
                        account_currency="USD",
                        account_timezone="UTC",
                        idempotency_key="worker-matrix-sync",
                    ),
                    MetaSyncScheduleRecord(
                        id=schedule_id,
                        workspace_id=other.workspace_id,
                        account_id=account_id,
                        enabled=True,
                        timezone_name="UTC",
                        local_hour=3,
                        lookback_days=1,
                        next_run_at=original_schedule_time,
                    ),
                    ExperimentRecord(
                        id=experiment_id,
                        workspace_id=other.workspace_id,
                        product_id=product.id,
                        name="Worker matrix experiment",
                        state="frozen",
                        revision=1,
                        config_json={"primary_metric": "ctr"},
                        metric_contract_hash="b" * 64,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    MetaIngestionRecord(
                        id=ingestion_id,
                        sync_run_id=sync_id,
                        kind="account",
                        state="queued",
                        request_json={},
                    ),
                    ExperimentEntryRecord(
                        id=entry_id,
                        workspace_id=other.workspace_id,
                        experiment_id=experiment_id,
                        asset_version_id=version_id,
                        marketing_vars_json={},
                        position=0,
                    ),
                ]
            )
            session.flush()
            session.add(
                LaunchPreviewRecord(
                    id=preview_id,
                    workspace_id=other.workspace_id,
                    experiment_id=experiment_id,
                    experiment_entry_id=entry_id,
                    asset_version_id=version_id,
                    ad_account_id="act_worker_matrix_other",
                    preview_json={},
                    preview_hash="c" * 64,
                    expires_at=utcnow() + timedelta(hours=1),
                    created_by=other.initiator_id,
                )
            )
            session.flush()
            session.add(
                LaunchConfirmationRecord(
                    id=confirmation_id,
                    preview_id=preview_id,
                    preview_hash="c" * 64,
                    token_hash="d" * 64,
                    actor_id=other.initiator_id,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            session.flush()
            session.add(
                LaunchOperationRecord(
                    id=operation_id,
                    workspace_id=other.workspace_id,
                    preview_id=preview_id,
                    confirmation_id=confirmation_id,
                    experiment_id=experiment_id,
                    state="queued",
                    current_step="planned",
                    marker="MAI|worker-matrix-other",
                )
            )

        attacker_jobs: dict[str, str] = {}
        with tenant_session(TEST_CONTEXT) as session:
            for position, (job_type, payload) in enumerate(
                (
                    ("meta_read_ingestion", {"ingestion_id": ingestion_id}),
                    ("meta_schedule_tick", {"schedule_id": schedule_id}),
                    ("launch_canary", {"operation_id": operation_id}),
                )
            ):
                job = jobs.enqueue_in_session(
                    session,
                    context=TEST_CONTEXT,
                    job_type=job_type,
                    payload=payload,
                    idempotency_key=f"worker-matrix:{job_type}",
                    max_attempts=1,
                )
                job.created_at = utcnow() - timedelta(days=1, seconds=-position)
                attacker_jobs[job_type] = job.id

        remote_state: dict = {}
        runtime = WorkerRuntime(
            owner="cross-tenant-matrix-worker",
            launch_adapter=FakeMetaWriteAdapter(remote_state),
        )
        with patch.object(
            config,
            "META_CREDENTIAL_WORKSPACE_ID",
            TEST_CONTEXT.workspace_id,
        ):
            for _ in range(3):
                self.assertTrue(asyncio.run(runtime.run_once()))

        with platform_session() as session:
            states = {
                job_type: session.get(JobRecord, job_id).state
                for job_type, job_id in attacker_jobs.items()
            }
            self.assertEqual(
                states,
                {
                    "meta_read_ingestion": "failed",
                    "meta_schedule_tick": "succeeded",
                    "launch_canary": "failed",
                },
            )
            self.assertEqual(session.get(MetaIngestionRecord, ingestion_id).state, "queued")
            self.assertEqual(
                session.get(MetaSyncScheduleRecord, schedule_id).next_run_at,
                original_schedule_time.replace(tzinfo=None),
            )
            self.assertEqual(session.get(LaunchOperationRecord, operation_id).state, "queued")
        self.assertEqual(remote_state, {})

    def test_json_import_cli_passes_an_explicit_tenant_context(self) -> None:
        from scripts.import_json import main as import_main

        report_path = TEST_ROOT / "cli-import-report.json"
        with patch(
            "scripts.import_json.import_json_tree", return_value={"ok": True}
        ) as importer, patch.object(
            sys,
            "argv",
            [
                "import_json",
                "--source",
                str(TEST_ROOT),
                "--report",
                str(report_path),
                "--workspace",
                "ws_cli_import",
                "--initiator",
                "user_cli_import",
            ],
        ), redirect_stdout(io.StringIO()):
            import_main()

        context, source = importer.call_args.args
        self.assertEqual(context.workspace_id, "ws_cli_import")
        self.assertEqual(context.initiator_id, "user_cli_import")
        self.assertEqual(context.initiator_role, "owner")
        self.assertEqual(context.role, "system")
        self.assertEqual(context.executor, "system:migration")
        self.assertTrue(context.request_id.startswith("req_"))
        self.assertEqual(context.trace_id, context.request_id)
        self.assertEqual(source, TEST_ROOT)

    def test_json_import_cli_refuses_an_implicit_tenant_or_initiator(self) -> None:
        from scripts.import_json import main as import_main

        for arguments in (
            ["import_json", "--initiator", "operator"],
            ["import_json", "--workspace", "ws_explicit"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(sys, "argv", arguments),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                import_main()

    def test_repository_context_hides_other_workspaces_and_rejects_stale_epoch(self) -> None:
        other = TenantContext(
            workspace_id="ws_other",
            role="owner",
            initiator_id="user_other",
            request_id="req_other",
        )
        product = Product(id="prod_other_workspace", name="Other tenant")
        store.save_product(other, product)

        self.assertIsNone(store.get_product(TEST_CONTEXT, product.id))
        self.assertEqual(store.get_product(other, product.id).name, "Other tenant")

        with probe_job_types("tenant_boundary_probe"), tenant_session(other) as session:
            foreign_job = jobs.enqueue_in_session(
                session,
                context=other,
                job_type="tenant_boundary_probe",
                payload={},
                idempotency_key="tenant-boundary-probe",
                resource_type="probe",
                resource_id="shared-looking-id",
            )
            foreign_job_id = foreign_job.id
        self.assertEqual(
            jobs.cancel_resource(TEST_CONTEXT, "probe", "shared-looking-id"), 0
        )
        with tenant_session(other) as session:
            self.assertEqual(session.get(JobRecord, foreign_job_id).state, "queued")
        self.assertEqual(jobs.cancel_resource(other, "probe", "shared-looking-id"), 1)

        stale = TenantContext(
            workspace_id=other.workspace_id,
            role="owner",
            initiator_id="user_other",
            authorization_epoch=other.authorization_epoch + 1,
        )
        with self.assertRaisesRegex(RuntimeError, "epoch is stale"):
            store.list_products(stale)

    def test_sqlite_dispatch_rejects_a_stale_authorization_epoch(self) -> None:
        context = TenantContext(
            workspace_id="ws_stale_dispatch",
            role="owner",
            initiator_id="user_stale_dispatch",
            authorization_epoch=0,
        )
        store.save_product(context, Product(id="prod_stale_dispatch", name="Stale job"))
        with probe_job_types("stale_dispatch_probe"), tenant_session(context) as session:
            job = jobs.enqueue_in_session(
                session,
                context=context,
                job_type="stale_dispatch_probe",
                payload={},
                idempotency_key="stale-dispatch-probe",
            )
            job_id = job.id
        with platform_session() as session:
            workspace = session.get(WorkspaceRecord, context.workspace_id)
            workspace.authorization_epoch = 1

        self.assertEqual(jobs._reject_stale_sqlite_jobs(utcnow()), 1)
        with platform_session() as session:
            rejected = session.get(JobRecord, job_id)
            self.assertEqual(rejected.state, "failed")
            self.assertEqual(rejected.error_code, "authorization_epoch_stale")
            audit = session.scalar(
                select(AuditEventRecord).where(
                    AuditEventRecord.event_type == "job.authorization_rejected",
                    AuditEventRecord.resource_id == job_id,
                )
            )
            self.assertEqual(audit.initiator_role, "owner")
            self.assertEqual(audit.executor, "system:worker")

    def test_sqlite_dispatch_rejects_an_expired_stale_lease_before_recovery(self) -> None:
        context = TenantContext(
            workspace_id="ws_stale_expired_dispatch",
            role="owner",
            initiator_id="user_stale_expired_dispatch",
            authorization_epoch=0,
        )
        product, _angles, plan = self._seed_product_plan(
            "stale_expired_dispatch", context
        )
        run_id = "run_stale_expired_dispatch"
        asset_id = "asset_stale_expired_dispatch"
        with tenant_session(context) as session:
            session.add(
                GenerationRunRecord(
                    id=run_id,
                    workspace_id=context.workspace_id,
                    product_id=product.id,
                    plan_id=plan.id,
                    provider="mock",
                    model="mock",
                    aspect_ratio="1:1",
                    image_size="1024",
                    variants=1,
                    use_ref=False,
                    status="done",
                    total=1,
                    done=1,
                    failed=0,
                    est_cost=Decimal("0"),
                    currency="USD",
                )
            )
            session.flush()
            session.add(
                AssetRecord(
                    id=asset_id,
                    workspace_id=context.workspace_id,
                    product_id=product.id,
                    run_id=run_id,
                    combo_id="C01",
                    combo_code="A1",
                    tags_json={},
                    filename="stale.png",
                    provider="mock",
                    model="mock",
                    prompt="stale",
                    fingerprint="stale-expired-dispatch",
                    status="ok",
                )
            )
            job = jobs.enqueue_in_session(
                session,
                context=context,
                job_type="generate_asset",
                payload={"asset_id": asset_id},
                idempotency_key="stale-expired-dispatch",
                safe_to_retry_after_crash=False,
            )
            job.state = "running"
            job.lease_owner = "old-worker"
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
            job_id = job.id

        with platform_session() as session:
            session.get(WorkspaceRecord, context.workspace_id).authorization_epoch = 1

        jobs.recover_expired_jobs()

        with platform_session() as session:
            rejected = session.get(JobRecord, job_id)
            self.assertEqual(rejected.state, "failed")
            self.assertEqual(rejected.error_code, "authorization_epoch_stale")

    def test_meta_contract_uses_decision_attribution_window(self) -> None:
        contract = metric_contract()
        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["action_attribution_windows"], ["7d_click", "1d_view"])
        snapshot = request_snapshot("insights", date(2026, 8, 1), date(2026, 8, 2))
        self.assertEqual(
            json.loads(snapshot["params"]["action_attribution_windows"]),
            ["7d_click", "1d_view"],
        )
        legacy = {**contract, "schema_version": 1, "action_attribution_windows": ["1d_click", "1d_view"]}
        self.assertNotEqual(contract_hash(contract), contract_hash(legacy))

    def test_generation_is_persistent_and_creates_asset_version(self) -> None:
        product, angles, plan = self._seed_product_plan("generation")
        run = manager.start(TEST_CONTEXT, product, angles, plan, GenRequest(product_id=product.id, provider="mock"))
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.est_cost, Decimal("0.00"))

        handled = asyncio.run(WorkerRuntime(owner="test-worker").run_once())
        self.assertTrue(handled)
        persisted = store.get_run(TEST_CONTEXT, product.id, run.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "done")
        self.assertEqual(persisted.done, 1)
        self.assertTrue((store.run_dir(product.id, run.id) / persisted.assets[0].filename).is_file())
        with platform_session() as session:
            version_count = session.scalar(
                select(func.count())
                .select_from(AssetVersionRecord)
                .join(AssetRecord)
                .where(AssetRecord.run_id == run.id)
            )
            created_event = session.scalar(
                select(AuditEventRecord).where(
                    AuditEventRecord.event_type == "generation_run.created",
                    AuditEventRecord.resource_id == run.id,
                )
            )
            completed_event = session.scalar(
                select(AuditEventRecord).where(
                    AuditEventRecord.event_type == "asset.completed",
                    AuditEventRecord.resource_id == run.assets[0].id,
                )
            )
        self.assertEqual(version_count, 1)
        self.assertEqual(created_event.initiator_id, "user_test")
        self.assertEqual(created_event.executor, "user")
        self.assertEqual(completed_event.initiator_id, "user_test")
        self.assertEqual(completed_event.initiator_role, "owner")
        self.assertEqual(completed_event.executor, "system:worker")
        self.assertEqual(completed_event.request_id, "req_test")
        self.assertEqual(completed_event.trace_id, "trace_test")

    def test_sqlite_audit_events_are_append_only_even_for_the_owner(self) -> None:
        with tenant_session(TEST_CONTEXT) as session:
            event = append_event(
                session,
                event_type="audit.sqlite_probe",
                resource_type="probe",
                resource_id="sqlite",
            )
            session.flush()
            for statement in (
                "UPDATE audit_events SET event_type = 'rewritten' WHERE id = :id",
                "DELETE FROM audit_events WHERE id = :id",
            ):
                with self.assertRaises(DBAPIError):
                    with session.begin_nested():
                        session.execute(text(statement), {"id": event.id})

    def test_legacy_launch_audit_uses_the_same_redaction_contract(self) -> None:
        event_type = "launch_redaction_probe"
        with tenant_session(TEST_CONTEXT) as session:
            launch_service._audit(
                session,
                event_type,
                {
                    "password": "legacy-secret",
                    "url": "https://alice:secret@example.test/path?token=query-secret",
                },
            )
        with platform_session() as session:
            legacy = session.scalar(
                select(LaunchAuditRecord).where(
                    LaunchAuditRecord.event_type == event_type
                )
            )
            generic = session.scalar(
                select(AuditEventRecord).where(
                    AuditEventRecord.event_type == event_type
                )
            )

        for details in (legacy.details_json, generic.details_json):
            encoded = json.dumps(details, sort_keys=True)
            self.assertNotIn("legacy-secret", encoded)
            self.assertNotIn("alice", encoded)
            self.assertNotIn("query-secret", encoded)

    def test_quality_and_verdict_are_append_only_with_latest_projection(self) -> None:
        product, angles, plan = self._seed_product_plan("history")
        run = manager.start(TEST_CONTEXT, product, angles, plan, GenRequest(product_id=product.id, provider="mock"))
        asyncio.run(WorkerRuntime(owner="history-worker").run_once())
        asset_id = run.assets[0].id

        store.set_quality(TEST_CONTEXT, product.id, asset_id, "needs_changes")
        store.set_quality(TEST_CONTEXT, product.id, asset_id, "approved")
        self.assertEqual(store.get_quality(TEST_CONTEXT, product.id)[asset_id], "approved")
        store.set_verdict(TEST_CONTEXT, product.id, asset_id, "learn", {"market": "US"})
        store.set_verdict(TEST_CONTEXT, product.id, asset_id, "win", {"market": "KR"})
        projection = store.get_verdicts(TEST_CONTEXT, product.id)[asset_id]
        self.assertEqual(projection["verdict"], "win")
        self.assertEqual(projection["context"]["market"], "KR")

    def test_expired_paid_running_job_becomes_uncertain(self) -> None:
        with probe_job_types(
            "paid-test", side_effect=SideEffect.PAID, crash=CrashPolicy.UNCERTAIN,
        ), tenant_session(TEST_CONTEXT) as session:
            job = jobs.enqueue_in_session(
                session,
                context=TEST_CONTEXT,
                job_type="paid-test",
                payload={"value": 1},
                idempotency_key="paid-test-1",
                safe_to_retry_after_crash=False,
            )
            job_id = job.id
        claimed = jobs.claim_next("crash-worker", lease_seconds=1)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job_id)
        self.assertEqual(claimed.context.workspace_id, TEST_CONTEXT.workspace_id)
        self.assertEqual(claimed.context.initiator_id, TEST_CONTEXT.initiator_id)
        self.assertEqual(claimed.context.executor, "system:worker")
        jobs.mark_running(claimed.context, job_id, "crash-worker", lease_seconds=1)
        with platform_session() as session:
            row = session.get(JobRecord, job_id)
            row.lease_expires_at = utcnow() - timedelta(seconds=1)
        result = jobs.recover_expired_jobs()
        self.assertEqual(result["uncertain"], 1)
        with platform_session() as session:
            self.assertEqual(session.get(JobRecord, job_id).state, "uncertain")
            transitions = session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.resource_id == job_id)
                .order_by(AuditEventRecord.created_at)
            ).all()
        by_type = {event.event_type: event for event in transitions}
        self.assertTrue(
            {"job.enqueued", "job.leased", "job.running", "job.uncertain"}
            <= set(by_type)
        )
        for event in by_type.values():
            self.assertEqual(event.initiator_role, "owner")
        enqueued_details = by_type["job.enqueued"].details_json
        self.assertNotIn("paid-test-1", json.dumps(enqueued_details))
        self.assertEqual(len(enqueued_details["idempotency_key_sha256"]), 64)

    def test_expired_launch_job_records_valid_operation_transitions(self) -> None:
        from app.platform.operations import create_operation

        self._seed_product_plan("launch_recovery_transition")
        launch_context = TenantContext(
            workspace_id=TEST_CONTEXT.workspace_id,
            role=TEST_CONTEXT.role,
            initiator_id=TEST_CONTEXT.initiator_id,
            request_id=TEST_CONTEXT.request_id,
            trace_id=TEST_CONTEXT.trace_id,
            meta_mode="customer_sandbox",
        )
        with tenant_session(launch_context) as session:
            operation = create_operation(
                session,
                command_type="launch_provisioning",
                actor=launch_context.initiator_id,
            )
            job = jobs.enqueue_in_session(
                session,
                context=launch_context,
                job_type="launch_provisioning",
                payload={
                    "operation_id": operation.id,
                    "meta_mode": launch_context.meta_mode,
                },
                idempotency_key=f"expired-launch:{operation.id}",
                safe_to_retry_after_crash=False,
            )
            operation_id = operation.id
            job_id = job.id

        # Reproduce the crash window: the dispatcher persisted Job.running,
        # but the worker had not yet persisted Operation.running.
        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            job.state = "running"
            job.lease_owner = "expired-launch-worker"
            job.lease_expires_at = utcnow() - timedelta(seconds=1)

        jobs.recover_expired_jobs()

        with platform_session() as session:
            self.assertEqual(session.get(JobRecord, job_id).state, "uncertain")
            self.assertEqual(
                session.get(OperationRecord, operation_id).state,
                "uncertain",
            )
            events = session.scalars(
                select(OperationStateEventRecord)
                .where(OperationStateEventRecord.operation_id == operation_id)
                .order_by(OperationStateEventRecord.created_at, OperationStateEventRecord.id)
            ).all()
        self.assertEqual(
            [(event.from_state, event.to_state) for event in events],
            [
                (None, "queued"),
                ("queued", "running"),
                ("running", "uncertain"),
            ],
        )

    def test_dispatched_job_can_be_marked_uncertain_without_failure(self) -> None:
        self._seed_product_plan("explicit_job_uncertain")
        with tenant_session(TEST_CONTEXT) as session:
            job = jobs.enqueue_in_session(
                session,
                context=TEST_CONTEXT,
                job_type="launch_provisioning",
                payload={"operation_id": "op_unknown"},
                idempotency_key="explicit-job-uncertain",
                safe_to_retry_after_crash=False,
            )
            job_id = job.id

        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            job.state = "running"
            job.lease_owner = "uncertain-worker"
            job.lease_expires_at = utcnow() + timedelta(seconds=30)

        jobs.mark_uncertain(
            TEST_CONTEXT,
            job_id,
            "uncertain-worker",
            code="launch_outcome_uncertain",
            detail="request dispatched without a recorded outcome",
        )

        with platform_session() as session:
            job = session.get(JobRecord, job_id)
            events = session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.resource_id == job_id)
            ).all()
        self.assertEqual(job.state, "uncertain")
        self.assertIsNone(job.finished_at)
        self.assertIsNone(job.lease_owner)
        self.assertEqual(job.error_code, "launch_outcome_uncertain")
        self.assertIn("job.uncertain", {event.event_type for event in events})
        self.assertNotIn("job.failed", {event.event_type for event in events})

    def test_expired_job_cannot_recover_from_another_workspace_asset(self) -> None:
        # Keep this test independently runnable: the home Workspace must exist
        # before its job can satisfy the jobs.workspace_id foreign key.
        self._seed_product_plan("recovery_home")
        other = TenantContext(
            workspace_id="ws_recovery_other",
            role="member",
            initiator_id="user_recovery_other",
            request_id="req_recovery_other",
            trace_id="trace_recovery_other",
        )
        product, _angles, plan = self._seed_product_plan("recovery_other", other)
        run_id = "run_recovery_other"
        asset_id = "asset_recovery_other"
        with tenant_session(other) as session:
            session.add(
                GenerationRunRecord(
                    id=run_id,
                    workspace_id=other.workspace_id,
                    product_id=product.id,
                    plan_id=plan.id,
                    provider="mock",
                    model="mock",
                    aspect_ratio="1:1",
                    image_size="1024",
                    variants=1,
                    use_ref=False,
                    status="done",
                    total=1,
                    done=1,
                    failed=0,
                    est_cost=Decimal("0"),
                    currency="USD",
                )
            )
            session.flush()
            session.add(
                AssetRecord(
                    id=asset_id,
                    workspace_id=other.workspace_id,
                    product_id=product.id,
                    run_id=run_id,
                    combo_id="C01",
                    combo_code="A1",
                    tags_json={},
                    filename="other.png",
                    provider="mock",
                    model="mock",
                    prompt="other",
                    fingerprint="recovery-other",
                    status="ok",
                )
            )

        with tenant_session(TEST_CONTEXT) as session:
            job = jobs.enqueue_in_session(
                session,
                context=TEST_CONTEXT,
                job_type="generate_asset",
                payload={"asset_id": asset_id},
                idempotency_key="cross-workspace-recovery",
                safe_to_retry_after_crash=False,
            )
            job.state = "running"
            job.lease_owner = "crashed-worker"
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
            job_id = job.id

        result = jobs.recover_expired_jobs()

        self.assertEqual(result, {"recovered": 0, "uncertain": 1})
        with platform_session() as session:
            self.assertEqual(session.get(JobRecord, job_id).state, "uncertain")

    def test_cost_hard_limit_rejects_before_enqueue(self) -> None:
        product, angles, plan = self._seed_product_plan("cost")
        run = Run(
            id="run_too_expensive",
            product_id=product.id,
            plan_id=plan.id,
            provider="openai",
            model="test",
            aspect_ratio="1:1",
            image_size="1K",
            variants=1,
            use_ref=False,
            total=1,
            est_cost=Decimal("5.01"),
            currency="USD",
        )
        with self.assertRaises(store.CostLimitExceeded):
            store.create_run(TEST_CONTEXT, run)

    def test_json_import_is_idempotent_and_reports_counts(self) -> None:
        legacy = TEST_ROOT / "legacy"
        product_dir = legacy / "products" / "prod_legacy"
        product_dir.mkdir(parents=True)
        product = Product(id="prod_legacy", name="Legacy Product")
        (product_dir / "product.json").write_text(
            json.dumps(product.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
        )
        first = import_json_tree(TEST_CONTEXT, legacy)
        second = import_json_tree(TEST_CONTEXT, legacy)
        self.assertTrue(first["ok"])
        self.assertEqual(first["counts"]["products"], 1)
        self.assertTrue(second["idempotent_replay"])
        self.assertIsNotNone(store.get_product(TEST_CONTEXT, "prod_legacy"))

        empty = TEST_ROOT / "legacy-empty"
        empty.mkdir()
        other = TenantContext(
            workspace_id="ws_import_other",
            role="owner",
            initiator_id="user_import_other",
        )
        first_empty = import_json_tree(TEST_CONTEXT, empty)
        other_empty = import_json_tree(other, empty)
        self.assertTrue(first_empty["ok"])
        self.assertTrue(other_empty["ok"])
        with platform_session() as session:
            matching_batches = session.scalar(
                select(func.count())
                .select_from(ImportBatchRecord)
                .where(ImportBatchRecord.source_hash == first_empty["source_hash"])
            )
        self.assertEqual(matching_batches, 2)

    def test_judge_is_pure_deterministic_and_never_promotes_insufficient_data(self) -> None:
        snapshot = {
            "snapshot_hash": "a" * 64,
            "experiment": {"config": {
                "primary_metric": "roas", "roas_threshold": "2.0", "alpha": "0.05",
                "bootstrap_samples": 500, "min_days": 2, "min_impressions": 0,
                "min_purchases": "2",
            }},
            "entries": [
                {"id": "entry_good", "position": 0, "daily": [
                    {"spend": "10", "purchase_value": "35", "purchases": "2"},
                    {"spend": "12", "purchase_value": "40", "purchases": "2"},
                    {"spend": "8", "purchase_value": "28", "purchases": "1"},
                ]},
                {"id": "entry_sparse", "position": 1, "daily": [
                    {"spend": "1", "purchase_value": "100", "purchases": "1"},
                ]},
            ],
        }
        first = pure_judge(snapshot)
        second = pure_judge(snapshot)
        self.assertEqual(first, second)
        self.assertEqual(first["entries"][0]["verdict"], "win")
        self.assertEqual(first["entries"][1]["verdict"], "inconclusive")
        self.assertIn("insufficient_days", first["entries"][1]["reason_codes"])
        self.assertIn("insufficient_purchases", first["entries"][1]["reason_codes"])

        cpa_snapshot = {
            "snapshot_hash": "c" * 64,
            "experiment": {"config": {
                "primary_metric": "cpa", "cpa_threshold": "20", "alpha": "0.05",
                "bootstrap_samples": 500, "min_days": 2, "min_impressions": 0,
                "min_purchases": "1",
            }},
            "entries": [{"id": "entry_cpa", "position": 0, "daily": [
                {"spend": "10", "purchases": "1", "purchase_value": "30"},
                {"spend": "10", "purchases": "0", "purchase_value": "0"},
            ]}],
        }
        cpa = pure_judge(cpa_snapshot)["entries"][0]
        self.assertEqual(cpa["estimate"], "20.000000000000")
        self.assertIsNone(cpa["interval_high"])
        self.assertEqual(cpa["verdict"], "learn")

    def test_immutable_experiment_freezes_and_appends_replayable_judgements(self) -> None:
        product, angles, plan = self._seed_product_plan("experiment")
        run = manager.start(
            TEST_CONTEXT,
            product, angles, plan,
            GenRequest(product_id=product.id, provider="mock", variants=2),
        )
        asyncio.run(WorkerRuntime(owner="experiment-worker-1").run_once())
        asyncio.run(WorkerRuntime(owner="experiment-worker-2").run_once())
        with platform_session() as session:
            versions = session.scalars(
                select(AssetVersionRecord).join(AssetRecord).where(AssetRecord.product_id == product.id)
                .order_by(AssetVersionRecord.id)
            ).all()
        self.assertEqual(len(versions), 2)
        metric_hash = "b" * 64
        experiment_id = experiment_service.create_experiment(
            TEST_CONTEXT,
            product_id=product.id,
            name="CTR frozen round",
            metric_contract_hash=metric_hash,
            config_snapshot={
                "primary_metric": "ctr", "ctr_threshold": "0.04", "currency": "USD",
                "min_days": 1, "min_impressions": 500, "min_clicks": 0,
                "min_purchases": "0", "alpha": "0.05", "bootstrap_samples": 500,
                "require_fresh_data": False,
            },
            entries=[
                {"asset_version_id": versions[0].id, "marketing_vars": {"hook": "A"}},
                {"asset_version_id": versions[1].id, "marketing_vars": {"hook": "B"}},
            ],
        )
        frozen = experiment_service.freeze_experiment(TEST_CONTEXT, experiment_id, 1)
        self.assertEqual(frozen["state"], "frozen")
        self.assertEqual(frozen["revision"], 2)
        frozen_hash = frozen["frozen_snapshot_hash"]

        for entry, ad_id in zip(frozen["entries"], ("ad_win", "ad_lose")):
            experiment_service.bind_entry(TEST_CONTEXT, experiment_id, entry["id"], "act_experiment", ad_id)
        cutoff = utcnow().date() - timedelta(days=1)
        earlier = cutoff - timedelta(days=1)
        with platform_session() as session:
            account = MetaAccountRecord(
                id="mact_experiment", workspace_id="ws_test", ad_account_id="act_experiment",
                name="Experiment Account", currency="USD", timezone_name="UTC", api_version="v26.0",
            )
            session.add(account)
            sync = MetaSyncRunRecord(
                id="msync_experiment", workspace_id="ws_test", account_id=account.id, state="completed",
                date_start=earlier, date_stop=cutoff, contract_hash=metric_hash,
                contract_json={"test": True}, api_version="v26.0", account_currency="USD",
                account_timezone="UTC", idempotency_key="experiment-metrics",
            )
            session.add(sync)
            session.flush()
            for index, (ad_id, clicks) in enumerate((("ad_win", 70), ("ad_lose", 10))):
                for day in (earlier, cutoff):
                    session.add(AdDailyMetricRecord(
                        id=f"adm_exp_{index}_{day}", account_id=account.id, sync_run_id=sync.id,
                        ad_id=ad_id, metric_date=day, spend=Decimal("10"), impressions=1000,
                        clicks=clicks, reach=900, purchases=Decimal("2"), purchase_value=Decimal("30"),
                        currency="USD", contract_hash=metric_hash, raw_json={},
                    ))

        first = experiment_service.run_judgement(TEST_CONTEXT, experiment_id, earlier)
        replay = experiment_service.run_judgement(TEST_CONTEXT, experiment_id, earlier)
        second = experiment_service.run_judgement(TEST_CONTEXT, experiment_id, cutoff)
        self.assertEqual(first["id"], replay["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["result"]["summary"]["win"], 1)
        self.assertEqual(second["result"]["summary"]["lose"], 1)
        projected = store.get_verdicts(TEST_CONTEXT, product.id)
        self.assertEqual({value["verdict"] for value in projected.values()}, {"win", "lose"})
        self.assertTrue(all(value["context"]["source"] == "experiment_judge" for value in projected.values()))
        self.assertEqual(
            experiment_service.get_experiment(TEST_CONTEXT, experiment_id)["frozen_snapshot_hash"],
            frozen_hash,
        )
        closed = experiment_service.close_experiment(TEST_CONTEXT, experiment_id, 2)
        self.assertEqual(closed["state"], "closed")
        with platform_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(JudgementRecord)
                                            .where(JudgementRecord.experiment_id == experiment_id)), 2)
            self.assertEqual(session.scalar(select(func.count()).select_from(ExperimentDecisionRecord)
                                            .where(ExperimentDecisionRecord.experiment_id == experiment_id)), 4)
            self.assertEqual(session.scalar(select(func.count()).select_from(ExperimentStateEventRecord)
                                            .where(ExperimentStateEventRecord.experiment_id == experiment_id)), 3)
            closed_event = session.scalar(select(ExperimentStateEventRecord).where(
                ExperimentStateEventRecord.experiment_id == experiment_id,
                ExperimentStateEventRecord.to_state == "closed",
            ))
            self.assertEqual(closed_event.from_state, "frozen")

    def test_killed_safe_worker_resumes_without_duplicate_committed_output(self) -> None:
        product, angles, plan = self._seed_product_plan("kill_resume")
        run = manager.start(TEST_CONTEXT, product, angles, plan, GenRequest(product_id=product.id, provider="mock"))
        # This suite intentionally shares one durable queue. Earlier tests may
        # leave unrelated queued work behind, so make this subprocess exercise
        # the run created above instead of whichever historical job happens to
        # sort first.
        with platform_session() as session:
            target_job = session.scalar(
                select(JobRecord).where(
                    JobRecord.resource_type == "run",
                    JobRecord.resource_id == run.id,
                )
            )
            self.assertIsNotNone(target_job)
            session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id != target_job.id,
                    JobRecord.state.in_(("queued", "retry_wait")),
                )
                .values(next_attempt_at=utcnow() + timedelta(days=1))
            )
        repo_root = Path(__file__).resolve().parents[1]
        # Pytest imports every test module during collection, and some of those
        # modules intentionally replace the process environment.  The already
        # imported application still points at this module's isolated database,
        # so pin the child worker to the same resolved configuration explicitly.
        worker_env = os.environ.copy()
        worker_env["MARKETAI_DATABASE_URL"] = config.DATABASE_URL
        worker_env["MARKETAI_DATA_DIR"] = str(config.DATA_DIR)
        process = subprocess.Popen(
            [sys.executable, "-m", "worker.main"],
            cwd=repo_root,
            env=worker_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            running_seen = False
            while time.monotonic() < deadline:
                with platform_session() as session:
                    row = session.scalar(
                        select(JobRecord).where(
                            JobRecord.resource_type == "run",
                            JobRecord.resource_id == run.id,
                        )
                    )
                    running_seen = bool(row and row.state == "running")
                if running_seen:
                    break
                time.sleep(0.02)
            self.assertTrue(running_seen, "Worker 未进入 running 状态")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        time.sleep(1.2)

        resumed = subprocess.run(
            [sys.executable, "-m", "worker.main", "--once"],
            cwd=repo_root,
            env=worker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        persisted = store.get_run(TEST_CONTEXT, product.id, run.id)
        self.assertEqual(persisted.status, "done")
        self.assertEqual(persisted.done, 1)

    def test_launch_canary_requires_fresh_confirmation_and_recovers_each_paused_step(self) -> None:
        config.LAUNCH_CONFIRMATION_SECRET = "test-confirmation-secret"
        config.LAUNCH_MAX_DAILY_BUDGET_MINOR = 500
        product, angles, plan = self._seed_product_plan("launch")
        run = manager.start(TEST_CONTEXT, product, angles, plan, GenRequest(
            product_id=product.id, provider="mock", variants=2,
        ))
        asyncio.run(WorkerRuntime(owner="launch-gen-1").run_once())
        asyncio.run(WorkerRuntime(owner="launch-gen-2").run_once())
        with platform_session() as session:
            versions = session.scalars(
                select(AssetVersionRecord).join(AssetRecord).where(AssetRecord.product_id == product.id)
                .order_by(AssetVersionRecord.id)
            ).all()
            session.add(MetaAccountRecord(
                id="mact_launch", workspace_id="ws_test", ad_account_id="act_launch",
                name="Launch Test", currency="USD", timezone_name="UTC", api_version="v26.0",
            ))
        experiment_id = experiment_service.create_experiment(
            TEST_CONTEXT,
            product_id=product.id, name="Launch canary source", metric_contract_hash="d" * 64,
            config_snapshot={
                "primary_metric": "ctr", "ctr_threshold": "0.02", "currency": "USD",
                "alpha": "0.05", "bootstrap_samples": 500,
            },
            entries=[{"asset_version_id": version.id, "marketing_vars": {"hook": str(index)}}
                     for index, version in enumerate(versions)],
        )
        frozen = experiment_service.freeze_experiment(TEST_CONTEXT, experiment_id, 1)
        entry_id = frozen["entries"][0]["id"]
        with self.assertRaises(launch_service.LaunchError):
            launch_service.create_preview(
                TEST_CONTEXT,
                experiment_id=experiment_id, entry_id=entry_id, ad_account_id="act_launch",
                page_id="page_1", landing_page_url="https://example.com/p", message="Canary",
                headline="Canary", country="US", daily_budget_minor=501,
            )
        preview = launch_service.create_preview(
            TEST_CONTEXT,
            experiment_id=experiment_id, entry_id=entry_id, ad_account_id="act_launch",
            page_id="page_1", landing_page_url="https://example.com/p", message="Canary body",
            headline="Canary headline", country="US", daily_budget_minor=200,
        )
        self.assertEqual(preview["preview"]["delivery"]["campaign_status"], "PAUSED")
        self.assertEqual(preview["preview"]["delivery"]["adset_status"], "PAUSED")
        self.assertEqual(preview["preview"]["delivery"]["ad_status"], "PAUSED")
        duplicate_preview = launch_service.create_preview(
            TEST_CONTEXT,
            experiment_id=experiment_id, entry_id=entry_id, ad_account_id="act_launch",
            page_id="page_1", landing_page_url="https://example.com/p", message="Canary body",
            headline="Canary headline", country="US", daily_budget_minor=200,
        )
        self.assertNotEqual(duplicate_preview["id"], preview["id"])
        self.assertEqual(duplicate_preview["preview_hash"], preview["preview_hash"])
        with self.assertRaises(launch_service.LaunchConflict):
            launch_service.confirm_preview(TEST_CONTEXT, preview["id"], "0" * 64)
        confirmation = launch_service.confirm_preview(
            TEST_CONTEXT, preview["id"], preview["preview_hash"]
        )

        remote_state = {}
        fake = FakeMetaWriteAdapter(remote_state, crash_after_create_once="adset_created")
        operation_id = launch_service.start_operation(
            TEST_CONTEXT, preview["id"], confirmation["confirmation_token"], test_adapter=fake,
        )
        self.assertEqual(
            operation_id,
            launch_service.start_operation(
                TEST_CONTEXT, preview["id"], confirmation["confirmation_token"], test_adapter=fake,
            ),
        )
        runtime = WorkerRuntime(owner="launch-worker", launch_adapter=fake)
        self.assertTrue(asyncio.run(runtime.run_once()))
        failed_once = launch_service.get_operation(TEST_CONTEXT, operation_id)
        self.assertEqual(failed_once["state"], "retry_wait")
        with platform_session() as session:
            retry_job = session.scalar(select(JobRecord).where(
                JobRecord.resource_type == "launch_operation", JobRecord.resource_id == operation_id,
            ))
            retry_job.next_attempt_at = utcnow() - timedelta(seconds=1)
        self.assertTrue(asyncio.run(runtime.run_once()))
        completed = launch_service.get_operation(TEST_CONTEXT, operation_id)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(len(completed["steps"]), 5)
        adset_step = next(row for row in completed["steps"] if row["step"] == "adset_created")
        self.assertEqual(adset_step["attempt"], 2)
        self.assertEqual(len(remote_state), 5)
        delivery_objects = [item for item in remote_state.values()
                            if item["configured_status"] != "N/A"]
        self.assertTrue(delivery_objects)
        self.assertTrue(all(item["configured_status"] == "PAUSED" for item in delivery_objects))
        self.assertGreaterEqual(fake.calls.count("preflight"), 2)
        with platform_session() as session:
            binding = session.scalar(select(MetaAdBindingRecord).where(
                MetaAdBindingRecord.launch_operation_id == operation_id
            ))
            self.assertIsNotNone(binding)
            self.assertEqual(binding.campaign_id, next(
                row["remote_object_id"] for row in completed["steps"] if row["step"] == "campaign_created"
            ))
            self.assertEqual(binding.adset_id, next(
                row["remote_object_id"] for row in completed["steps"] if row["step"] == "adset_created"
            ))

        # Reissuing an identical preview is valid; its short-lived confirmation is independent.
        reissued = launch_service.create_preview(
            TEST_CONTEXT,
            experiment_id=experiment_id, entry_id=frozen["entries"][1]["id"], ad_account_id="act_launch",
            page_id="page_1", landing_page_url="https://example.com/p", message="Other",
            headline="Other", country="US", daily_budget_minor=200,
        )
        with platform_session() as session:
            row = session.get(LaunchPreviewRecord, reissued["id"])
            row.expires_at = utcnow() - timedelta(seconds=1)
        with self.assertRaises(launch_service.LaunchConflict):
            launch_service.confirm_preview(TEST_CONTEXT, reissued["id"], reissued["preview_hash"])

    def test_z_meta_read_sync_resumes_cursor_and_projects_daily_truth(self) -> None:
        config.META_API_VERSION = "v26.0"
        config.META_ACCESS_TOKEN = "test-access-token"
        config.META_APP_SECRET = "test-app-secret"
        config.META_AD_ACCOUNT_ID = "act_123456"
        config.META_CREDENTIAL_WORKSPACE_ID = TEST_CONTEXT.workspace_id
        config.META_SYNC_ENABLED = False
        config.META_PURCHASE_ACTION_TYPE = "omni_purchase"

        class FakeGateway:
            def __init__(self) -> None:
                self.calls = []
                self.fail_second_insight_page_once = True

            async def get(self, path, params):
                self.calls.append((path, dict(params)))
                self.assert_safe(params)
                if path == "act_123456":
                    return 200, {
                        "id": "act_123456", "name": "Test Account",
                        "currency": "USD", "timezone_name": "Asia/Hong_Kong",
                    }, {}
                if path.endswith("/insights"):
                    if not params.get("after"):
                        return 200, {
                            "data": [{
                                "ad_id": "ad_1", "ad_name": "MAI|asset_1", "adset_id": "set_1",
                                "adset_name": "Canary", "date_start": "2026-08-23", "date_stop": "2026-08-23",
                                "spend": "12.340000", "impressions": "1000", "reach": "800",
                                "inline_link_clicks": "25", "inline_link_click_ctr": "2.5",
                                "actions": [
                                    {"action_type": "omni_purchase", "value": "2"},
                                    {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "99"},
                                ],
                                "action_values": [{"action_type": "omni_purchase", "value": "48.50"}],
                                "purchase_roas": [{"action_type": "omni_purchase", "value": "3.9303"}],
                            }],
                            "paging": {
                                "cursors": {"after": "cursor-page-2"},
                                "next": "https://graph.facebook.com/next?after=cursor-page-2&access_token=must-not-persist",
                            },
                        }, {"x-app-usage": "{}"}
                    if self.fail_second_insight_page_once:
                        self.fail_second_insight_page_once = False
                        return 500, {"error": {"code": 2, "message": "temporary"}}, {}
                    return 200, {"data": [{
                        "ad_id": "ad_1", "ad_name": "MAI|asset_1", "adset_id": "set_1",
                        "adset_name": "Canary", "date_start": "2026-08-24", "date_stop": "2026-08-24",
                        "spend": "0", "impressions": "0", "reach": "0", "inline_link_clicks": "0",
                    }]}, {}
                if path.endswith("/ads"):
                    return 200, {"data": [{
                        "id": "ad_1", "name": "MAI|asset_1", "status": "PAUSED", "effective_status": "PAUSED",
                    }]}, {}
                if path.endswith("/adsets"):
                    return 200, {"data": [{
                        "id": "set_1", "name": "Canary", "status": "PAUSED", "effective_status": "PAUSED",
                    }]}, {}
                raise AssertionError(path)

            @staticmethod
            def assert_safe(params):
                assert "access_token" not in params
                assert len(params.get("appsecret_proof", "")) == 64

        gateway = FakeGateway()
        sync_id = meta_service.request_sync(
            TEST_CONTEXT,
            date.fromisoformat("2026-08-23"), date.fromisoformat("2026-08-24")
        )
        self.assertEqual(
            sync_id,
            meta_service.request_sync(
                TEST_CONTEXT, date.fromisoformat("2026-08-23"), date.fromisoformat("2026-08-24")
            ),
        )
        legacy_started = utcnow() - timedelta(days=10)
        with platform_session() as session:
            account = session.scalar(select(MetaAccountRecord).where(
                MetaAccountRecord.workspace_id == "ws_test",
                MetaAccountRecord.ad_account_id == "act_123456",
            ))
            account.observation_started_at = legacy_started
            account.observation_contract_hash = "legacy-contract"
        runtime = WorkerRuntime(owner="meta-test-worker", meta_client=MetaClient(gateway))

        # account succeeds; insights stores page 1 then receives a retryable error.
        self.assertTrue(asyncio.run(runtime.run_once()))
        self.assertTrue(asyncio.run(runtime.run_once()))
        with platform_session() as session:
            insight = session.scalar(select(MetaIngestionRecord).where(
                MetaIngestionRecord.sync_run_id == sync_id, MetaIngestionRecord.kind == "insights"
            ))
            self.assertEqual(insight.pages_fetched, 1)
            self.assertEqual(insight.next_after_cursor, "cursor-page-2")
            retry_job = session.scalar(select(JobRecord).where(
                JobRecord.resource_id == sync_id, JobRecord.state == "retry_wait"
            ))
            retry_job.next_attempt_at = utcnow() - timedelta(seconds=1)

        # The retry resumes at cursor-page-2; remaining status jobs then complete.
        for _ in range(3):
            self.assertTrue(asyncio.run(runtime.run_once()))

        status = meta_service.sync_status(TEST_CONTEXT, sync_id)
        self.assertEqual(status["sync"]["state"], "completed")
        self.assertEqual(status["account"]["currency"], "USD")
        self.assertIsNotNone(status["account"]["observation_started_at"])
        self.assertEqual(status["account"]["observation_contract_hash"], status["sync"]["contract_hash"])
        self.assertTrue(status["account"]["observation_contract_current"])
        observed_at = status["account"]["observation_started_at"]
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=legacy_started.tzinfo)
        self.assertGreater(observed_at, legacy_started)
        with platform_session() as session:
            metrics = session.scalars(
                select(AdDailyMetricRecord).where(AdDailyMetricRecord.sync_run_id == sync_id)
                .order_by(AdDailyMetricRecord.metric_date)
            ).all()
            self.assertEqual(len(metrics), 2)
            self.assertEqual(metrics[0].purchases, Decimal("2"))
            self.assertEqual(metrics[0].purchase_value, Decimal("48.5"))
            self.assertEqual(metrics[1].spend, Decimal("0"))
            self.assertEqual(session.scalar(select(func.count()).select_from(MetaEntityStatusEventRecord)), 2)

        insight_calls = [params for path, params in gateway.calls if path.endswith("/insights")]
        self.assertEqual(insight_calls[-1]["after"], "cursor-page-2")
        raw_text = "".join(
            gzip.open(path, "rt", encoding="utf-8").read()
            for path in config.META_RAW_DIR.rglob("*.json.gz")
        )
        self.assertNotIn("must-not-persist", raw_text)
        self.assertNotIn("test-access-token", raw_text)
        report = meta_service.reconcile(TEST_CONTEXT)
        self.assertIn("ad_1", report["issues"]["managed_ads_without_binding"])


if __name__ == "__main__":
    unittest.main()
