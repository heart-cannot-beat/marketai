"""HTTP-level tests for /api/v1/generation routes (Issue #21 Task 2; moved to
v1 paths in #27 Task 9 when the legacy /api/generation router was removed).

Exercises the real FastAPI app via ``TestClient`` with ``request_tenant_context``
overridden to a fixed ``TenantContext`` -- the same pattern
``tests/test_upload_intents.py`` uses -- so requests hit the actual route
bodies, ``tenant_session``, and the RFC 9457 problem-handler wiring
registered in ``app/main.py`` (``register_generation_problem_handlers``,
still shared by the v1 sub-router -- see ``app/generation/v1_router.py``'s
module docstring), rather than calling the domain functions directly the way
``test_generation_draft.py``/``test_generation_preview.py``/
``test_generation_submission.py`` do.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.adapters.persistence.models import new_id
from app.main import app
from app.platform.http_identity import request_tenant_context
from tests.support.library_fixtures import LibraryFixtures


class TestGenerationAPI(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _create_draft(self, **overrides) -> str:
        body = {
            "product_id": self._product_id(self.ctx),
            "provider": "mock",
            "model": "mock-v1",
            "execution_mode": "mock",
            "combination_strategy": {"kind": "full", "planner_version": "v1"},
        }
        body.update(overrides)
        response = self.client.post(
            "/api/v1/generation/drafts",
            json=body,
            headers={"Idempotency-Key": new_id("ik")},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_create_draft_route_returns_201(self):
        response = self.client.post(
            "/api/v1/generation/drafts",
            json={
                "product_id": self._product_id(self.ctx),
                "provider": "mock",
                "model": "mock-v1",
                "execution_mode": "mock",
                "combination_strategy": {"kind": "full", "planner_version": "v1"},
            },
            headers={"Idempotency-Key": new_id("ik")},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("id", body)
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["state"], "editing")

    def test_create_preview_route_unknown_draft_returns_404_problem(self):
        response = self.client.post(
            "/api/v1/generation/drafts/gd_does_not_exist/previews",
            headers={"Idempotency-Key": new_id("ik"), "If-Match": '"1"'},
        )
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["code"], "draft_not_found")
        self.assertIn("application/problem+json", response.headers["content-type"])

    def test_submit_route_with_mismatched_preview_hash_returns_422_problem(self):
        draft_id = self._create_draft()
        preview_response = self.client.post(
            f"/api/v1/generation/drafts/{draft_id}/previews",
            headers={"Idempotency-Key": new_id("ik"), "If-Match": '"1"'},
        )
        self.assertEqual(preview_response.status_code, 201)
        preview_id = preview_response.json()["id"]

        response = self.client.post(
            "/api/v1/generation/runs",
            json={
                "preview_id": preview_id,
                "preview_hash": "not-the-real-hash",
            },
            headers={"Idempotency-Key": new_id("ik"), "If-Match": '"1"'},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "preview_hash_mismatch")
        self.assertIn("application/problem+json", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
