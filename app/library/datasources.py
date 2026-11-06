"""DataSource CredentialVersion binding: ``bind_credential`` / ``get_active_binding``.

Spec §4.3.

Binds a ``CredentialVersion`` (owned by ``app.entitlements.credentials``, #18)
to a DataSource entry.  Only a reference (``credential_version_id``) ever
crosses into Creative Library storage or audit detail -- the plaintext secret
stays inside the entitlements module's envelope encryption.  Re-binding
supersedes the previous binding rather than mutating it in place: at most one
``active`` binding exists per DataSource entry at any time, enforced by
``ix_dcb_one_active`` on ``DatasourceCredentialBindingRecord``.
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError as _IntegrityError

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    DatasourceCredentialBindingRecord,
    new_id,
    utcnow,
)
from ..platform.tenancy import TenantContext
from .contracts import ENTRY_DATA_SOURCE, LIFECYCLE_ACTIVE
from .service import (
    LibraryConflict,
    LibraryError,
    LibraryForbidden,
    _TerminalFailure,
    _audit,
    _entry,
    _idem_detail,
    _run_with_idempotency,
)

BINDING_ACTIVE = "active"
BINDING_SUPERSEDED = "superseded"


def bind_credential(
    context: TenantContext,
    data_source_entry_id: str,
    credential_version_id: str,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> dict:
    """Bind a CredentialVersion to a DataSource entry (§4.3). Owner-only.

    Re-binding supersedes whatever binding was previously ``active`` and
    claims the next ``revision`` -- the DataSource never has two active
    credentials at once, and the binding history is never overwritten, only
    superseded.
    """

    def body(session, key_hash):
        if context.role not in ("owner", "system"):
            _audit(
                session, context, "bind_credential",
                entry_id=data_source_entry_id, result="forbidden",
                detail=_idem_detail(key_hash),
            )
            return _TerminalFailure(LibraryForbidden("只有 Owner 可以绑定凭据"))

        entry = _entry(session, context, data_source_entry_id)
        if entry.entry_type != ENTRY_DATA_SOURCE:
            raise LibraryError("条目不是数据源", code="entry_type_mismatch")
        if entry.lifecycle != LIFECYCLE_ACTIVE:
            raise LibraryConflict("数据源已归档，不能绑定凭据", code="entry_archived")

        current_max = session.scalar(
            select(func.max(DatasourceCredentialBindingRecord.revision)).where(
                DatasourceCredentialBindingRecord.data_source_entry_id == data_source_entry_id,
                DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
            )
        )
        next_revision = int(current_max or 0) + 1

        session.execute(
            update(DatasourceCredentialBindingRecord)
            .where(
                DatasourceCredentialBindingRecord.data_source_entry_id == data_source_entry_id,
                DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
                DatasourceCredentialBindingRecord.state == BINDING_ACTIVE,
            )
            .values(state=BINDING_SUPERSEDED, updated_at=utcnow())
        )

        binding = DatasourceCredentialBindingRecord(
            id=new_id("dcb"),
            workspace_id=context.workspace_id,
            data_source_entry_id=data_source_entry_id,
            credential_version_id=credential_version_id,
            revision=next_revision,
            state=BINDING_ACTIVE,
        )
        session.add(binding)
        try:
            session.flush()
        except _IntegrityError:
            raise LibraryConflict("凭据绑定冲突，可能存在并发绑定操作", code="library_credential_binding_conflict")

        result = {
            "id": binding.id,
            "data_source_entry_id": data_source_entry_id,
            "credential_version_id": credential_version_id,
            "revision": next_revision,
            "state": BINDING_ACTIVE,
        }
        _audit(
            session, context, "bind_credential", entry_id=data_source_entry_id,
            detail={
                "credential_version_id": credential_version_id,
                "revision": next_revision,
                **_idem_detail(key_hash),
            },
        )
        return result

    return _run_with_idempotency(
        context, idempotency_key, "bind_credential", request_fingerprint, body
    )


def get_active_binding(
    context: TenantContext,
    data_source_entry_id: str,
) -> dict | None:
    """Return the currently active credential binding for a DataSource, or ``None``.

    Read-only projection; not idempotency-tracked.
    """
    with tenant_session(context) as session:
        record = session.scalars(
            select(DatasourceCredentialBindingRecord).where(
                DatasourceCredentialBindingRecord.data_source_entry_id == data_source_entry_id,
                DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
                DatasourceCredentialBindingRecord.state == BINDING_ACTIVE,
            )
        ).one_or_none()
        if record is None:
            return None
        return {
            "id": record.id,
            "data_source_entry_id": record.data_source_entry_id,
            "credential_version_id": record.credential_version_id,
            "revision": record.revision,
            "state": record.state,
        }
