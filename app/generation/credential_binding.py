"""Which BYOK credential a Workspace has bound for Generation.

The preview, Atomic Submission and the Worker all answer the same question
-- "is there an active credential bound to ``binding_scope='generation'``
for this provider?" -- so it is answered once, here, with a column-level
query: the API role may read a credential version's identity and state but
never its ciphertext (``deploy/postgres/domain-tenant-roles.sql``), and the
decrypting Worker path (:func:`open_bound_secret`) loads the envelope
separately.

Scope semantics: ``binding_scope='generation'`` is the Workspace's key *for
a provider's generative calls*, not for one endpoint. The OpenAI key an
Owner binds once in Settings pays for GPT-Image renders
(``app.generation.byok_dispatch``) and for governed ``llm_chat`` calls
(``app.providers.llm_chat``) alike, so existing bindings keep working for
chat without re-binding and there is no second scope to keep in sync.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    CredentialProtector,
    EncryptedEnvelope,
)
from app.entitlements.credentials import CredentialBindingRecord, CredentialVersionRecord
from app.platform.tenancy import TenantContext

GENERATION_BINDING_SCOPE = "generation"


def resolve_generation_credential(
    session: Session, ctx: TenantContext, provider: str,
) -> dict:
    """The ``credential_evidence`` dict ``execution_policy`` admits BYOK with.

    ``{"active": True, "provider": ..., "version_id": ...}`` when an active
    credential is bound for ``provider``; ``{}`` otherwise (no binding, or a
    binding whose version was rotated/revoked underneath it).
    """
    row = session.execute(
        select(
            CredentialBindingRecord.credential_version_id,
            CredentialVersionRecord.state,
        )
        .join(
            CredentialVersionRecord,
            (CredentialBindingRecord.credential_version_id == CredentialVersionRecord.id)
            & (CredentialBindingRecord.workspace_id == CredentialVersionRecord.workspace_id),
        )
        .where(
            CredentialBindingRecord.workspace_id == ctx.workspace_id,
            CredentialBindingRecord.binding_scope == GENERATION_BINDING_SCOPE,
            CredentialBindingRecord.provider == provider,
        )
    ).first()
    if row is None or row[1] != "active":
        return {}
    return {"active": True, "provider": provider, "version_id": row[0]}


def open_bound_secret(
    session: Session,
    ctx: TenantContext,
    credential_version_id: str,
    protector: CredentialProtector,
) -> bytes:
    """Decrypt one of this Workspace's credential versions (Worker only).

    The version is looked up inside ``ctx``'s Workspace, so another
    Workspace's version id resolves to nothing (``NoResultFound``) rather
    than to its key; the envelope's AAD binds the same Workspace again at
    decryption. The plaintext is returned as ``bytes`` for the caller to
    hand to an adapter as an ephemeral ``memoryview`` and zero afterwards.
    """
    version = session.execute(
        select(CredentialVersionRecord).where(
            CredentialVersionRecord.id == credential_version_id,
            CredentialVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()
    envelope = EncryptedEnvelope(
        encrypted_value=version.encrypted_secret,
        encrypted_data_key=version.encrypted_data_key,
        nonce=version.nonce,
        encryption_key_id=version.encryption_key_id,
        encryption_algorithm=version.encryption_algorithm,
        aad_schema_version=version.aad_schema_version,
    )
    enc_ctx = CredentialEncryptionContext(
        workspace_id=ctx.workspace_id,
        credential_version_id=version.id,
        credential_key=version.credential_key,
        provider=version.provider,
        version_no=version.version_no,
    )
    with protector.open(envelope, enc_ctx) as view:
        return bytes(view)
