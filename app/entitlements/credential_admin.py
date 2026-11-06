"""CredentialAdmin -- owner-only BYOK credential lifecycle commands.

Create, rotate, revoke, bind, and list BYOK credentials for a workspace.
All mutations require owner role.

Every read here names its columns. The API role that runs these commands is
granted the non-secret columns of ``credential_versions`` only
(``deploy/postgres/domain-tenant-roles.sql``): ``encrypted_secret``,
``encrypted_data_key`` and ``nonce`` are readable by the Worker alone, so an
ORM load of a whole ``CredentialVersionRecord`` is a permission error on
PostgreSQL. Writes go through explicit ``INSERT``/``UPDATE`` statements for
the same reason.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    CredentialProtector,
)
from app.entitlements.credentials import (
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.entitlements.types import CredentialMetadata
from app.market_data.credentials import binding_scope_mismatch
from app.platform.tenancy import TenantContext


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CredentialNotFound(ValueError):
    """The version is not one of this Workspace's (missing or another Workspace's)."""


class CredentialScopeMismatch(ValueError):
    """The provider's key may not serve the requested binding scope."""


def _require_owner(context: TenantContext) -> None:
    if context.initiator_role != "owner" and context.role != "owner":
        raise PermissionError(
            "credential management requires owner role; "
            f"got role={context.role!r}, initiator_role={context.initiator_role!r}"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: The columns the API role may read from ``credential_versions``.
_METADATA_COLUMNS = (
    CredentialVersionRecord.id,
    CredentialVersionRecord.workspace_id,
    CredentialVersionRecord.credential_key,
    CredentialVersionRecord.version_no,
    CredentialVersionRecord.provider,
    CredentialVersionRecord.display_label,
    CredentialVersionRecord.state,
    CredentialVersionRecord.created_by,
    CredentialVersionRecord.created_at,
    CredentialVersionRecord.rotated_at,
    CredentialVersionRecord.revoked_at,
)


def _version_fingerprint(version_id: str) -> str:
    """A stable, non-secret fingerprint of one credential version.

    Earlier revisions hashed the ciphertext, which the API role can no
    longer read; the version id is unique per sealed value and serves the
    same display purpose (telling two versions apart) without touching it.
    """
    return _sha256(version_id.encode("utf-8"))


def _to_metadata(row: Any) -> CredentialMetadata:
    return CredentialMetadata(
        credential_version_id=row.id,
        workspace_id=row.workspace_id,
        credential_key=row.credential_key,
        version_no=row.version_no,
        value_sha256=_version_fingerprint(row.id),
        created_by=row.created_by,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        provider=row.provider,
        display_label=row.display_label,
        state=row.state,
        rotated_at=row.rotated_at,
    )


def _version_row(session: Session, workspace_id: str, version_id: str):
    return session.execute(
        select(*_METADATA_COLUMNS).where(
            CredentialVersionRecord.workspace_id == workspace_id,
            CredentialVersionRecord.id == version_id,
        )
    ).first()


def _next_version_no(session: Session, workspace_id: str, credential_key: str) -> int:
    latest = session.execute(
        select(func.max(CredentialVersionRecord.version_no)).where(
            CredentialVersionRecord.workspace_id == workspace_id,
            CredentialVersionRecord.credential_key == credential_key,
        )
    ).scalar_one()
    return int(latest or 0) + 1


def _active_version(session: Session, workspace_id: str, credential_key: str):
    return session.execute(
        select(*_METADATA_COLUMNS).where(
            CredentialVersionRecord.workspace_id == workspace_id,
            CredentialVersionRecord.credential_key == credential_key,
            CredentialVersionRecord.state == "active",
        )
    ).first()


class CredentialAdmin:
    """Owner-only credential lifecycle commands."""

    def __init__(self, protector: CredentialProtector) -> None:
        self._protector = protector

    def active_version_id(
        self, session: Session, context: TenantContext, credential_key: str,
    ) -> str | None:
        _require_owner(context)
        row = _active_version(session, context.workspace_id, credential_key)
        return row.id if row is not None else None

    def active_provider(
        self, session: Session, context: TenantContext, credential_key: str,
    ) -> str | None:
        """The provider of ``credential_key``'s active version, if it has one."""
        _require_owner(context)
        row = _active_version(session, context.workspace_id, credential_key)
        return row.provider if row is not None else None

    def create(
        self,
        session: Session,
        context: TenantContext,
        *,
        credential_key: str,
        provider: str,
        plaintext: bytes,
        display_label: str | None = None,
    ) -> CredentialMetadata:
        _require_owner(context)

        if _active_version(session, context.workspace_id, credential_key) is not None:
            raise ValueError(
                f"active credential already exists for {credential_key!r}"
            )

        version_id = _new_id("cv")
        now = _utcnow()
        # A key whose every version was revoked can be created again; its
        # numbering continues (``(workspace, key, version_no)`` is unique).
        version_no = _next_version_no(session, context.workspace_id, credential_key)

        enc_ctx = CredentialEncryptionContext(
            workspace_id=context.workspace_id,
            credential_version_id=version_id,
            credential_key=credential_key,
            provider=provider,
            version_no=version_no,
        )
        envelope = self._protector.seal(plaintext, enc_ctx)

        record = CredentialVersionRecord(
            id=version_id,
            workspace_id=context.workspace_id,
            credential_key=credential_key,
            version_no=version_no,
            provider=provider,
            display_label=display_label,
            state="active",
            encrypted_secret=envelope.encrypted_value,
            encrypted_data_key=envelope.encrypted_data_key,
            encryption_key_id=envelope.encryption_key_id,
            encryption_algorithm=envelope.encryption_algorithm,
            nonce=envelope.nonce,
            aad_schema_version=envelope.aad_schema_version,
            created_by=context.initiator_id or context.executor,
            created_at=now,
        )
        session.add(record)
        session.flush()
        # Expire the ORM identity so a later read of this version goes back
        # to the database for its (permitted) columns instead of serving the
        # just-inserted ciphertext from the session cache.
        session.expunge(record)

        return _to_metadata(_version_row(session, context.workspace_id, version_id))

    def rotate(
        self,
        session: Session,
        context: TenantContext,
        *,
        credential_key: str,
        new_plaintext: bytes,
    ) -> CredentialMetadata:
        _require_owner(context)

        current = _active_version(session, context.workspace_id, credential_key)
        if current is None:
            raise ValueError(f"no active credential for {credential_key!r}")

        now = _utcnow()
        new_version_no = current.version_no + 1
        new_version_id = _new_id("cv")

        enc_ctx = CredentialEncryptionContext(
            workspace_id=context.workspace_id,
            credential_version_id=new_version_id,
            credential_key=credential_key,
            provider=current.provider,
            version_no=new_version_no,
        )
        envelope = self._protector.seal(new_plaintext, enc_ctx)

        session.execute(
            update(CredentialVersionRecord)
            .where(
                CredentialVersionRecord.id == current.id,
                CredentialVersionRecord.workspace_id == context.workspace_id,
            )
            .values(
                state="rotated",
                rotated_at=now,
                rotated_by=context.initiator_id or context.executor,
            )
        )

        new_record = CredentialVersionRecord(
            id=new_version_id,
            workspace_id=context.workspace_id,
            credential_key=credential_key,
            version_no=new_version_no,
            provider=current.provider,
            display_label=current.display_label,
            state="active",
            encrypted_secret=envelope.encrypted_value,
            encrypted_data_key=envelope.encrypted_data_key,
            encryption_key_id=envelope.encryption_key_id,
            encryption_algorithm=envelope.encryption_algorithm,
            nonce=envelope.nonce,
            aad_schema_version=envelope.aad_schema_version,
            created_by=context.initiator_id or context.executor,
            created_at=now,
        )
        session.add(new_record)
        session.flush()
        session.expunge(new_record)

        return _to_metadata(_version_row(session, context.workspace_id, new_version_id))

    def revoke(
        self,
        session: Session,
        context: TenantContext,
        *,
        credential_key: str,
        reason: str,
    ) -> None:
        _require_owner(context)

        current = _active_version(session, context.workspace_id, credential_key)
        if current is None:
            raise ValueError(f"no active credential for {credential_key!r}")

        now = _utcnow()
        session.execute(
            update(CredentialVersionRecord)
            .where(
                CredentialVersionRecord.id == current.id,
                CredentialVersionRecord.workspace_id == context.workspace_id,
            )
            .values(
                state="revoked",
                revoked_at=now,
                revoked_by=context.initiator_id or context.executor,
                revoke_reason=reason,
            )
        )
        session.flush()

    def bind(
        self,
        session: Session,
        context: TenantContext,
        *,
        binding_scope: str,
        provider: str,
        credential_version_id: str,
    ) -> dict:
        _require_owner(context)

        # Validate: credential version must exist, be active, match the
        # requested provider, and that provider must serve the scope.
        cred_version = _version_row(session, context.workspace_id, credential_version_id)
        if cred_version is None:
            raise CredentialNotFound(
                f"credential version {credential_version_id!r} not found "
                f"in workspace {context.workspace_id!r}"
            )
        mismatch = binding_scope_mismatch(binding_scope, provider)
        if mismatch is not None:
            raise CredentialScopeMismatch(mismatch)
        if cred_version.state != "active":
            raise ValueError(
                f"credential version {credential_version_id!r} is in state "
                f"{cred_version.state!r}, expected 'active'"
            )
        if cred_version.provider != provider:
            raise ValueError(
                f"credential version {credential_version_id!r} is for "
                f"provider {cred_version.provider!r}, not {provider!r}"
            )

        now = _utcnow()
        binding_id = _new_id("cb")

        existing = (
            session.query(CredentialBindingRecord)
            .filter_by(
                workspace_id=context.workspace_id,
                binding_scope=binding_scope,
                provider=provider,
            )
            .first()
        )

        if existing is not None:
            existing.credential_version_id = credential_version_id
            existing.revision += 1
            existing.bound_by = context.initiator_id or context.executor
            existing.updated_at = now
            session.flush()
            binding_id = existing.id
        else:
            binding = CredentialBindingRecord(
                id=binding_id,
                workspace_id=context.workspace_id,
                binding_scope=binding_scope,
                provider=provider,
                credential_version_id=credential_version_id,
                revision=1,
                bound_by=context.initiator_id or context.executor,
                created_at=now,
                updated_at=now,
            )
            session.add(binding)
            session.flush()

        return {
            "binding_id": binding_id,
            "workspace_id": context.workspace_id,
            "binding_scope": binding_scope,
            "provider": provider,
            "credential_version_id": credential_version_id,
        }

    def list_metadata(
        self,
        session: Session,
        context: TenantContext,
    ) -> list[CredentialMetadata]:
        _require_owner(context)

        rows = session.execute(
            select(*_METADATA_COLUMNS)
            .where(
                CredentialVersionRecord.workspace_id == context.workspace_id,
                CredentialVersionRecord.state == "active",
            )
            .order_by(CredentialVersionRecord.credential_key)
        ).all()
        return [_to_metadata(r) for r in rows]

    def bindings_by_version(
        self,
        session: Session,
        context: TenantContext,
    ) -> dict[str, list[dict[str, str]]]:
        """Which ``(binding_scope, provider)`` bindings point at each version."""
        _require_owner(context)

        rows = session.execute(
            select(
                CredentialBindingRecord.credential_version_id,
                CredentialBindingRecord.binding_scope,
                CredentialBindingRecord.provider,
            )
            .where(CredentialBindingRecord.workspace_id == context.workspace_id)
            .order_by(CredentialBindingRecord.binding_scope, CredentialBindingRecord.provider)
        ).all()
        bindings: dict[str, list[dict[str, str]]] = {}
        for version_id, scope, provider in rows:
            bindings.setdefault(version_id, []).append(
                {"binding_scope": scope, "provider": provider}
            )
        return bindings
