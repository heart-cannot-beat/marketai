"""Domain SQLAlchemy models for BYOK credential management.

Spec section 2.4: CredentialVersionRecord -- immutable encrypted credential
versions with envelope encryption, rotation lifecycle, and workspace-bound AAD.

Spec section 2.5: CredentialBindingRecord -- maps a credential version to a
provider/scope, enforcing at most one active binding per provider per workspace.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base


class CredentialVersionRecord(Base):
    """Immutable encrypted credential version (spec section 2.4).

    Each row stores one version of a credential key (e.g.
    ``META_ACCESS_TOKEN``, ``OPENAI_API_KEY``).  The plaintext is
    AES-256-GCM encrypted with a workspace-bound AAD context using
    KMS envelope encryption.  Rows are immutable except for state
    transitions: active -> rotated, active -> revoked, rotated -> revoked.
    """

    __tablename__ = "credential_versions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "credential_key", "version_no",
            name="uq_credential_version_workspace_key_version",
        ),
        UniqueConstraint(
            "workspace_id", "id",
            name="uq_credential_versions_workspace_id_id",
        ),
        CheckConstraint(
            "version_no >= 1",
            name="credential_version_no_positive",
        ),
        CheckConstraint(
            "state IN ('active', 'rotated', 'revoked')",
            name="credential_version_state_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    credential_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    display_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active",
    )

    # Envelope encryption fields
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    encryption_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="aes-256-gcm",
    )
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    aad_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    rotated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)


# Partial unique index: at most one active (non-revoked) credential per
# credential_key per workspace.  Uses state='active' rather than revoked_at IS NULL
# to match the spec's state machine.
Index(
    "uq_credential_version_active",
    CredentialVersionRecord.workspace_id,
    CredentialVersionRecord.credential_key,
    unique=True,
    sqlite_where=text("state = 'active'"),
    postgresql_where=text("state = 'active'"),
)


class CredentialBindingRecord(Base):
    """Maps a credential version to a provider/scope (spec section 2.5).

    Binding is the current projection; history is preserved by append-only
    AuditEvents.  The provider must match the CredentialVersion's provider.
    Updates use revision/ETag to prevent concurrent overwrites.
    """

    __tablename__ = "credential_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "binding_scope", "provider",
            name="uq_credential_binding_workspace_scope_provider",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_credential_bindings_workspace_cred_credential_versions",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    binding_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bound_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
