"""Usage of the Operations that run against Meta (Issue #146).

Launch, Launch recovery, Scale export and the Meta observation sync record
their mode only in Meta-mode tables. The transaction that settles an
Operation cannot read those (their RLS needs a Meta-mode binding), so the
path that settles one passes the Meta mode it ran in to
``transition_operation``:

| Meta mode               | ``operations`` / ``processing_seconds`` | In ``total`` |
|-------------------------|-----------------------------------------|--------------|
| ``controlled_mock``     | ``mock``                                | no           |
| ``customer_sandbox``    | ``not_applicable``                      | yes          |
| ``internal_validation`` | ``not_applicable``                      | yes          |

Here: the mapping and its guards; a Scale export run by the deployed Worker
in each mode; a Launch admitted through the API and provisioned (or failed)
by the deployed Worker; a scheduled observation sync, published and failed.
Launch recovery is covered in ``tests/test_launch_recovery.py``, the
idle-time folds in ``tests/test_legacy_operations.py``, and the PostgreSQL
roles in ``tests/test_usage_pg.py`` and ``tests/test_legacy_operations_pg.py``.
"""
from __future__ import annotations

import ast
import asyncio
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import OperationRecord
from app.entitlements import operation_usage
from app.entitlements.operation_usage import META_OPERATION_COMMAND_TYPES, operation_usage_mode
from app.entitlements.usage_models import UsageEventRecord
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationState, create_operation, transition_operation
from app.platform.tenancy import TenantContext
from app.scale.exports import create_export
from app.worker_runtime import WorkerRuntime
from tests import test_meta_api_routes as launch_routes
from tests import test_meta_observation_sync as observation
from tests import test_scale_exports as scale_exports
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

ROOT = Path(__file__).resolve().parents[1]
S = OperationState

#: Meta mode -> the bucket of the Operation that ran in it.
BUCKETS = {
    "controlled_mock": "mock",
    "customer_sandbox": "not_applicable",
    "internal_validation": "not_applicable",
}


def usage_of(operation_id: str) -> list[tuple[str, str]]:
    """``(metric_key, execution_mode)`` of one Operation's Usage rows."""
    with platform_session() as session:
        return sorted(
            (row.metric_key, row.execution_mode)
            for row in session.scalars(select(UsageEventRecord).where(
                UsageEventRecord.source_resource_type == "operation",
                UsageEventRecord.source_resource_id == operation_id,
            ))
        )


def counted_as(bucket: str) -> list[tuple[str, str]]:
    return [("operations", bucket), ("processing_seconds", bucket)]


class UsageReader:
    """``GET /api/v1/usage`` as a member of the Workspace."""

    def usage(self, workspace_id: str, metric: str = "operations") -> dict:
        member = TenantContext(workspace_id=workspace_id, role="member", initiator_id="usr_usage_reader")
        previous = app.dependency_overrides.get(request_tenant_context)
        app.dependency_overrides[request_tenant_context] = lambda: member
        client = TestClient(app, raise_server_exceptions=False)
        try:
            response = client.get("/api/v1/usage", params={"metric_key": metric})
        finally:
            client.close()
            if previous is None:
                app.dependency_overrides.pop(request_tenant_context, None)
            else:
                app.dependency_overrides[request_tenant_context] = previous
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_bucket_and_total(self, workspace_id: str, bucket: str) -> None:
        """One Operation counted in ``bucket``: in ``total`` unless it is Mock."""
        for metric in ("operations", "processing_seconds"):
            with self.subTest(metric=metric):
                body = self.usage(workspace_id, metric)
                counted = Decimal(body["by_execution_mode"][bucket])
                if metric == "operations":
                    self.assertEqual(counted, Decimal("1"))
                expected_total = Decimal("0") if bucket == "mock" else counted
                self.assertEqual(Decimal(body["total"]), expected_total, "Mock is never real use")


# ---------------------------------------------------------------------------
# The mapping, and the guards that keep every Meta Operation on it
# ---------------------------------------------------------------------------

