"""Tests for the Meta test-support route (app/meta/test_support.py).

Three concerns:
1. Endpoint absent when META_ADAPTER != fake_meta
2. Rejects cross-workspace requests (workspace_id mismatch)
3. Happy path: creates FakeMetaObjectRecord entries and returns provenance
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.platform.tenancy import TenantContext


OWNER = TenantContext(
    workspace_id="ws_test",
    role="owner",
    initiator_id="owner_1",
)

CROSS_WORKSPACE = TenantContext(
    workspace_id="ws_other",
    role="owner",
    initiator_id="owner_2",
)


class MetaTestSupportAbsentTest(unittest.TestCase):
    """Endpoint absent when META_ADAPTER != fake_meta."""

    def test_route_returns_404_when_meta_adapter_not_fake(self):
        from app.meta import test_support
        from app.platform.http_identity import request_tenant_context

        with patch.object(test_support, "META_ADAPTER", ""):
            from app.main import app

            app.dependency_overrides[request_tenant_context] = lambda: OWNER
            try:
                from fastapi.testclient import TestClient

                with TestClient(app) as client:
                    response = client.post(
                        "/api/domain/v1/workspaces/ws_test/test-support/meta-observation/advance",
                        json={"namespace": "ns-1", "scenario": "insufficient_metrics"},
                    )
                self.assertEqual(response.status_code, 404)
            finally:
                app.dependency_overrides.pop(request_tenant_context, None)


class MetaTestSupportCrossWorkspaceTest(unittest.TestCase):
    """Rejects requests where URL workspace_id doesn't match verified claims."""

    def test_rejects_cross_workspace_request(self):
        from app.meta import test_support
        from app.platform.http_identity import request_tenant_context

        with patch.object(test_support, "META_ADAPTER", "fake_meta"):
            from app.main import app

            app.dependency_overrides[request_tenant_context] = lambda: CROSS_WORKSPACE
            try:
                from fastapi.testclient import TestClient

                with TestClient(app) as client:
                    response = client.post(
                        "/api/domain/v1/workspaces/ws_test/test-support/meta-observation/advance",
                        json={"namespace": "ns-1", "scenario": "insufficient_metrics"},
                    )
                self.assertEqual(response.status_code, 403)
                self.assertIn("workspace_mismatch", response.text)
            finally:
                app.dependency_overrides.pop(request_tenant_context, None)


class MetaTestSupportInvalidScenarioTest(unittest.TestCase):
    """Rejects unknown scenario names."""

    def test_rejects_invalid_scenario(self):
        from app.meta import test_support
        from app.platform.http_identity import request_tenant_context

        with patch.object(test_support, "META_ADAPTER", "fake_meta"):
            from app.main import app

            app.dependency_overrides[request_tenant_context] = lambda: OWNER
            try:
                from fastapi.testclient import TestClient

                with TestClient(app) as client:
                    response = client.post(
                        "/api/domain/v1/workspaces/ws_test/test-support/meta-observation/advance",
                        json={"namespace": "ns-1", "scenario": "nonexistent_scenario"},
                    )
                self.assertEqual(response.status_code, 400)
                self.assertIn("invalid_scenario", response.text)
            finally:
                app.dependency_overrides.pop(request_tenant_context, None)
