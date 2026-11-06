"""HTTP-level tests for the Owner credential commands behind the Settings page.

``POST/GET /api/v1/credentials``, ``PUT .../bindings/{scope}`` and
``POST .../revoke`` are what ``ProviderCredentialsSection`` (web) drives
through the BFF proxy, so this exercises the real FastAPI app via
``TestClient`` with ``request_tenant_context`` overridden (the
``tests/test_generation_api.py`` pattern): statuses, the ``application/
problem+json`` shape the proxy forwards, and the Generation capabilities
the bindings unlock.
"""
from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.platform.http_identity import request_tenant_context
from tests.support.library_fixtures import LibraryFixtures


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class CredentialApiTests(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="owner_a"))
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # -- helpers ----------------------------------------------------------

    def _create(self, key: str = "openai_api_key", provider: str = "openai",
                plaintext: bytes = b"sk-proj-test", label: str | None = "Team key"):
        return self.client.post(
            "/api/v1/credentials",
            json={
                "credential_key": key,
                "provider": provider,
                "plaintext_b64": _b64(plaintext),
                "display_label": label,
            },
        )

    def _bind(self, version_id: str, provider: str = "openai", scope: str = "generation"):
        return self.client.put(
            f"/api/v1/credentials/{version_id}/bindings/{scope}",
            json={"provider": provider},
        )

    def _capabilities(self) -> dict:
        response = self.client.get("/api/v1/generation/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- lifecycle --------------------------------------------------------

    def test_create_returns_200_with_the_settings_page_metadata(self):
        response = self._create()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["credential_key"], "openai_api_key")
        self.assertEqual(body["provider"], "openai")
        self.assertEqual(body["display_label"], "Team key")
        self.assertEqual(body["state"], "active")
        self.assertEqual(body["version_no"], 1)
        self.assertIsNone(body["rotated_at"])
        self.assertIsNone(body["revoked_at"])
        self.assertTrue(body["credential_version_id"].startswith("cv_"))
        # Never the secret, in any encoding.
        self.assertNotIn("sk-proj-test", response.text)
        self.assertNotIn(_b64(b"sk-proj-test"), response.text)

        listed = self.client.get("/api/v1/credentials")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [(c["credential_key"], c["provider"], c["state"]) for c in listed.json()],
            [("openai_api_key", "openai", "active")],
        )

    def test_binding_for_generation_advertises_byok(self):
        before = self._capabilities()
        self.assertNotIn("byok", before["modes"])
        self.assertEqual(before["credential_readiness"], {})

        version_id = self._create().json()["credential_version_id"]
        bound = self._bind(version_id)
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["binding_scope"], "generation")
        self.assertEqual(bound.json()["credential_version_id"], version_id)

        after = self._capabilities()
        self.assertIn("byok", after["modes"])
        self.assertIn("openai", after["providers"])
        self.assertEqual(
            after["credential_readiness"]["openai"],
            {"has_active_credential": True, "credential_version_id": version_id},
        )

        # Re-binding the same version is idempotent for the caller.
        again = self._bind(version_id)
        self.assertEqual(again.status_code, 200, again.text)

    def test_revoking_the_bound_key_withdraws_byok(self):
        version_id = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(version_id).status_code, 200)

        revoked = self.client.post(
            f"/api/v1/credentials/{version_id}/revoke",
            json={"reason": "removed from settings"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json(), {"ok": True})

        self.assertEqual(self.client.get("/api/v1/credentials").json(), [])
        capabilities = self._capabilities()
        self.assertNotIn("byok", capabilities["modes"])
        self.assertEqual(
            capabilities["credential_readiness"]["openai"],
            {"has_active_credential": False, "credential_version_id": None},
        )

        # A second revoke finds no active version under that key.
        second = self.client.post(
            f"/api/v1/credentials/{version_id}/revoke",
            json={"reason": "again"},
        )
        self.assertEqual(second.status_code, 404)
        self.assertEqual(second.json()["code"], "credential_not_found")

    def test_a_removed_key_can_be_added_again(self):
        first = self._create().json()
        self.client.post(
            f"/api/v1/credentials/{first['credential_version_id']}/revoke",
            json={"reason": "removed from settings"},
        )
        again = self._create(plaintext=b"sk-proj-second", label="Second key")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["credential_key"], "openai_api_key")
        self.assertEqual(again.json()["version_no"], 2)
        self.assertEqual(again.json()["display_label"], "Second key")
        self.assertEqual(self._bind(again.json()["credential_version_id"]).status_code, 200)
        self.assertTrue(
            self._capabilities()["credential_readiness"]["openai"]["has_active_credential"]
        )

    def test_rotation_keeps_the_key_but_needs_a_fresh_binding(self):
        first = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(first).status_code, 200)

        rotated = self.client.post(
            "/api/v1/credentials/openai_api_key/rotate",
            json={"new_plaintext_b64": _b64(b"sk-proj-new")},
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        second = rotated.json()
        self.assertEqual(second["version_no"], 2)
        self.assertEqual(second["state"], "active")
        self.assertEqual(second["display_label"], "Team key")
        self.assertNotEqual(second["credential_version_id"], first)

        # The generation binding still points at the rotated version, so
        # byok is withdrawn until the new version is bound.
        self.assertFalse(
            self._capabilities()["credential_readiness"]["openai"]["has_active_credential"]
        )
        self.assertEqual(self._bind(second["credential_version_id"]).status_code, 200)
        self.assertEqual(
            self._capabilities()["credential_readiness"]["openai"]["credential_version_id"],
            second["credential_version_id"],
        )

    # -- refusals, as problem+json the BFF forwards -------------------------

    def _assert_problem(self, response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn("application/problem+json", response.headers["content-type"])
        self.assertEqual(response.json()["code"], code)

    def test_duplicate_key_is_a_409_problem(self):
        self.assertEqual(self._create().status_code, 200)
        self._assert_problem(self._create(), 409, "credential_key_active")

    def test_invalid_or_empty_plaintext_is_a_422_problem(self):
        response = self.client.post(
            "/api/v1/credentials",
            json={"credential_key": "openai_api_key", "provider": "openai",
                  "plaintext_b64": "not base64!"},
        )
        self._assert_problem(response, 422, "invalid_plaintext")
        self._assert_problem(self._create(plaintext=b"   "), 422, "invalid_plaintext")

    def test_binding_a_revoked_or_foreign_version_is_refused(self):
        version_id = self._create().json()["credential_version_id"]
        self.client.post(
            f"/api/v1/credentials/{version_id}/revoke", json={"reason": "gone"},
        )
        self._assert_problem(self._bind(version_id), 409, "credential_not_bindable")
        # A version this Workspace does not have is not found, as in revoke.
        self._assert_problem(self._bind("cv_missing"), 404, "credential_not_found")

        other = self._create(key="meta_token", provider="meta").json()
        self._assert_problem(
            self._bind(other["credential_version_id"], provider="openai"),
            409, "credential_not_bindable",
        )

    def test_members_cannot_see_or_change_credentials(self):
        member = self._make_context(self.ctx.workspace_id, role="member", actor="member_b")
        app.dependency_overrides[request_tenant_context] = lambda: member
        self._assert_problem(self.client.get("/api/v1/credentials"), 403, "owner_role_required")
        self._assert_problem(self._create(), 403, "owner_role_required")

    def test_three_image_providers_have_independent_metadata_rotation_and_bindings(self):
        """Dummy secrets only; no vendor request is made by these commands."""
        providers = {"openai":"openai_api_key", "gemini":"gemini_api_key", "seedream":"ark_api_key"}
        versions = {}
        for provider, key in providers.items():
            secret = f"fixture-{provider}-only".encode()
            created = self._create(key=key, provider=provider, plaintext=secret)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertNotIn(secret.decode(), created.text)
            self.assertNotIn(_b64(secret), created.text)
            versions[provider] = created.json()["credential_version_id"]
            self.assertEqual(self._bind(versions[provider], provider).status_code, 200)
        for provider, key in providers.items():
            with self.subTest(provider=provider):
                rotated = self.client.post(f"/api/v1/credentials/{key}/rotate", json={"new_plaintext_b64":_b64(b"fixture-rotated-only")})
                self.assertEqual(rotated.status_code, 200, rotated.text)
                next_id = rotated.json()["credential_version_id"]
                self.assertNotEqual(next_id, versions[provider])
                self.assertEqual(rotated.json()["provider"], provider)
                self.assertFalse(self._capabilities()["credential_readiness"][provider]["has_active_credential"])
                self.assertEqual(self._bind(next_id, provider).status_code, 200)
                versions[provider] = next_id
                readiness = self._capabilities()["credential_readiness"]
                for other, version in versions.items():
                    self.assertEqual(readiness[other], {"has_active_credential":True,"credential_version_id":version})
        for provider, version in versions.items():
            revoked = self.client.post(f"/api/v1/credentials/{version}/revoke", json={"reason":"isolated software acceptance"})
            self.assertEqual(revoked.status_code, 200, revoked.text)
            readiness = self._capabilities()["credential_readiness"]
            self.assertFalse(readiness[provider]["has_active_credential"])
        self.assertEqual(self.client.get("/api/v1/credentials").json(), [])

    def test_gemini_and_seedream_mutations_deny_members_foreign_workspaces_and_provider_mismatch(self):
        for provider, key in (("gemini","gemini_api_key"),("seedream","ark_api_key")):
            with self.subTest(provider=provider):
                owner = self.ctx
                version = self._create(key=key, provider=provider).json()["credential_version_id"]
                wrong = "seedream" if provider == "gemini" else "gemini"
                self._assert_problem(self._bind(version, wrong), 409, "credential_not_bindable")
                self.ctx = self._make_context(owner.workspace_id, role="member", actor="member_b")
                self._assert_problem(self._bind(version, provider), 403, "owner_role_required")
                self._assert_problem(self._create(key=key, provider=provider), 403, "owner_role_required")
                self._assert_problem(self.client.post(f"/api/v1/credentials/{key}/rotate",json={"new_plaintext_b64":_b64(b"fixture-only")}),403,"owner_role_required")
                self._assert_problem(self.client.post(f"/api/v1/credentials/{version}/revoke",json={"reason":"fixture-only"}),403,"owner_role_required")
                self.ctx = self.owner_ctx(self._make_context(role="owner", actor="other_owner"))
                self.assertEqual(self.client.get("/api/v1/credentials").json(), [])
                self._assert_problem(self._bind(version, provider), 404, "credential_not_found")
                self._assert_problem(self.client.post(f"/api/v1/credentials/{key}/rotate",json={"new_plaintext_b64":_b64(b"fixture-only")}),404,"credential_not_found")
                self._assert_problem(self.client.post(f"/api/v1/credentials/{version}/revoke",json={"reason":"fixture-only"}),404,"credential_not_found")
                self.ctx = owner


class CredentialBffPolicyTests(unittest.TestCase):
    """The BFF forwards each Settings credential command's JSON body.

    ``revoke_credential`` was declared with ``maxRequestBytes: 0`` while the
    Domain requires ``{"reason": ...}``, so the BFF refused every "Remove"
    with 413 ``request_body_too_large`` before it reached FastAPI.
    """

    def test_every_credential_command_lets_its_body_through(self):
        policy = json.loads(
            (Path(__file__).resolve().parent.parent
             / "contracts/openapi/marketai-domain-v1-policy.json").read_text()
        )
        for operation in ("create_credential", "rotate_credential",
                          "bind_credential", "revoke_credential"):
            with self.subTest(operation=operation):
                self.assertGreaterEqual(policy[operation]["maxRequestBytes"], 1024)


if __name__ == "__main__":
    unittest.main()
