"""DataSource connection checks: what the Worker can verify without a provider (#383).

No DataSource adapter talks to a provider yet: ``manual`` has nothing to
connect to, ``mock`` is the Controlled Mock, and ``shopify`` has no adapter
(``app/localization/fact_sources.py`` forbids a stub). So a check verifies
only what is local -- the effective Published Version, its content, whether
its adapter is implemented, and the state of the bound CredentialVersion. It
never decrypts a secret (only the version's ``state`` column is read) and
never makes a network request. A real probe belongs to the ticket of the
first real adapter.

The execution mode is the version's, never the client's: the requested value
is recorded beside it and changes nothing, and nothing falls back to the
Mock.

Each check is one append-only audit event (``CHECK_EVENT``) on the data
source, written in the same transaction that settles its Operation. The
Operation only says the check ran; whether the source is healthy lives in the
event (contract §3.2: one status never carries two dimensions), which
``get_connection_check`` serves by the id the Operation's result names.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.database import tenant_session
from ..adapters.persistence.models import (
    AuditEventRecord,
    DatasourceCredentialBindingRecord,
)
from ..entitlements.credentials import CredentialVersionRecord
from ..localization.fact_sources import ControlledMockFactSourceAdapter
from ..platform.audit import append_event
from ..platform.tenancy import TenantContext, current_context
from .contracts import ENTRY_DATA_SOURCE, validate_content
from .service import LibraryError, LibraryNotFound, _effective_version, _entry

CHECK_EVENT = "library.data_source_connection_checked"
#: What this check verifies. A future real probe gets its own version, so its
#: evidence never reads as this local one.
CHECK_VERSION = "local-v1"
#: ``OperationResultRef.resource_type`` of a finished check; the id is the
#: audit event's.
RESULT_RESOURCE_TYPE = "data_source_connection_check"

HEALTH_HEALTHY = "healthy"
HEALTH_FAILED = "failed"
HEALTH_REVOKED = "revoked"

#: The closed set of conclusions, in the order they are tested, with the
#: ConnectionHealth each one means.
REASON_HEALTH: dict[str, str] = {
    "data_source_not_published": HEALTH_FAILED,
    "data_source_config_invalid": HEALTH_FAILED,
    "adapter_not_enabled": HEALTH_FAILED,
    "credential_not_active": HEALTH_REVOKED,
    "manual_source_no_connection": HEALTH_HEALTHY,
    "controlled_mock_source": HEALTH_HEALTHY,
}

#: The adapters ``app/localization/truth.py`` can serve, with the version
#: identity it stamps on their evidence. Any other ``ADAPTER_KINDS`` entry
#: resolves to ``adapter_not_enabled`` there and here.
IMPLEMENTED_ADAPTERS: dict[str, str] = {
    "manual": "manual-v1",
    "mock": ControlledMockFactSourceAdapter._VERSION,
}

_ADAPTER_REASON = {
    "manual": "manual_source_no_connection",
    "mock": "controlled_mock_source",
}


@dataclass(frozen=True)
class ConnectionCheckOutcome:
    """One check's conclusion; ``as_details`` is its audit body.

    The bound CredentialVersion is ``bound_version_*``: the audit redactor
    masks every key that contains ``credential``.
    """

    reason_code: str
    adapter_kind: str | None = None
    adapter_version: str | None = None
    execution_mode: str | None = None
    data_source_version_id: str | None = None
    bound_version_id: str | None = None
    bound_version_state: str | None = None

    @property
    def health(self) -> str:
        return REASON_HEALTH[self.reason_code]

    def as_details(self, *, requested_mode: str | None, operation_id: str) -> dict:
        return {
            "check_version": CHECK_VERSION,
            "health": self.health,
            **asdict(self),
            "requested_mode": requested_mode,
            "operation_id": operation_id,
        }


def evaluate(
    *,
    version_id: str | None,
    content: object,
    bound_version_id: str | None,
    bound_version_state: str | None,
) -> ConnectionCheckOutcome:
    """Conclude a check from what was read; the first failing test wins."""
    if version_id is None:
        return ConnectionCheckOutcome("data_source_not_published")
    if not isinstance(content, dict) or not validate_content(ENTRY_DATA_SOURCE, content).ok:
        return ConnectionCheckOutcome(
            "data_source_config_invalid", data_source_version_id=version_id,
        )
    adapter_kind = content["adapter_kind"]
    found = ConnectionCheckOutcome(
        "adapter_not_enabled",
        adapter_kind=adapter_kind,
        adapter_version=IMPLEMENTED_ADAPTERS.get(adapter_kind),
        execution_mode=content["execution_mode"],
        data_source_version_id=version_id,
        bound_version_id=bound_version_id,
        bound_version_state=bound_version_state,
    )
    if adapter_kind not in IMPLEMENTED_ADAPTERS:
        return found
    if bound_version_id is not None and bound_version_state != "active":
        return _with_reason(found, "credential_not_active")
    return _with_reason(found, _ADAPTER_REASON[adapter_kind])


def _with_reason(outcome: ConnectionCheckOutcome, reason_code: str) -> ConnectionCheckOutcome:
    return ConnectionCheckOutcome(**{**asdict(outcome), "reason_code": reason_code})


def record_connection_check(
    session: Session,
    *,
    data_source_entry_id: str,
    requested_mode: str | None,
    operation_id: str,
) -> AuditEventRecord:
    """Check the data source and append the evidence to ``session``'s transaction.

    Raises ``LibraryError`` when there is no data source to check: the check
    could not run, so nothing is recorded.
    """
    context = current_context(session)
    entry = _entry(session, context, data_source_entry_id)
    if entry.entry_type != ENTRY_DATA_SOURCE:
        raise LibraryError("条目不是数据源", code="entry_type_mismatch")
    version = _effective_version(session, entry)
    bound_version_id = session.scalar(
        select(DatasourceCredentialBindingRecord.credential_version_id).where(
            DatasourceCredentialBindingRecord.workspace_id == context.workspace_id,
            DatasourceCredentialBindingRecord.data_source_entry_id == entry.id,
            DatasourceCredentialBindingRecord.state == "active",
        )
    )
    bound_version_state = None
    if bound_version_id is not None:
        # The state column alone: the envelope is never loaded.
        bound_version_state = session.scalar(
            select(CredentialVersionRecord.state).where(
                CredentialVersionRecord.workspace_id == context.workspace_id,
                CredentialVersionRecord.id == bound_version_id,
            )
        ) or "missing"
    outcome = evaluate(
        version_id=version.id if version is not None else None,
        content=version.content_json if version is not None else None,
        bound_version_id=bound_version_id,
        bound_version_state=bound_version_state,
    )
    return append_event(
        session,
        event_type=CHECK_EVENT,
        resource_type=ENTRY_DATA_SOURCE,
        resource_id=entry.id,
        details=outcome.as_details(
            requested_mode=requested_mode[:40] if requested_mode else None,
            operation_id=operation_id,
        ),
    )


def get_connection_check(
    context: TenantContext, data_source_entry_id: str, check_id: str,
) -> dict:
    """One check of ``data_source_entry_id``, read back from its evidence.

    An id that names another Workspace's check, another data source's, or
    any other audit event is ``connection_check_not_found``, the same as an
    id that never existed.
    """
    with tenant_session(context) as session:
        event = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.workspace_id == context.workspace_id,
                AuditEventRecord.id == check_id,
                AuditEventRecord.event_type == CHECK_EVENT,
                AuditEventRecord.resource_type == ENTRY_DATA_SOURCE,
                AuditEventRecord.resource_id == data_source_entry_id,
            )
        )
        if event is None:
            raise LibraryNotFound("连接检查不存在", code="connection_check_not_found")
        details = event.details_json or {}
        return {
            "check_id": event.id,
            "data_source_entry_id": event.resource_id,
            "health": details.get("health"),
            "reason_code": details.get("reason_code"),
            "adapter_kind": details.get("adapter_kind"),
            "adapter_version": details.get("adapter_version"),
            "execution_mode": details.get("execution_mode"),
            "data_source_version_id": details.get("data_source_version_id"),
            "credential_state": details.get("bound_version_state"),
            "checked_at": event.created_at,
        }
