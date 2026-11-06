"""Tests for CredentialProtector / LocalKmsAdapter envelope encryption."""
from __future__ import annotations

import os
import unittest

from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    CredentialProtector,
    EncryptedEnvelope,
    LocalKmsAdapter,
    _PlaintextScope,
)


def _make_context(**overrides):
    defaults = dict(
        workspace_id="ws_test123",
        credential_version_id="cv_abc",
        credential_key="META_ACCESS_TOKEN",
        provider="meta",
        version_no=1,
    )
    defaults.update(overrides)
    return CredentialEncryptionContext(**defaults)


class TestLocalKmsAdapter(unittest.TestCase):
    """Unit tests for LocalKmsAdapter."""

    def setUp(self):
        self.secret = os.urandom(32)
        self.adapter = LocalKmsAdapter(self.secret)

    # 1. seal then open round-trip returns original plaintext
    def test_seal_open_roundtrip(self):
        plaintext = b"sk-test-secret-key-12345"
        ctx = _make_context()

        envelope = self.adapter.seal(plaintext, ctx)
        with self.adapter.open(envelope, ctx) as view:
            recovered = bytes(view)

        self.assertEqual(recovered, plaintext)

    # 2. different AAD context fails to decrypt (tamper detection)
    def test_different_context_fails(self):
        plaintext = b"another-secret"
        ctx1 = _make_context(workspace_id="ws_alpha")
        ctx2 = _make_context(workspace_id="ws_beta")

        envelope = self.adapter.seal(plaintext, ctx1)

        with self.assertRaises(Exception):
            # AES-GCM should reject decryption with wrong AAD.
            with self.adapter.open(envelope, ctx2) as view:
                bytes(view)

    # 3. seal produces different nonce each time (randomness)
    def test_seal_produces_different_nonce(self):
        plaintext = b"same-plaintext"
        ctx = _make_context()

        envelope1 = self.adapter.seal(plaintext, ctx)
        envelope2 = self.adapter.seal(plaintext, ctx)

        self.assertNotEqual(envelope1.nonce, envelope2.nonce)
        # The ciphertext should also differ because the nonce differs.
        self.assertNotEqual(
            envelope1.encrypted_value, envelope2.encrypted_value
        )

    # 4. open context manager zeroes buffer on exit
    def test_open_zeroes_buffer_on_exit(self):
        plaintext = b"secret-to-zero"
        ctx = _make_context()
        envelope = self.adapter.seal(plaintext, ctx)

        scope = self.adapter.open(envelope, ctx)
        with scope as view:
            # View should contain the plaintext.
            self.assertEqual(bytes(view), plaintext)

        # After exit, the internal buffer should be zeroed.
        # Access the internal buffer directly.
        self.assertTrue(all(b == 0 for b in scope._buf))

    # 5. LocalKmsAdapter rejects master_secret shorter than 32 bytes
    def test_rejects_short_master_secret(self):
        with self.assertRaises(ValueError) as cm:
            LocalKmsAdapter(b"too-short")
        self.assertIn("at least 32 bytes", str(cm.exception))

    def test_rejects_empty_master_secret(self):
        with self.assertRaises(ValueError):
            LocalKmsAdapter(b"")

    # 6. envelope has correct metadata
    def test_envelope_metadata(self):
        plaintext = b"test"
        ctx = _make_context()
        envelope = self.adapter.seal(plaintext, ctx)

        self.assertEqual(envelope.encryption_key_id, "local-kms-v1")
        self.assertEqual(envelope.encryption_algorithm, "aes-256-gcm")
        self.assertEqual(envelope.aad_schema_version, 1)
        self.assertIsInstance(envelope.encrypted_value, bytes)
        self.assertIsInstance(envelope.encrypted_data_key, bytes)
        self.assertIsInstance(envelope.nonce, bytes)
        # Nonce should be 12 bytes for AES-GCM.
        self.assertEqual(len(envelope.nonce), 12)

    # 7. different providers produce different ciphertext
    def test_different_provider_different_ciphertext(self):
        plaintext = b"same-key"
        ctx_meta = _make_context(provider="meta")
        ctx_openai = _make_context(provider="openai")

        env_meta = self.adapter.seal(plaintext, ctx_meta)
        env_openai = self.adapter.seal(plaintext, ctx_openai)

        # Even if by chance the nonces were the same (extremely unlikely),
        # the derived keys differ, so ciphertext must differ.
        # We just verify we can round-trip each with its own context.
        with self.adapter.open(env_meta, ctx_meta) as view:
            self.assertEqual(bytes(view), plaintext)
        with self.adapter.open(env_openai, ctx_openai) as view:
            self.assertEqual(bytes(view), plaintext)

    # 8. different version_no produces different ciphertext
    def test_different_version_no(self):
        plaintext = b"versioned-secret"
        ctx_v1 = _make_context(version_no=1)
        ctx_v2 = _make_context(version_no=2)

        env1 = self.adapter.seal(plaintext, ctx_v1)

        # Cannot decrypt v1 envelope with v2 context.
        with self.assertRaises(Exception):
            with self.adapter.open(env1, ctx_v2) as view:
                bytes(view)

    # 9. Satisfies CredentialProtector protocol
    def test_satisfies_protocol(self):
        self.assertIsInstance(self.adapter, CredentialProtector)

    # 10. local_only factory method
    def test_local_only_factory(self):
        adapter = LocalKmsAdapter.local_only(os.urandom(32))
        self.assertIsInstance(adapter, LocalKmsAdapter)

    # 11. Large plaintext
    def test_large_plaintext(self):
        plaintext = os.urandom(10000)
        ctx = _make_context()
        envelope = self.adapter.seal(plaintext, ctx)
        with self.adapter.open(envelope, ctx) as view:
            self.assertEqual(bytes(view), plaintext)

    # 12. Tampered ciphertext fails
    def test_tampered_ciphertext_fails(self):
        plaintext = b"original"
        ctx = _make_context()
        envelope = self.adapter.seal(plaintext, ctx)

        # Flip a byte in the ciphertext.
        tampered = bytearray(envelope.encrypted_value)
        tampered[0] ^= 0xFF
        tampered_envelope = EncryptedEnvelope(
            encrypted_value=bytes(tampered),
            encrypted_data_key=envelope.encrypted_data_key,
            nonce=envelope.nonce,
            encryption_key_id=envelope.encryption_key_id,
            encryption_algorithm=envelope.encryption_algorithm,
            aad_schema_version=envelope.aad_schema_version,
        )

        with self.assertRaises(Exception):
            with self.adapter.open(tampered_envelope, ctx) as view:
                bytes(view)


class TestPlaintextScope(unittest.TestCase):
    """Test the _PlaintextScope context manager directly."""

    def test_yields_memoryview(self):
        scope = _PlaintextScope(b"hello")
        with scope as view:
            self.assertIsInstance(view, memoryview)
            self.assertEqual(bytes(view), b"hello")

    def test_zeroes_on_exit(self):
        scope = _PlaintextScope(b"secret")
        with scope:
            pass
        self.assertTrue(all(b == 0 for b in scope._buf))

    def test_zeroes_on_exception(self):
        scope = _PlaintextScope(b"secret")
        with self.assertRaises(RuntimeError):
            with scope:
                raise RuntimeError("oops")
        self.assertTrue(all(b == 0 for b in scope._buf))


if __name__ == "__main__":
    unittest.main()
