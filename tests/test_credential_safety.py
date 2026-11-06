"""Credential safety and canary tests (Task 24).

Tests verify that the CredentialProtector and CredentialAdmin modules
correctly protect secrets: cross-workspace AAD rejection, unique nonces,
and canary absence from API responses.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    EncryptedEnvelope,
    LocalKmsAdapter,
)

CANARY_SECRET = b"CANARY-sk-DEADBEEF-credential-leak-detector"
MASTER_SECRET = b"test-master-key-must-be-32-bytes!"


class TestCrossWorkspaceAADRejection(unittest.TestCase):
    def test_cross_workspace_aad_fails_decryption(self):
        """Decrypting with a different workspace_id must fail."""
        kms = LocalKmsAdapter(MASTER_SECRET)
        ctx_a = CredentialEncryptionContext(
            workspace_id="ws_a",
            credential_version_id="cv_1",
            credential_key="openai_api_key",
            provider="openai",
            version_no=1,
        )
        ctx_b = CredentialEncryptionContext(
            workspace_id="ws_b",
            credential_version_id="cv_1",
            credential_key="openai_api_key",
            provider="openai",
            version_no=1,
        )

        envelope = kms.seal(CANARY_SECRET, ctx_a)

        with self.assertRaises(Exception):
            with kms.open(envelope, ctx_b):
                pass


class TestCrossProviderAADRejection(unittest.TestCase):
    def test_cross_provider_aad_fails_decryption(self):
        """Decrypting with a different provider must fail."""
        kms = LocalKmsAdapter(MASTER_SECRET)
        ctx_openai = CredentialEncryptionContext(
            workspace_id="ws_x",
            credential_version_id="cv_2",
            credential_key="api_key",
            provider="openai",
            version_no=1,
        )
        ctx_meta = CredentialEncryptionContext(
            workspace_id="ws_x",
            credential_version_id="cv_2",
            credential_key="api_key",
            provider="meta",
            version_no=1,
        )

        envelope = kms.seal(b"secret-value", ctx_openai)

        with self.assertRaises(Exception):
            with kms.open(envelope, ctx_meta):
                pass


class TestUniqueNonces(unittest.TestCase):
    def test_two_seals_produce_different_nonces(self):
        """Each seal operation must produce a unique nonce."""
        kms = LocalKmsAdapter(MASTER_SECRET)
        ctx = CredentialEncryptionContext(
            workspace_id="ws_nonce",
            credential_version_id="cv_3",
            credential_key="test_key",
            provider="gemini",
            version_no=1,
        )

        envelope1 = kms.seal(b"secret1", ctx)
        envelope2 = kms.seal(b"secret1", ctx)

        self.assertNotEqual(envelope1.nonce, envelope2.nonce)
        self.assertNotEqual(envelope1.encrypted_value, envelope2.encrypted_value)


class TestSealOpenRoundtrip(unittest.TestCase):
    def test_seal_then_open_recovers_plaintext(self):
        """Sealing and opening with the same context recovers the plaintext."""
        kms = LocalKmsAdapter(MASTER_SECRET)
        ctx = CredentialEncryptionContext(
            workspace_id="ws_rt",
            credential_version_id="cv_4",
            credential_key="api_key",
            provider="openai",
            version_no=1,
        )

        plaintext = b"sk-proj-ABCDEF1234567890"
        envelope = kms.seal(plaintext, ctx)

        with kms.open(envelope, ctx) as view:
            self.assertEqual(bytes(view), plaintext)


class TestPlaintextScopeZeroing(unittest.TestCase):
    def test_plaintext_buffer_zeroed_after_exit(self):
        """After exiting the PlaintextScope, the buffer is best-effort zeroed."""
        kms = LocalKmsAdapter(MASTER_SECRET)
        ctx = CredentialEncryptionContext(
            workspace_id="ws_zero",
            credential_version_id="cv_5",
            credential_key="api_key",
            provider="meta",
            version_no=1,
        )

        envelope = kms.seal(b"sensitive-data-here!", ctx)
        scope = kms.open(envelope, ctx)

        with scope as view:
            recovered = bytes(view)
            self.assertEqual(recovered, b"sensitive-data-here!")

        # After exit, the internal buffer should be zeroed
        self.assertTrue(all(b == 0 for b in scope._buf))


class TestCanaryAbsentFromCredentialListAPI(unittest.TestCase):
    def test_canary_not_in_api_response(self):
        """The canary secret must never appear in API list responses."""
        os.environ.setdefault("MARKETAI_LOCAL_IDENTITY_ENABLED", "true")
        os.environ.setdefault("MARKETAI_LOCAL_WORKSPACE_ID", "ws_canary")
        os.environ.setdefault("MARKETAI_LOCAL_INITIATOR_ID", "test-owner")

        from datetime import datetime, timezone
        from fastapi.testclient import TestClient
        from app.platform.tenancy import TenantContext
        from app.platform.http_identity import request_tenant_context

        test_context = TenantContext(
            workspace_id="ws_canary",
            role="owner",
            executor="test",
            initiator_id="test-owner",
            initiator_role="owner",
        )

        mock_metadata = MagicMock()
        mock_metadata.credential_version_id = "cv_canary"
        mock_metadata.workspace_id = "ws_canary"
        mock_metadata.credential_key = "openai_api_key"
        mock_metadata.version_no = 1
        mock_metadata.provider = "openai"
        mock_metadata.display_label = None
        mock_metadata.state = "active"
        mock_metadata.rotated_at = None
        mock_metadata.value_sha256 = "abc123"
        mock_metadata.created_by = "test-owner"
        mock_metadata.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mock_metadata.revoked_at = None

        mock_admin_instance = MagicMock()
        mock_admin_instance.list_metadata.return_value = [mock_metadata]
        mock_admin_instance.bindings_by_version.return_value = {}

        from app.main import app
        app.dependency_overrides[request_tenant_context] = lambda: test_context
        try:
            with patch(
                "app.entitlements.credential_admin.CredentialAdmin",
                return_value=mock_admin_instance,
            ):
                with TestClient(app) as client:
                    resp = client.get("/api/v1/credentials")
                    self.assertEqual(resp.status_code, 200)
                    self.assertNotIn(CANARY_SECRET.decode(), resp.text)
                    self.assertNotIn("encrypted_secret", resp.text)
                    self.assertNotIn("ciphertext", resp.text)
        finally:
            app.dependency_overrides.pop(request_tenant_context, None)


class TestLocalOnlyFactoryFallback(unittest.TestCase):
    def test_local_only_without_secret_uses_fallback(self):
        """local_only() without an explicit secret uses config or dev placeholder."""
        adapter = LocalKmsAdapter.local_only()
        self.assertIsInstance(adapter, LocalKmsAdapter)

    def test_local_only_with_explicit_secret(self):
        """local_only() with an explicit secret uses it directly."""
        adapter = LocalKmsAdapter.local_only(MASTER_SECRET)
        self.assertIsInstance(adapter, LocalKmsAdapter)


if __name__ == "__main__":
    unittest.main()