class MetaModeMappingTests(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()

    def settle(self, command_type: str, **transition) -> str:
        with tenant_session(self.ctx) as session:
            operation_id = create_operation(session, command_type=command_type, context=self.ctx).id
        with tenant_session(self.ctx) as session:
            transition_operation(session, operation_id, S.RUNNING, reason="test", actor="tester")
            transition_operation(session, operation_id, S.SUCCEEDED, reason="test", actor="tester", **transition)
        return operation_id

    def test_each_meta_mode_counts_in_its_bucket(self):
        for meta_mode, bucket in BUCKETS.items():
            with self.subTest(meta_mode=meta_mode):
                self.assertEqual(usage_of(self.settle("scale_export", meta_mode=meta_mode)), counted_as(bucket))

    def test_a_meta_mode_passed_on_a_transition_that_settles_nothing_counts_nothing(self):
        with tenant_session(self.ctx) as session:
            operation_id = create_operation(session, command_type="scale_export", context=self.ctx).id
            transition_operation(session, operation_id, S.RUNNING, reason="test", actor="tester",
                                 meta_mode="controlled_mock")
        self.assertEqual(usage_of(operation_id), [])

    def test_a_recorded_provider_mode_still_outranks_a_controlled_mock(self):
        """byok > sandbox > mock holds when one side of it is the Meta mode."""
        cases = {(): "mock", ("mock",): "mock", ("sandbox",): "sandbox", ("byok", "mock"): "byok"}
        for recorded, expected in cases.items():
            with self.subTest(recorded=recorded):
                session = MagicMock()
                session.execute.return_value.scalars.return_value.all.return_value = list(recorded)
                self.assertEqual(operation_usage_mode(session, "ws", "op", meta_mode="controlled_mock"), expected)
                self.assertEqual(
                    operation_usage_mode(session, "ws", "op", meta_mode="customer_sandbox"),
                    expected if recorded else "not_applicable",
                )

    def test_a_meta_operation_settled_without_its_mode_is_logged_and_never_guessed_mock(self):
        with patch.object(operation_usage, "logger") as log:
            operation_id = self.settle("launch_provisioning")
        self.assertEqual(usage_of(operation_id), counted_as("not_applicable"))
        self.assertEqual(log.warning.call_args.args[0], "usage_operation_meta_mode_missing")
        self.assertEqual(log.warning.call_args.kwargs["extra"]["command_type"], "launch_provisioning")

    def test_any_other_operation_needs_no_meta_mode(self):
        with patch.object(operation_usage, "logger") as log:
            operation_id = self.settle("test.command")
        self.assertEqual(usage_of(operation_id), counted_as("not_applicable"))
        log.warning.assert_not_called()

    def test_an_unrecognised_meta_mode_is_logged_and_ignored(self):
        with patch.object(operation_usage, "logger") as log:
            operation_id = self.settle("scale_export", meta_mode="mock")
        self.assertEqual(usage_of(operation_id), counted_as("not_applicable"))
        self.assertEqual(log.warning.call_args.args[0], "usage_operation_meta_mode_unrecognized")

    def test_the_registry_names_every_operation_the_meta_modules_create(self):
        """A new kind of Meta Operation must be registered, or its mode is never passed."""
        created = set()
        for path in self._meta_modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            constants = {
                target.id: node.value.value
                for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                for target in node.targets if isinstance(target, ast.Name)
            }
            for call in self._calls(tree, {"create_operation", "create_or_replay_operation"}):
                command_type = next(k.value for k in call.keywords if k.arg == "command_type")
                if isinstance(command_type, ast.Name):
                    command_type = ast.Constant(constants[command_type.id])
                self.assertIsInstance(command_type, ast.Constant, f"{path}:{call.lineno}")
                created.add(command_type.value)
        self.assertEqual(created, META_OPERATION_COMMAND_TYPES)

    def test_every_settling_transition_in_the_meta_modules_passes_its_meta_mode(self):
        """Only ``running`` and ``uncertain`` (not terminal) may leave it out."""
        missing = []
        for path in self._meta_modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            for call in self._calls(tree, {"transition_operation"}):
                states = {node.attr for node in ast.walk(call) if isinstance(node, ast.Attribute)
                          and isinstance(node.value, ast.Name) and node.value.id == "OperationState"}
                if states <= {"RUNNING", "UNCERTAIN"} and states:
                    continue
                if not any(k.arg == "meta_mode" for k in call.keywords):
                    missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
        self.assertEqual(missing, [])

    @staticmethod
    def _meta_modules() -> list[Path]:
        return sorted(p for package in ("scale", "launch", "meta") for p in (ROOT / "app" / package).rglob("*.py"))

    @staticmethod
    def _calls(tree: ast.AST, names: set[str]) -> list[ast.Call]:
        return [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) in names or getattr(node.func, "attr", None) in names)
        ]


