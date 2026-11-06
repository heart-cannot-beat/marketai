"""DataSource CredentialVersion binding: bind_credential / get_active_binding.

Spec §4.3 -- Task 6.
"""
import unittest

from tests.support.library_fixtures import LibraryFixtures
from app.library.contracts import (
    ENTRY_DATA_SOURCE, ROLE_OWNER, ROLE_MEMBER,
)
from app.library.datasources import bind_credential, get_active_binding
from app.library.service import LibraryForbidden


class TestCredentialBinding(LibraryFixtures):

    def _publish_data_source(self, ctx):
        content = {
            "adapter_kind": "manual",
            "capabilities": ["price"],
            "market_context": ["US"],
            "config": {},
            "config_schema_version": "manual/1.0",
            "execution_mode": "real",
        }
        owner = self.owner_ctx(ctx)
        return self._publish(owner, ENTRY_DATA_SOURCE, "Test DS", content)

    def _create_credential(self, ctx):
        """Stub: create a minimal credential_versions row for FK satisfaction.

        credential_versions is owned by app.entitlements.credentials
        (CredentialVersionRecord, #18) -- envelope-encryption columns are
        NOT NULL there, so every one of them must be supplied even though
        this test never reads the ciphertext back.
        """
        from app.adapters.persistence.database import tenant_session
        from app.adapters.persistence.models import new_id, utcnow
        cred_id = new_id("cred")
        with tenant_session(ctx) as session:
            from sqlalchemy import text
            session.execute(text(
                "INSERT INTO credential_versions ("
                "id, workspace_id, credential_key, version_no, provider, state, "
                "encrypted_secret, encrypted_data_key, encryption_key_id, "
                "encryption_algorithm, nonce, aad_schema_version, created_by, created_at"
                ") VALUES ("
                ":id, :ws, :credential_key, 1, 'meta', 'active', "
                ":secret, :data_key, 'test-key', 'aes-256-gcm', :nonce, 1, 'test', :now"
                ")"
            ), {
                "id": cred_id, "ws": ctx.workspace_id, "credential_key": cred_id,
                "secret": b"", "data_key": b"", "nonce": b"",
                "now": utcnow(),
            })
        return cred_id

    def test_bind_creates_active_binding(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx)
        cred_id = self._create_credential(ctx)
        result = bind_credential(
            ctx, ds_entry_id, cred_id,
            idempotency_key="bind-1", request_fingerprint="fp1",
        )
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["credential_version_id"], cred_id)

    def test_rebind_supersedes_previous(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx)
        cred1 = self._create_credential(ctx)
        cred2 = self._create_credential(ctx)
        r1 = bind_credential(ctx, ds_entry_id, cred1,
                             idempotency_key="bind-2a", request_fingerprint="fp2a")
        r2 = bind_credential(ctx, ds_entry_id, cred2,
                             idempotency_key="bind-2b", request_fingerprint="fp2b")
        self.assertEqual(r1["revision"], 1)
        self.assertEqual(r2["revision"], 2)
        active = get_active_binding(ctx, ds_entry_id)
        self.assertEqual(active["credential_version_id"], cred2)
        self.assertEqual(active["revision"], 2)

    def test_member_cannot_bind(self):
        ctx_owner = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx_owner)
        cred_id = self._create_credential(ctx_owner)
        ctx_member = self.ctx(role=ROLE_MEMBER, workspace=ctx_owner.workspace_id, provision=False)
        with self.assertRaises(LibraryForbidden):
            bind_credential(ctx_member, ds_entry_id, cred_id,
                            idempotency_key="bind-3", request_fingerprint="fp3")

    def test_cross_workspace_blocked(self):
        ctx_a = self.ctx(role=ROLE_OWNER)
        ctx_b = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx_a)
        cred_id = self._create_credential(ctx_b)
        with self.assertRaises(Exception):
            bind_credential(ctx_b, ds_entry_id, cred_id,
                            idempotency_key="bind-4", request_fingerprint="fp4")

    def test_no_active_binding_returns_none(self):
        ctx = self.ctx(role=ROLE_OWNER)
        ds_entry_id, _ = self._publish_data_source(ctx)
        self.assertIsNone(get_active_binding(ctx, ds_entry_id))


if __name__ == "__main__":
    unittest.main()
