"""Tests for the entitlement API routes and worker dispatch fence.

Covers:
  - Inbox endpoint: valid envelope, wrong token, oversized body
  - Credential list endpoint
  - Spend guardrail get endpoint
  - Worker dispatch fence: legacy vs coordinated path selection
  - Cost summary and usage endpoints
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Use whatever database the app already has configured.
os.environ.setdefault("MARKETAI_LOCAL_IDENTITY_ENABLED", "true")
os.environ.setdefault("MARKETAI_LOCAL_WORKSPACE_ID", "ws_test")
os.environ.setdefault("MARKETAI_LOCAL_INITIATOR_ID", "test-owner")

import pytest  # noqa: E402

from app.platform.tenancy import TenantContext  # noqa: E402


SERVICE_TOKEN = "test-inbox-service-token-xyz"


def test_worker_only_import_resolves_provider_credential_foreign_keys():
    """The Worker entrypoint must register credential tables without API imports."""
    script = """
from app.worker_runtime import WorkerRuntime
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
)
for model in (ProviderCallAttemptRecord, ProviderCostLedgerEntryRecord):
    for constraint in model.__table__.foreign_key_constraints:
        _ = constraint.referred_table
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _init_db():
    """Run Alembic migrations once for the module."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


@pytest.fixture
def client(_init_db):
    """FastAPI TestClient with local identity enabled and service token set."""
    from fastapi.testclient import TestClient
    from app import config
    from app.platform.http_identity import request_tenant_context

    test_context = TenantContext(
        workspace_id="ws_test",
        role="owner",
        executor="test",
        initiator_id="test-owner",
        initiator_role="owner",
    )

    # Patch the service token into config at runtime so the inbox sees it
    original_token = config.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN
    config.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN = SERVICE_TOKEN
    try:
        from app.main import app
        app.dependency_overrides[request_tenant_context] = lambda: test_context
        with TestClient(app) as c:
            yield c
    finally:
        config.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN = original_token
        app.dependency_overrides.pop(request_tenant_context, None)


# ---------------------------------------------------------------------------
# Inbox endpoint tests
# ---------------------------------------------------------------------------

class TestInboxEndpoint:
    """POST /api/internal/domain/workspaces/{wid}/entitlement-inbox"""

    def test_inbox_endpoint_rejects_wrong_token(self, client):
        """Wrong Bearer token returns 403."""
        resp = client.post(
            "/api/internal/domain/workspaces/ws_test/entitlement-inbox",
            content=b'{"test": true}',
            headers={
                "Authorization": "Bearer wrong-token-value",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403

    def test_inbox_endpoint_rejects_missing_token(self, client):
        """Missing Bearer token returns 403."""
        resp = client.post(
            "/api/internal/domain/workspaces/ws_test/entitlement-inbox",
            content=b'{"test": true}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_inbox_endpoint_rejects_oversized_body(self, client):
        """Body > 64KB returns 413."""
        oversized = b"x" * 65537
        resp = client.post(
            "/api/internal/domain/workspaces/ws_test/entitlement-inbox",
            content=oversized,
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "Content-Type": "application/json",
                "Content-Length": str(len(oversized)),
            },
        )
        assert resp.status_code == 413

    def test_inbox_endpoint_applies_valid_envelope(self, client):
        """POST signed envelope returns 200 + "applied".

        The projection module requires a valid signature; we mock
        EntitlementProjectionModule.ingest at the lazy import point.
        """
        from app.entitlements.types import InboxOutcome

        mock_module_instance = MagicMock()
        mock_module_instance.ingest = AsyncMock(return_value=InboxOutcome.APPLIED)

        with patch(
            "app.entitlements.projection.EntitlementProjectionModule",
            return_value=mock_module_instance,
        ):
            envelope = json.dumps({
                "protected_header": {
                    "schema_version": 2,
                    "signature_algorithm": "hmac-sha256",
                    "signature_key_id": "k1",
                },
                "payload": {
                    "workspace_id": "ws_test",
                    "snapshot_id": "snap_1",
                    "entitlement_version": 1,
                    "lease_sequence": 1,
                    "projection_version": 1,
                    "semantic_hash": "abc123",
                },
                "signature": "fake_sig",
            }).encode()

            resp = client.post(
                "/api/internal/domain/workspaces/ws_test/entitlement-inbox",
                content=envelope,
                headers={
                    "Authorization": f"Bearer {SERVICE_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["outcome"] == "applied"
            mock_module_instance.ingest.assert_called_once()


# ---------------------------------------------------------------------------
# Credential list endpoint test
# ---------------------------------------------------------------------------

class TestCredentialList:
    """GET /api/v1/credentials"""

    def test_credential_list_returns_metadata(self, client):
        """GET /api/v1/credentials returns a list (mocked at module level)."""
        from datetime import datetime, timezone

        mock_metadata = MagicMock()
        mock_metadata.credential_version_id = "cv_test123"
        mock_metadata.workspace_id = "ws_test"
        mock_metadata.credential_key = "meta_api_key"
        mock_metadata.version_no = 1
        mock_metadata.provider = "meta"
        mock_metadata.display_label = None
        mock_metadata.state = "active"
        mock_metadata.rotated_at = None
        mock_metadata.value_sha256 = "abcdef"
        mock_metadata.created_by = "test-owner"
        mock_metadata.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mock_metadata.revoked_at = None

        mock_admin_instance = MagicMock()
        mock_admin_instance.list_metadata.return_value = [mock_metadata]
        mock_admin_instance.bindings_by_version.return_value = {}

        with patch(
            "app.entitlements.credential_admin.CredentialAdmin",
            return_value=mock_admin_instance,
        ):
            resp = client.get("/api/v1/credentials")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["credential_key"] == "meta_api_key"
            assert data[0]["credential_version_id"] == "cv_test123"


# ---------------------------------------------------------------------------
# Spend guardrail get endpoint test
# ---------------------------------------------------------------------------

class TestSpendGuardrail:
    """GET /api/v1/spend-guardrail"""

    def test_guardrail_get_returns_current(self, client):
        """GET /api/v1/spend-guardrail returns guardrail data."""
        from decimal import Decimal

        mock_data = {
            "workspace_id": "ws_test",
            "revision": 1,
            "budget_currency": "USD",
            "per_operation_limit": Decimal("100"),
            "rolling_24h_limit": Decimal("500"),
            "period_limit": Decimal("10000"),
            "outstanding_exposure_limit": Decimal("200"),
            "platform_policy_version": 1,
        }

        mock_admin_instance = MagicMock()
        mock_admin_instance.get.return_value = mock_data

        with patch(
            "app.entitlements.guardrail_admin.GuardrailAdmin",
            return_value=mock_admin_instance,
        ):
            resp = client.get("/api/v1/spend-guardrail")
            assert resp.status_code == 200
            data = resp.json()
            assert data["workspace_id"] == "ws_test"
            assert data["revision"] == 1
            assert data["per_operation_limit"] == "100"


# ---------------------------------------------------------------------------
# Worker dispatch fence tests
# ---------------------------------------------------------------------------

class TestWorkerDispatchFence:
    """Verify that the worker dispatch fence routes legacy vs coordinated jobs."""

    def test_legacy_job_uses_generate_with_retry(self):
        """execution_contract_version=0 (or absent) uses legacy path."""
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")

        mock_job = MagicMock()
        mock_job.payload = {
            "product_id": "p1",
            "run_id": "r1",
            "asset_id": "a1",
        }
        mock_job.id = "job_1"
        mock_job.context = TenantContext(
            workspace_id="ws_test",
            role="system",
            initiator_id="test-user",
            executor="system:worker",
        )

        with patch.object(runtime, "_generate_asset_legacy", new_callable=AsyncMock) as mock_legacy:
            asyncio.run(
                runtime._generate_asset(mock_job)
            )
            mock_legacy.assert_called_once_with(mock_job)

    def test_coordinated_job_uses_coordinator(self):
        """execution_contract_version >= 1 uses coordinated path."""
        from app.worker_runtime import WorkerRuntime

        mock_coordinator = MagicMock()
        runtime = WorkerRuntime(owner="test-worker", coordinator=mock_coordinator)

        mock_job = MagicMock()
        mock_job.payload = {
            "product_id": "p1",
            "run_id": "r1",
            "asset_id": "a1",
            "execution_contract_version": 1,
        }
        mock_job.id = "job_2"
        mock_job.context = TenantContext(
            workspace_id="ws_test",
            role="system",
            initiator_id="test-user",
            executor="system:worker",
        )

        with patch.object(runtime, "_generate_asset_coordinated", new_callable=AsyncMock) as mock_coord:
            asyncio.run(
                runtime._generate_asset(mock_job)
            )
            mock_coord.assert_called_once_with(mock_job)

    def test_coordinated_job_without_coordinator_fails(self):
        """Coordinated path without coordinator fails gracefully."""
        from app.worker_runtime import WorkerRuntime
        from app.platform import jobs
        from app import store

        runtime = WorkerRuntime(owner="test-worker", coordinator=None)

        mock_job = MagicMock()
        mock_job.payload = {
            "product_id": "p1",
            "run_id": "r1",
            "asset_id": "a1",
            "execution_contract_version": 2,
        }
        mock_job.id = "job_3"
        mock_job.context = TenantContext(
            workspace_id="ws_test",
            role="system",
            initiator_id="test-user",
            executor="system:worker",
        )

        with patch.object(store, "fail_asset") as mock_fail_asset, \
             patch.object(jobs, "fail") as mock_fail_job:
            asyncio.run(
                runtime._generate_asset(mock_job)
            )
            mock_fail_asset.assert_called_once()
            # Verify the error code is "coordinator_not_configured"
            call_kwargs = mock_fail_asset.call_args
            assert call_kwargs[1]["code"] == "coordinator_not_configured"
            mock_fail_job.assert_called_once()

    def test_run_once_routes_known_localization_job(self):
        from app.platform import jobs
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(type="localization.translate_copy")

        with (
            patch.object(jobs, "claim_next", return_value=mock_job),
            patch.object(
                runtime, "_localization_job", new_callable=AsyncMock
            ) as dispatch,
        ):
            assert asyncio.run(runtime.run_once()) is True

        dispatch.assert_awaited_once_with(mock_job)

    def test_run_once_routes_generation_qc_job(self):
        from app.platform import jobs
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(type="generation.qc_asset_version")

        with (
            patch.object(jobs, "claim_next", return_value=mock_job),
            patch.object(runtime, "_generation_qc_asset_version") as dispatch,
        ):
            assert asyncio.run(runtime.run_once()) is True

        dispatch.assert_called_once_with(mock_job)

    @pytest.mark.parametrize(
        ("job_type", "payload", "handler_name", "is_async"),
        [
            (
                "localization.analyze_source",
                {
                    "batch_id": "lb_1",
                    "asset_version_id": "av_1",
                    "analysis_recipe_hash": "recipe-1",
                },
                "handle_analyze_source",
                True,
            ),
            (
                "localization.translate_copy",
                {
                    "batch_id": "lb_1",
                    "asset_version_id": "av_1",
                    "target_market": "de-DE",
                    "inventory_id": "sci_1",
                    "claim_decision_set_id": "lcds_1",
                },
                "handle_translate_copy",
                True,
            ),
            (
                "localization.edit_attempt",
                {"batch_id": "lb_1", "attempt_number": 1},
                "handle_edit_attempt",
                True,
            ),
            (
                "localization.run_deterministic_qc",
                {"batch_id": "lb_1", "item_id": "li_1", "attempt_id": "la_1"},
                "handle_run_deterministic_qc",
                False,
            ),
            (
                "localization.ocr_check",
                {"batch_id": "lb_1", "attempt_number": 1},
                "handle_ocr_check",
                False,
            ),
            (
                "localization.visual_qc",
                {"batch_id": "lb_1", "attempt_number": 1},
                "handle_visual_qc",
                False,
            ),
            (
                "localization.finalize_item",
                {"batch_id": "lb_1", "item_id": "li_1", "attempt_id": "la_1"},
                "handle_finalize_item",
                False,
            ),
            (
                "localization.fold_batch",
                {"batch_id": "lb_1"},
                "handle_fold_batch",
                False,
            ),
        ],
    )
    def test_localization_dispatcher_reaches_every_pipeline_handler(
        self, job_type, payload, handler_name, is_async
    ):
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(
            id="job_loc_1",
            type=job_type,
            payload=payload,
            context=TenantContext(
                workspace_id="ws_test",
                role="system",
                initiator_id="test-user",
                executor="system:worker",
            ),
        )
        handler_patch = patch(
            f"app.localization.worker.{handler_name}",
            new_callable=AsyncMock if is_async else MagicMock,
        )

        with (
            patch(
                "app.worker_runtime.tenant_session",
                side_effect=lambda _context: nullcontext(MagicMock()),
            ),
            patch.object(
                runtime, "_localization_resource_id", return_value="li_1"
            ),
            patch.object(
                runtime, "_localization_attempt_id", return_value="la_1"
            ),
            handler_patch as handler,
        ):
            asyncio.run(runtime._dispatch_localization_job(mock_job))

        handler.assert_called_once()

    def test_unknown_localization_job_uses_existing_terminal_fence(self):
        from app.platform import jobs
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(
            id="job_unknown",
            type="localization.not_registered",
            context=TenantContext(
                workspace_id="ws_test",
                role="system",
                initiator_id="test-user",
                executor="system:worker",
            ),
        )

        with (
            patch.object(jobs, "claim_next", return_value=mock_job),
            patch.object(jobs, "fail") as fail,
        ):
            assert asyncio.run(runtime.run_once()) is True

        fail.assert_called_once_with(
            mock_job.context,
            mock_job.id,
            "test-worker",
            code="unknown_job_type",
            detail="未知任务类型: localization.not_registered",
            retryable=False,
        )

    def test_localization_handler_failure_enters_queue_retry(self):
        from app.platform import jobs
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(
            id="job_retry",
            type="localization.translate_copy",
            context=TenantContext(
                workspace_id="ws_test",
                role="system",
                initiator_id="test-user",
                executor="system:worker",
            ),
        )

        with (
            patch.object(jobs, "mark_running") as mark_running,
            patch.object(jobs, "succeed") as succeed,
            patch.object(jobs, "fail") as fail,
            patch.object(
                runtime,
                "_dispatch_localization_job",
                new_callable=AsyncMock,
                side_effect=RuntimeError("transient database error"),
            ),
        ):
            asyncio.run(runtime._localization_job(mock_job))

        mark_running.assert_called_once_with(
            mock_job.context, mock_job.id, "test-worker"
        )
        succeed.assert_not_called()
        # Routed through the outcome table as a `retry`, so the queue's own
        # attempt budget decides whether this becomes `retry_wait` or
        # `failed` -- the fence no longer makes that call itself.
        fail.assert_called_once_with(
            mock_job.context,
            mock_job.id,
            "test-worker",
            code="localization_job_failed",
            detail="transient database error",
            retryable=True,
            retry_after_seconds=None,
        )


    def test_attempt_scoped_qc_job_reads_its_ids_from_the_payload(self):
        """`authorize_isolated_qc` scopes its QC Jobs to the *Attempt*, so
        `resource_id` is an Attempt id. Passing that as `item_id` looks up
        an Item that does not exist and the whole isolated recovery
        silently never runs."""
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(
            id="job_iso_qc",
            type="localization.ocr_check",
            payload={
                "batch_id": "lb_1",
                "item_id": "li_1",
                "attempt_id": "la_1",
                "step": "ocr_check",
            },
            context=TenantContext(
                workspace_id="ws_test",
                role="system",
                initiator_id="test-user",
                executor="system:worker",
            ),
        )

        with (
            patch(
                "app.worker_runtime.tenant_session",
                side_effect=lambda _context: nullcontext(MagicMock()),
            ),
            patch.object(
                runtime,
                "_localization_resource_id",
                side_effect=AssertionError(
                    "resource_id must not be read for an Attempt-scoped Job"
                ),
            ),
            patch(
                "app.localization.worker.handle_ocr_check", new_callable=MagicMock
            ) as handler,
        ):
            asyncio.run(runtime._dispatch_localization_job(mock_job))

        handler.assert_called_once()
        kwargs = handler.call_args.kwargs
        assert kwargs["item_id"] == "li_1"
        assert kwargs["attempt_id"] == "la_1"
        assert kwargs["batch_id"] == "lb_1"

    def test_item_scoped_qc_job_still_falls_back_to_resource_id(self):
        """The Jobs `confirm()` admits carry no `item_id` in their payload
        -- their `resource_id` is the Item, and that path must keep
        working."""
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(
            id="job_item_qc",
            type="localization.visual_qc",
            payload={"batch_id": "lb_1", "attempt_number": 1, "step": "visual_qc"},
            context=TenantContext(
                workspace_id="ws_test",
                role="system",
                initiator_id="test-user",
                executor="system:worker",
            ),
        )

        with (
            patch(
                "app.worker_runtime.tenant_session",
                side_effect=lambda _context: nullcontext(MagicMock()),
            ),
            patch.object(
                runtime, "_localization_resource_id", return_value="li_from_resource"
            ),
            patch.object(
                runtime, "_localization_attempt_id", return_value="la_derived"
            ),
            patch(
                "app.localization.worker.handle_visual_qc", new_callable=MagicMock
            ) as handler,
        ):
            asyncio.run(runtime._dispatch_localization_job(mock_job))

        kwargs = handler.call_args.kwargs
        assert kwargs["item_id"] == "li_from_resource"
        assert kwargs["attempt_id"] == "la_derived"

    def test_handler_outcome_signal_settles_the_job_by_its_outcome(self):
        """A coordinator result carrying `success=False` used to leave a
        `succeeded` Job, so its Operation aggregated to `succeeded` for a
        call that produced nothing."""
        from app.platform import jobs
        from app.platform.job_outcomes import JobFailed, JobUncertain
        from app.worker_runtime import WorkerRuntime

        context = TenantContext(
            workspace_id="ws_test",
            role="system",
            initiator_id="test-user",
            executor="system:worker",
        )

        for signal, expected in (
            (
                JobUncertain("timeout", "no response before deadline"),
                "uncertain",
            ),
            (JobFailed("provider_rejected", "controlled failure"), "failed"),
        ):
            runtime = WorkerRuntime(owner="test-worker")
            mock_job = MagicMock(
                id="job_signal",
                type="localization.analyze_source",
                context=context,
            )
            with (
                patch.object(jobs, "mark_running"),
                patch.object(jobs, "succeed") as succeed,
                patch.object(jobs, "fail", return_value="failed") as fail,
                patch.object(jobs, "mark_uncertain") as mark_uncertain,
                patch.object(
                    runtime,
                    "_dispatch_localization_job",
                    new_callable=AsyncMock,
                    side_effect=signal,
                ),
            ):
                asyncio.run(runtime._localization_job(mock_job))

            succeed.assert_not_called()
            if expected == "uncertain":
                mark_uncertain.assert_called_once()
                fail.assert_not_called()
            else:
                fail.assert_called_once()
                assert fail.call_args.kwargs["retryable"] is False
                mark_uncertain.assert_not_called()

    def test_obsolete_signal_closes_the_job_as_succeeded(self):
        from app.platform import jobs
        from app.platform.job_outcomes import JobObsolete
        from app.worker_runtime import WorkerRuntime

        runtime = WorkerRuntime(owner="test-worker")
        mock_job = MagicMock(
            id="job_obsolete",
            type="localization.edit_attempt",
            context=TenantContext(
                workspace_id="ws_test",
                role="system",
                initiator_id="test-user",
                executor="system:worker",
            ),
        )

        with (
            patch.object(jobs, "mark_running"),
            patch.object(jobs, "succeed") as succeed,
            patch.object(jobs, "fail") as fail,
            patch.object(jobs, "record_obsolete") as record_obsolete,
            patch.object(
                runtime,
                "_dispatch_localization_job",
                new_callable=AsyncMock,
                side_effect=JobObsolete("batch_terminal", "cancelled"),
            ),
        ):
            asyncio.run(runtime._localization_job(mock_job))

        record_obsolete.assert_called_once()
        succeed.assert_called_once()
        fail.assert_not_called()


# ---------------------------------------------------------------------------
# Cost summary endpoint test
# ---------------------------------------------------------------------------

class TestCostSummary:
    """GET /api/v1/cost-summary"""

    def test_cost_summary_returns_empty(self, client):
        """Cost summary with no data returns empty entries."""
        resp = client.get("/api/v1/cost-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["workspace_id"] == "ws_test"
        assert isinstance(data["entries"], list)


# ---------------------------------------------------------------------------
# Usage endpoint test
# ---------------------------------------------------------------------------

class TestUsage:
    """GET /api/v1/usage"""

    def test_usage_returns_aggregate(self, client):
        """Usage endpoint returns aggregated data (mocked).

        This only covers the response shape. ``tests/test_usage_pg.py`` runs
        the real query as the API role, which this mock hid (#347).
        """
        from decimal import Decimal

        mock_query_instance = MagicMock()
        mock_query_instance.aggregate.return_value = Decimal("42")

        with patch(
            "app.entitlements.usage.UsageQuery",
            return_value=mock_query_instance,
        ):
            resp = client.get("/api/v1/usage?metric_key=provider_call")
            assert resp.status_code == 200
            data = resp.json()
            assert data["metric_key"] == "provider_call"
            assert data["total"] == "42"