# ---------------------------------------------------------------------------
# Scale export, run by the deployed Worker
# ---------------------------------------------------------------------------

class ScaleExportUsageTests(UsageReader, LibraryFixtures):
    def export(self, meta_mode: str) -> tuple[str, str]:
        """Request one export in ``meta_mode`` and let the deployed Worker run it."""
        workspace_id = f"ws_usage_meta_export_{uuid.uuid4().hex[:12]}"
        self.provision_workspace(workspace_id)
        self.addCleanup(cancel_workspace_jobs, workspace_id)
        ctx = scale_exports._owner_context(workspace_id, meta_mode=meta_mode)
        scenario = "scn_ci_only_usage" if meta_mode == "controlled_mock" else "__no_scenario__"
        with tenant_session(ctx) as session:
            enrollment_id, episode_id = scale_exports._seed_enrollment_and_episode(
                session, workspace_id, meta_mode=meta_mode, scenario_scope_key=scenario,
            )
            result = create_export(
                session, ctx, enrollment_id=enrollment_id, episode_id=episode_id,
                export_kind="evidence_summary", idempotency_key=f"ci-only-usage-{uuid.uuid4().hex}",
            )
        quarantine_foreign_jobs(workspace_id)
        self.assertTrue(asyncio.run(WorkerRuntime(owner=f"ci-only-usage-{uuid.uuid4().hex[:6]}").run_once()))
        with tenant_session(ctx) as session:
            self.assertEqual(session.get(OperationRecord, result.operation_id).state, "succeeded")
        return workspace_id, result.operation_id

    def test_a_controlled_mock_export_is_mock_and_stays_out_of_total(self):
        workspace_id, operation_id = self.export("controlled_mock")
        self.assertEqual(usage_of(operation_id), counted_as("mock"))
        self.assert_bucket_and_total(workspace_id, "mock")

    def test_an_export_in_a_real_meta_mode_is_not_applicable_as_before(self):
        for meta_mode in ("customer_sandbox", "internal_validation"):
            with self.subTest(meta_mode=meta_mode):
                workspace_id, operation_id = self.export(meta_mode)
                self.assertEqual(usage_of(operation_id), counted_as("not_applicable"))
                self.assert_bucket_and_total(workspace_id, "not_applicable")


# ---------------------------------------------------------------------------
# Launch, admitted through the API and provisioned by the deployed Worker
# ---------------------------------------------------------------------------

