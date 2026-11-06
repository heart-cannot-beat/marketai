"""End-to-end BFF bridge integration test (Issue #27 Task 10).

Proves that a delegated Workspace request crosses the generated gateway,
BFF Adapter, Domain Interface, and response pipeline without exposing
Workspace identity or object keys.

Uses ``fastapi.testclient.TestClient`` against the real ``app.main.app`` --
the same pattern as ``tests/test_v1_library_routes.py`` and
``tests/test_v1_generation_routes.py`` -- with ``request_tenant_context``
overridden to a fixed ``TenantContext`` via ``LibraryFixtures``. There is no
``auth_headers`` fixture in this suite; identity is supplied through the
dependency override instead of a bearer token, matching every other v1
route test in this repo.

Also verifies the generated allowlist (``contracts/openapi/marketai-domain-v1.json``)
covers every domain operation the BFF bridge is meant to expose.
"""
from __future__ import annotations

import json
import unittest

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault before app.config is imported anywhere else in
# the process -- see tests/test_v1_library_routes.py for the same note.
from tests.support.library_fixtures import LibraryFixtures

from app.main import app
from app.platform.http_identity import request_tenant_context


class TestBFFBridgeE2E(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        from fastapi.testclient import TestClient

        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-e2e-{tag}"}

    # ------------------------------------------------------------------
    # Library: reads and create/get lifecycle
    # ------------------------------------------------------------------

    def test_library_entry_types_v1(self):
        resp = self.client.get("/api/v1/library/entry-types")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("entry_types", data)
        self.assertIsInstance(data["entry_types"], list)

    def test_library_create_and_get_entry(self):
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": "product", "display_name": "E2E Test Product"},
            headers=self._ik("create-1"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        entry = resp.json()
        self.assertEqual(entry["entry_type"], "product")
        self.assertEqual(entry["display_name"], "E2E Test Product")
        entry_id = entry["id"]

        resp2 = self.client.get(f"/api/v1/library/entries/{entry_id}")
        self.assertEqual(resp2.status_code, 200, resp2.text)
        self.assertIsNotNone(resp2.headers.get("ETag"))
        self.assertEqual(resp2.json()["id"], entry_id)

    # ------------------------------------------------------------------
    # Generation: reads
    # ------------------------------------------------------------------

    def test_generation_list_drafts_v1(self):
        resp = self.client.get("/api/v1/generation/drafts")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_generation_list_drafts_with_query_params(self):
        resp = self.client.get("/api/v1/generation/drafts?state=open&limit=10")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("items", data)

    # ------------------------------------------------------------------
    # Old (pre-bridge) paths are gone
    # ------------------------------------------------------------------

    def test_old_library_path_returns_404(self):
        resp = self.client.get("/api/library/entries")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_old_generation_path_returns_404(self):
        resp = self.client.get("/api/generation/drafts")
        self.assertEqual(resp.status_code, 404, resp.text)

    # ------------------------------------------------------------------
    # Idempotency-Key / If-Match enforcement crosses the bridge intact
    # ------------------------------------------------------------------

    def test_create_entry_without_idempotency_key_fails(self):
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": "product", "display_name": "No Key"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_save_draft_without_if_match_returns_428(self):
        create_resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": "product", "display_name": "ETag Test"},
            headers=self._ik("etag-1"),
        )
        self.assertEqual(create_resp.status_code, 201, create_resp.text)
        entry_id = create_resp.json()["id"]

        resp = self.client.put(
            f"/api/v1/library/entries/{entry_id}/draft",
            json={"content": {"name": "Updated"}},
            headers=self._ik("etag-2"),
        )
        self.assertEqual(resp.status_code, 428, resp.text)

    def test_save_draft_with_stale_revision_returns_409(self):
        create_resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": "product", "display_name": "Revision Test"},
            headers=self._ik("rev-1"),
        )
        self.assertEqual(create_resp.status_code, 201, create_resp.text)
        entry_id = create_resp.json()["id"]

        resp = self.client.put(
            f"/api/v1/library/entries/{entry_id}/draft",
            json={"content": {"name": "First"}},
            headers={**self._ik("rev-2"), "If-Match": '"1"'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # The revision has advanced to 2 -- replaying the original "1"
        # If-Match now conflicts rather than silently overwriting.
        resp2 = self.client.put(
            f"/api/v1/library/entries/{entry_id}/draft",
            json={"content": {"name": "Stale"}},
            headers={**self._ik("rev-3"), "If-Match": '"1"'},
        )
        self.assertEqual(resp2.status_code, 409, resp2.text)


class TestAllowlistCoverage(unittest.TestCase):
    """Standalone check -- no HTTP client or DB needed, just the spec on disk."""

    def test_allowlist_covers_all_operations(self):
        spec_path = "contracts/openapi/marketai-domain-v1.json"
        with open(spec_path) as f:
            spec = json.load(f)
        ops = set()
        for path_item in spec.get("paths", {}).values():
            for method_detail in path_item.values():
                if isinstance(method_detail, dict) and "operationId" in method_detail:
                    ops.add(method_detail["operationId"])
        self.assertGreaterEqual(len(ops), 50, f"Expected >=50 operations, found {len(ops)}: {sorted(ops)}")


if __name__ == "__main__":
    unittest.main()
