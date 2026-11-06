# tests/test_meta_platform_api.py
"""Platform administration route tests (Task 17, spec §8.2, §8.7).

Flat in ``tests/`` and using the real ``app.main.app`` with ``TestClient``
and the shared migrated engine -- mirroring ``tests/test_meta_api_routes.py``
-- since no ``platform_client``/``platform_auth_headers``/``client``/
``auth_headers`` fixtures exist in ``tests/conftest.py`` (the brief's own
Step 1 snippet assumes fixtures that do not exist in this codebase, per the
task-17 brief's corrections).

Success-path tests override ``app.meta.platform_api.request_platform_context``
with a fixed ``LegacyPlatformContext``, the same dependency-override pattern
``test_meta_api_routes.py`` uses for ``request_tenant_context``. The auth
guard's own tests instead exercise the real dependency end to end, driving
``app.config.PLATFORM_ADMIN_TOKEN`` directly (restored via ``addCleanup``).
Task 4 made ``request_platform_context`` JWT-primary with the static bearer
token as a deprecated fallback (``app.platform.platform_operator_identity.
PlatformOperatorJWTVerifier``, Task 3); these guard tests still exercise only
the static-token fallback path, since ``app.state.platform_operator_jwt_verifier``
is ``None`` by default in this test process (no ``PLATFORM_OPERATOR_JWKS_URL``
configured) -- so the "no mechanism configured" 503, "missing/malformed
Authorization header" 401, and "token satisfies neither mechanism" 401 cases
all still exercise real code. The old "wrong token" case now returns 401
(not 403): ``request_platform_context`` no longer distinguishes "no token"
from "wrong token" now that a real JWT validator exists on this surface,
matching ``require_platform_operator_jwt``'s all-token-errors-are-401
convention. JWT-path behavior itself is covered by
``tests/test_platform_operator_identity.py``'s PO16-PO23.
"""
from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app import config
from app.adapters.persistence.database import engine, platform_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.credentials import CredentialVersionRecord
from app.main import app
from app.meta.platform_api import LegacyPlatformContext, request_platform_context


def _migrate_to_head() -> None:
    db_file = engine.url.database
    if db_file and db_file != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


