"""Fail-closed tests for tenant-bound external capabilities."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import config
from app.launch import service as launch_service
from app.meta import service as meta_service
from app.platform.tenancy import TenantContext
from app.schemas import Product


OWNER = TenantContext(
    workspace_id="ws_alpha",
    role="owner",
    initiator_id="owner_alpha",
)
MEMBER = TenantContext(
    workspace_id="ws_alpha",
    role="member",
    initiator_id="member_alpha",
)


class TenantBoundExternalCapabilityTests(unittest.TestCase):
    def test_meta_status_rejects_a_workspace_not_bound_to_the_credentials(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        app.dependency_overrides[request_tenant_context] = lambda: OWNER
        try:
            with (
                patch.object(config, "META_ACCESS_TOKEN", "token"),
                patch.object(config, "META_APP_SECRET", "secret"),
                patch.object(config, "META_AD_ACCOUNT_ID", "act_global_secret"),
                patch.object(config, "META_API_VERSION", "v26.0"),
                patch.object(config, "META_CREDENTIAL_WORKSPACE_ID", "ws_beta"),
                TestClient(app) as client,
            ):
                response = client.get("/api/meta/status")
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "meta_not_configured")
        self.assertNotIn("act_global_secret", response.text)

    def test_meta_reconciliation_rejects_a_workspace_not_bound_to_the_credentials(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        app.dependency_overrides[request_tenant_context] = lambda: OWNER
        try:
            with (
                patch.object(config, "META_ACCESS_TOKEN", "token"),
                patch.object(config, "META_APP_SECRET", "secret"),
                patch.object(config, "META_AD_ACCOUNT_ID", "act_global_secret"),
                patch.object(config, "META_API_VERSION", "v26.0"),
                patch.object(config, "META_CREDENTIAL_WORKSPACE_ID", "ws_beta"),
                TestClient(app) as client,
            ):
                response = client.post("/api/meta/reconcile")
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("act_global_secret", response.text)

    def test_meta_credentials_cannot_project_into_an_unbound_workspace(self) -> None:
        with (
            patch.object(config, "META_ACCESS_TOKEN", "token"),
            patch.object(config, "META_APP_SECRET", "secret"),
            patch.object(config, "META_AD_ACCOUNT_ID", "act_123"),
            patch.object(config, "META_API_VERSION", "v26.0"),
            patch.object(config, "META_CREDENTIAL_WORKSPACE_ID", "ws_beta"),
            self.assertRaises(meta_service.MetaConfigurationError),
        ):
            meta_service.request_sync(OWNER)

    def test_member_cannot_admit_a_real_meta_launch(self) -> None:
        with self.assertRaises(launch_service.LaunchForbidden):
            launch_service.start_operation(MEMBER, "preview", "confirmation")

    def test_worker_refuses_real_launch_not_admitted_by_an_owner(self) -> None:
        with self.assertRaises(launch_service.LaunchForbidden):
            asyncio.run(
                launch_service.execute_operation(
                    MEMBER.for_system_execution(), "operation"
                )
            )

    def test_angle_route_preserves_configured_provider_behavior_with_tenant_context(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        product = Product(id="prod_paid_guard", name="Paid guard")
        paid_call = AsyncMock(return_value='''{
            "dimensions": [{
                "label": "钩子",
                "values": [
                    {"label": "痛点", "prompt_fragment": "show the problem"},
                    {"label": "收益", "prompt_fragment": "show the benefit"}
                ]
            }],
            "style_notes": "clean commercial photography"
        }''')
        app.dependency_overrides[request_tenant_context] = lambda: OWNER
        try:
            with (
                patch("app.main.store.get_product", return_value=product),
                patch("app.main.store.save_angles") as save_angles,
                patch.object(config, "resolve_llm", return_value=("openai", "paid-model")),
                patch("app.angle_engine.llm.chat", paid_call),
                TestClient(app) as client,
            ):
                response = client.post(f"/api/products/{product.id}/angles/generate")
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "llm")
        paid_call.assert_awaited_once()
        saved_context, saved_angles = save_angles.call_args.args
        self.assertEqual(saved_context, OWNER)
        self.assertEqual(saved_angles.product_id, product.id)
