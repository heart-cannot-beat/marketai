"""CredentialProtector: envelope encryption for BYOK credentials.

AES-256-GCM envelope encryption with HKDF-SHA256 key derivation.
The ``LocalKmsAdapter`` is the dev/test implementation backed by a
local master secret; production will plug in an HSM or cloud KMS
adapter that satisfies the same ``CredentialProtector`` protocol.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialEncryptionContext:
    """AAD context fields for credential encryption."""

    workspace_id: str
    credential_version_id: str
    credential_key: str
    provider: str
    version_no: int
    aad_schema_version: int = 1


@dataclass(frozen=True)
class EncryptedEnvelope:
    """Encrypted credential value with envelope metadata."""

    encrypted_value: bytes
    encrypted_data_key: bytes
    nonce: bytes
    encryption_key_id: str
    encryption_algorithm: str
    aad_schema_version: int = 1


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialProtector(Protocol):
    """Envelope encryption protocol for BYOK credentials."""

    def seal(
        self,
        plaintext: bytes,
        context: CredentialEncryptionContext,
    ) -> EncryptedEnvelope:
        """Encrypt *plaintext* under the given AAD context."""
        ...

    def open(
        self,
        envelope: EncryptedEnvelope,
        context: CredentialEncryptionContext,
    ) -> "_PlaintextScope":
        """Decrypt *envelope* under the given AAD context.

        Returns a context manager that yields a ``memoryview`` over
        the decrypted bytes and best-effort zeroes the buffer on exit.
        """
        ...

    @classmethod
    def local_only(cls, secret: bytes | None = None) -> "LocalKmsAdapter":
        """Return a ``LocalKmsAdapter`` backed by *secret*.

        When *secret* is ``None``, falls back to the config value
        ``MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET`` or a dev-only
        placeholder so existing call sites that omit the argument
        continue to work.
        """
        if secret is None:
            from app import config

            raw = getattr(
                config, "MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET", ""
            ) or "dev-only-insecure-placeholder-key!!"
            secret = raw.encode("utf-8") if isinstance(raw, str) else raw
        return LocalKmsAdapter(secret)


class _PlaintextScope:
    """Context manager yielding a memoryview with best-effort zeroing."""

    __slots__ = ("_buf",)

    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    def __enter__(self) -> memoryview:
        return memoryview(self._buf)

    def __exit__(self, *_exc: object) -> None:
        # Best-effort buffer zeroing.
        for i in range(len(self._buf)):
            self._buf[i] = 0


# ---------------------------------------------------------------------------
# LocalKmsAdapter (dev/test concrete implementation)
# ---------------------------------------------------------------------------

# Constants
_KEY_ID = "local-kms-v1"
_ALGORITHM = "aes-256-gcm"
_DATA_KEY_LEN = 32
_NONCE_LEN = 12


def _build_aad(context: CredentialEncryptionContext) -> bytes:
    """Build the AAD string from the encryption context."""
    aad = (
        f"v{context.aad_schema_version}:"
        f"{context.workspace_id}:"
        f"{context.credential_key}:"
        f"{context.provider}:"
        f"{context.version_no}"
    )
    return aad.encode("utf-8")


def _derive_key(master_secret: bytes, info: bytes, length: int = 32) -> bytes:
    """Derive a key using HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=SHA256(),
        length=length,
        salt=None,
        info=info,
    )
    return hkdf.derive(master_secret)


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two equal-length byte strings."""
    return bytes(x ^ y for x, y in zip(a, b))


class LocalKmsAdapter:
    """Dev/test envelope encryption backed by a local master secret.

    Uses AES-256-GCM for data encryption and HKDF-SHA256 for key
    derivation.  The data key is "encrypted" by XOR with a wrapping
    key derived from the master secret.
    """

    __slots__ = ("_master_secret",)

    def __init__(self, master_secret: bytes) -> None:
        if len(master_secret) < 32:
            raise ValueError(
                f"master_secret must be at least 32 bytes, got {len(master_secret)}"
            )
        self._master_secret = master_secret

    # -- CredentialProtector protocol ----------------------------------------

    def seal(
        self,
        plaintext: bytes,
        context: CredentialEncryptionContext,
    ) -> EncryptedEnvelope:
        aad = _build_aad(context)

        # Generate a random data key and nonce.
        data_key = os.urandom(_DATA_KEY_LEN)
        nonce = os.urandom(_NONCE_LEN)

        # Derive the encryption key from the master secret using AAD as info.
        encryption_key = _derive_key(self._master_secret, info=aad, length=_DATA_KEY_LEN)

        # Encrypt plaintext with AES-256-GCM.
        aesgcm = AESGCM(encryption_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

        # "Encrypt" the data key by XOR with a wrapping key derived from
        # master_secret.  The wrapping key uses a different info label so it
        # cannot collide with the encryption key derivation.
        wrapping_key = _derive_key(
            self._master_secret,
            info=b"local-kms-v1:wrap:" + aad,
            length=_DATA_KEY_LEN,
        )
        encrypted_data_key = _xor_bytes(data_key, wrapping_key)

        return EncryptedEnvelope(
            encrypted_value=ciphertext,
            encrypted_data_key=encrypted_data_key,
            nonce=nonce,
            encryption_key_id=_KEY_ID,
            encryption_algorithm=_ALGORITHM,
            aad_schema_version=context.aad_schema_version,
        )

    def open(
        self,
        envelope: EncryptedEnvelope,
        context: CredentialEncryptionContext,
    ) -> _PlaintextScope:
        aad = _build_aad(context)

        # Derive the same encryption key.
        encryption_key = _derive_key(self._master_secret, info=aad, length=_DATA_KEY_LEN)

        # Decrypt plaintext.
        aesgcm = AESGCM(encryption_key)
        plaintext = aesgcm.decrypt(envelope.nonce, envelope.encrypted_value, aad)

        return _PlaintextScope(plaintext)

    # -- factory -------------------------------------------------------------

    @classmethod
    def local_only(cls, secret: bytes | None = None) -> "LocalKmsAdapter":
        """Return a ``LocalKmsAdapter`` backed by *secret*."""
        if secret is None:
            from app import config

            raw = getattr(
                config, "MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET", ""
            ) or "dev-only-insecure-placeholder-key!!"
            secret = raw.encode("utf-8") if isinstance(raw, str) else raw
        return cls(secret)