class LaunchUsageTests(UsageReader, launch_routes.MetaApiRoutesTestCase):
    _frozen_experiment = launch_routes.LaunchPreviewRouteTests._frozen_experiment
    _preview_body = launch_routes.LaunchPreviewRouteTests._preview_body

    def launch(self, meta_mode: str) -> str:
        """``POST .../execute`` in ``meta_mode``; returns the Operation id. The Job is queued first."""
        ad_account = f"act_usage_{uuid.uuid4().hex[:8]}"
        connection = self.client.post("/api/v1/meta/connections",
                                      json={"ad_account_id": ad_account, "meta_mode": meta_mode})
        self.assertEqual(connection.status_code, 201, connection.text)
        binding = self.client.post(f"/api/v1/meta/connections/{connection.json()['id']}/bindings",
                                   json={"ad_account_id": ad_account, "meta_mode": meta_mode})
        self.assertEqual(binding.status_code, 201, binding.text)
        body = self._preview_body(connection.json()["id"], ad_account)
        body["meta_mode"] = meta_mode
        preview = self.client.post("/api/v1/launch/previews", json=body)
        self.assertEqual(preview.status_code, 201, preview.text)
        confirmation = self.client.post(
            f"/api/v1/launch/previews/{preview.json()['id']}/confirm",
            json={"meta_mode": meta_mode, "preview_hash": preview.json()["preview_hash"]},
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        accepted = self.client.post(
            f"/api/v1/launch/previews/{preview.json()['id']}/execute",
            json={"meta_mode": meta_mode, "confirmation_token": confirmation.json()["confirmation_token"]},
            headers={"Idempotency-Key": f"ci-only-usage-launch-{uuid.uuid4().hex}"},
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        quarantine_foreign_jobs(self.ctx.workspace_id)
        return accepted.json()["operation_id"]

    def run_worker(self) -> None:
        self.assertTrue(asyncio.run(WorkerRuntime(owner=f"ci-only-usage-{uuid.uuid4().hex[:6]}").run_once()))

    def state(self, operation_id: str) -> str:
        with platform_session() as session:
            return session.get(OperationRecord, operation_id).state

    def test_a_controlled_mock_launch_is_mock_and_stays_out_of_total(self):
        operation_id = self.launch("controlled_mock")
        self.run_worker()
        self.assertEqual(self.state(operation_id), "succeeded")
        self.assertEqual(usage_of(operation_id), counted_as("mock"))
        self.assert_bucket_and_total(self.ctx.workspace_id, "mock")

    def test_a_controlled_mock_launch_the_worker_fails_is_mock(self):
        operation_id = self.launch("controlled_mock")
        with patch("app.worker_runtime.execute_provisioning", side_effect=RuntimeError("ci-only saga fault")):
            self.run_worker()
        self.assertEqual(self.state(operation_id), "failed")
        self.assertEqual(usage_of(operation_id), counted_as("mock"))
        self.assert_bucket_and_total(self.ctx.workspace_id, "mock")

    def test_a_customer_sandbox_launch_is_not_applicable_as_before(self):
        operation_id = self.launch("customer_sandbox")
        self.run_worker()
        self.assertEqual(self.state(operation_id), "succeeded")
        self.assertEqual(usage_of(operation_id), counted_as("not_applicable"))
        self.assert_bucket_and_total(self.ctx.workspace_id, "not_applicable")


# ---------------------------------------------------------------------------
# The scheduled Meta observation sync
# ---------------------------------------------------------------------------

class ObservationSyncUsageTests(UsageReader, observation.ObservationSyncTestCase):
    def controlled_mock_generation(self, scenario_config: dict) -> str:
        """A controlled_mock contract generation, scheduled by hand (ADR 0009) and due now."""
        from app.meta.connections import create_connection
        from app.meta.models import ObservationContractGenerationRecord
        from app.meta.observation import ensure_observation_schedule

        owner = replace(self.owner, meta_mode="controlled_mock")
        with tenant_session(owner) as session:
            connection_id = create_connection(session, context=owner, ad_account_id=observation.AD_ACCOUNT,
                                              scenario_config=scenario_config).id
        response = self.client.post(f"/api/v1/meta/connections/{connection_id}/bindings",
                                    json={"ad_account_id": observation.AD_ACCOUNT, "meta_mode": "controlled_mock"})
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post("/api/v1/meta/contracts",
                                    json=observation.daily_contract_body(connection_id, "controlled_mock"))
        self.assertEqual(response.status_code, 201, response.text)
        generation_id = response.json()["id"]
        with tenant_session(observation.worker_context(self.workspace_id, "controlled_mock")) as session:
            ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord, generation_id))
        self._make_due(generation_id)
        return generation_id

    def operation(self) -> OperationRecord:
        operation, = self._rows(OperationRecord)
        self.assertEqual(operation.command_type, "meta.observation_sync")
        return operation

    def test_a_published_controlled_mock_sync_is_mock_and_stays_out_of_total(self):
        self.controlled_mock_generation({})
        self._drain()
        operation = self.operation()
        self.assertEqual(operation.state, "succeeded")
        self.assertEqual(usage_of(operation.id), counted_as("mock"))
        self.assert_bucket_and_total(self.workspace_id, "mock")

    def test_a_failed_controlled_mock_sync_is_mock(self):
        self.controlled_mock_generation(
            {"faults": {"rules": [{"operation": "read_insights", "effect": "read_error", "match_page": 1}]}})
        self._drain(release_retries=True)
        operation = self.operation()
        self.assertEqual(operation.state, "failed")
        self.assertEqual(usage_of(operation.id), counted_as("mock"))

    def test_a_customer_sandbox_sync_is_not_applicable_as_before(self):
        _, generation = self._sandbox(ads=1)
        self._make_due(generation["id"])
        self._drain()
        operation = self.operation()
        self.assertEqual(operation.state, "succeeded")
        self.assertEqual(usage_of(operation.id), counted_as("not_applicable"))
        self.assert_bucket_and_total(self.workspace_id, "not_applicable")