class PlatformApiTestCase(unittest.TestCase):
    """Base: shared migration, a fresh Workspace id per test, override wired."""

    @classmethod
    def setUpClass(cls) -> None:
        _migrate_to_head()

    def setUp(self) -> None:
        self.workspace_id = f"ws_platform_api_{uuid.uuid4().hex[:12]}"
        app.dependency_overrides[request_platform_context] = (
            lambda: LegacyPlatformContext(
                actor_id="platform:test-operator", workspace_id=None, auth_method="jwt",
            )
        )
        self.addCleanup(app.dependency_overrides.pop, request_platform_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # -- helpers ----------------------------------------------------------

    def _bootstrap(self, **overrides) -> dict:
        body = {
            "workspace_id": self.workspace_id,
            "meta_mode": "internal_validation",
            "ad_account_id": "act_internal",
            "access_token_material": "token_material_here",
            "app_secret_material": "secret_material_here",
        }
        body.update(overrides)
        resp = self.client.post("/api/platform/v1/meta/bootstrap", json=body)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class BootstrapTests(PlatformApiTestCase):
    def test_bootstrap_creates_credentials_and_connection(self):
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json={
                "workspace_id": self.workspace_id,
                "meta_mode": "internal_validation",
                "ad_account_id": "act_internal",
                "access_token_material": "token_material_here",
                "app_secret_material": "secret_material_here",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertIn("connection_id", data)
        self.assertIn("access_token_version_id", data)
        self.assertIn("app_secret_version_id", data)
        self.assertTrue(data["connection_id"].startswith("mcon_"))
        self.assertIsNotNone(data["access_token_version_id"])
        self.assertIsNotNone(data["app_secret_version_id"])

        # The Workspace was auto-created as a platform Workspace (required
        # for internal_validation connections, app.meta.connections.create_connection).
        with platform_session() as session:
            ws = session.get(WorkspaceRecord, self.workspace_id)
            self.assertIsNotNone(ws)
            self.assertTrue(ws.is_platform_workspace)

            cred = session.get(CredentialVersionRecord, data["access_token_version_id"])
            self.assertIsNotNone(cred)
            self.assertEqual(cred.credential_key, "meta_access_token")
            self.assertEqual(cred.state, "active")
            # The plaintext material is never stored verbatim -- only its
            # KMS-sealed envelope.
            self.assertNotEqual(cred.encrypted_secret, b"token_material_here")

    def test_bootstrap_sandbox_mode_requires_no_credentials(self):
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json={
                "workspace_id": self.workspace_id,
                "meta_mode": "customer_sandbox",
                "ad_account_id": "act_sandbox",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertIsNone(data["access_token_version_id"])
        self.assertIsNone(data["app_secret_version_id"])

        # customer_sandbox does not require a platform Workspace.
        with platform_session() as session:
            ws = session.get(WorkspaceRecord, self.workspace_id)
            self.assertFalse(ws.is_platform_workspace)

    def test_bootstrap_internal_validation_missing_material_rejected(self):
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json={
                "workspace_id": self.workspace_id,
                "meta_mode": "internal_validation",
                "ad_account_id": "act_internal",
                "access_token_material": "only_one",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_bootstrap_sandbox_with_credentials_rejected(self):
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json={
                "workspace_id": self.workspace_id,
                "meta_mode": "customer_sandbox",
                "ad_account_id": "act_sandbox",
                "access_token_material": "unexpected",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_bootstrap_invalid_meta_mode_rejected(self):
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json={
                "workspace_id": self.workspace_id,
                "meta_mode": "not_a_real_mode",
                "ad_account_id": "act_1",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_bootstrap_repeat_call_conflicts(self):
        """Bootstrap is not yet idempotent (see Task 17 report Concerns):
        a repeat call for the same Workspace/credential_key surfaces as 409,
        not a silent replay."""
        self._bootstrap()
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json={
                "workspace_id": self.workspace_id,
                "meta_mode": "internal_validation",
                "ad_account_id": "act_internal",
                "access_token_material": "token_material_here",
                "app_secret_material": "secret_material_here",
            },
        )
        self.assertEqual(resp.status_code, 409)


# ---------------------------------------------------------------------------
# Platform auth guard
# ---------------------------------------------------------------------------


class PlatformAuthGuardTests(unittest.TestCase):
    """Exercises the real ``request_platform_context`` dependency (no override)."""

    @classmethod
    def setUpClass(cls) -> None:
        _migrate_to_head()

    def setUp(self) -> None:
        self.workspace_id = f"ws_platform_guard_{uuid.uuid4().hex[:12]}"
        self._original_token = config.PLATFORM_ADMIN_TOKEN
        self.addCleanup(self._restore_token)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _restore_token(self) -> None:
        config.PLATFORM_ADMIN_TOKEN = self._original_token

    def _bootstrap_body(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "meta_mode": "customer_sandbox",
            "ad_account_id": "act_guard",
        }

    def test_unconfigured_token_refuses_with_503(self):
        config.PLATFORM_ADMIN_TOKEN = ""
        resp = self.client.post("/api/platform/v1/meta/bootstrap", json=self._bootstrap_body())
        self.assertEqual(resp.status_code, 503)

    def test_missing_bearer_header_rejected(self):
        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-platform-token"
        resp = self.client.post("/api/platform/v1/meta/bootstrap", json=self._bootstrap_body())
        self.assertEqual(resp.status_code, 401)

    def test_wrong_bearer_token_rejected(self):
        # 401, not the pre-Task-4 403: request_platform_context no longer
        # distinguishes "no token" from "wrong token" now that a real JWT
        # validator exists on this surface (matches
        # require_platform_operator_jwt's all-token-errors-are-401 convention).
        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-platform-token"
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json=self._bootstrap_body(),
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_correct_bearer_token_grants_access(self):
        config.PLATFORM_ADMIN_TOKEN = "s3cr3t-platform-token"
        resp = self.client.post(
            "/api/platform/v1/meta/bootstrap",
            json=self._bootstrap_body(),
            headers={"Authorization": "Bearer s3cr3t-platform-token"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)


# ---------------------------------------------------------------------------
# Platform-wide connection reads
# ---------------------------------------------------------------------------


class ConnectionReadTests(PlatformApiTestCase):
    def test_list_and_get_connection(self):
        created = self._bootstrap()
        connection_id = created["connection_id"]

        listed = self.client.get("/api/platform/v1/meta/connections")
        self.assertEqual(listed.status_code, 200)
        ids = [row["id"] for row in listed.json()["items"]]
        self.assertIn(connection_id, ids)

        got = self.client.get(f"/api/platform/v1/meta/connections/{connection_id}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["id"], connection_id)
        self.assertEqual(got.json()["workspace_id"], self.workspace_id)

    def test_get_connection_not_found(self):
        resp = self.client.get("/api/platform/v1/meta/connections/mcon_does_not_exist")
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


class ReconcileTests(PlatformApiTestCase):
    def test_reconcile_returns_connection_and_active_binding(self):
        created = self._bootstrap()
        connection_id = created["connection_id"]

        resp = self.client.post(f"/api/platform/v1/meta/connections/{connection_id}/reconcile")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["connection"]["id"], connection_id)
        self.assertIsNotNone(data["active_binding"])
        self.assertEqual(data["active_binding"]["meta_connection_id"], connection_id)
        self.assertEqual(
            data["active_binding"]["access_token_version_id"], created["access_token_version_id"],
        )

    def test_reconcile_unknown_connection_not_found(self):
        resp = self.client.post(
            "/api/platform/v1/meta/connections/mcon_does_not_exist/reconcile"
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Credential revocation
# ---------------------------------------------------------------------------


class CredentialRevocationTests(PlatformApiTestCase):
    def test_revoke_credential_marks_it_revoked(self):
        created = self._bootstrap()
        credential_version_id = created["access_token_version_id"]

        resp = self.client.post(
            f"/api/platform/v1/credentials/{credential_version_id}/revoke",
            json={"reason": "rotation drill"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])

        with platform_session() as session:
            cred = session.get(CredentialVersionRecord, credential_version_id)
            self.assertEqual(cred.state, "revoked")
            self.assertEqual(cred.revoke_reason, "rotation drill")

    def test_revoke_unknown_credential_not_found(self):
        resp = self.client.post(
            "/api/platform/v1/credentials/cred_does_not_exist/revoke",
            json={"reason": "n/a"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_revoke_twice_second_call_not_found(self):
        created = self._bootstrap()
        credential_version_id = created["access_token_version_id"]

        first = self.client.post(
            f"/api/platform/v1/credentials/{credential_version_id}/revoke",
            json={"reason": "first revoke"},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            f"/api/platform/v1/credentials/{credential_version_id}/revoke",
            json={"reason": "second revoke"},
        )
        self.assertEqual(second.status_code, 404)


if __name__ == "__main__":
    unittest.main()
