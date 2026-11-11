"""Mode-driven adapter selection and credential resolution (spec §4).

Resolution sequence:
1. Determine adapter from meta_mode
2. Resolve binding metadata (composite FK query)
3. For internal_validation: the platform Workspace only, and a binding that
   names both credential versions. The read adapter dereferences them through
   the credential protector on every call (#112), so no plaintext outlives
   one request and none is ever persisted.

Intent commit (§4.2) acquires FOR SHARE locks on connection, binding,
and credential versions in a single transaction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.meta.http_read_adapter import build_internal_validation_adapter
from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.meta.models import (
    MetaCallIntentRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)


class AdapterResolutionError(RuntimeError):
    """No adapter for this read. ``permanent``: no retry of the same Job can
    change that (the Workspace or the binding's credentials are wrong)."""

    def __init__(self, message: str, *, code: str = "meta_adapter_unavailable", permanent: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.permanent = permanent


@dataclass(frozen=True)
class ResolvedAdapter:
    adapter: Any
    binding: MetaDataSourceBindingRecord
    access_token_version_id: str | None
    app_secret_version_id: str | None
    scenario_version_id: str | None


def _compute_request_fingerprint(
    method: str, endpoint: str, parameters: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {"method": method, "endpoint": endpoint, "parameters": parameters},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def resolve_adapter(
    session: Session,
    *,
    workspace_id: str,
    meta_mode: str,
    connection_id: str,
    ad_account_id: str,
    read_only: bool = False,
) -> ResolvedAdapter:
    """The adapter for one Meta call of this connection.

    ``read_only`` callers (the scheduled observation executor) may get the
    ``internal_validation`` read adapter, which has no write method. Every
    other caller -- the Launch saga's writes and readbacks -- is refused for
    that mode before any call intent exists, as it always was: the mode
    reads Meta and never writes to it (ADR 0009, ADR 0017).
    """
    conn = session.execute(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.id == connection_id,
            MetaConnectionRecord.workspace_id == workspace_id,
            MetaConnectionRecord.meta_mode == meta_mode,
        )
    ).scalar_one_or_none()
    if conn is None or conn.state != "active":
        raise AdapterResolutionError(
            f"Connection {connection_id} not active for "
            f"workspace={workspace_id} mode={meta_mode}"
        )
    binding = session.execute(
        select(MetaDataSourceBindingRecord).where(
            MetaDataSourceBindingRecord.workspace_id == workspace_id,
            MetaDataSourceBindingRecord.meta_mode == meta_mode,
            MetaDataSourceBindingRecord.meta_connection_id == connection_id,
            MetaDataSourceBindingRecord.ad_account_id == ad_account_id,
            MetaDataSourceBindingRecord.state == "active",
        )
    ).scalar_one_or_none()
    if binding is None:
        raise AdapterResolutionError(
            f"No active binding for connection={connection_id} "
            f"account={ad_account_id}"
        )
    scenario_version_id = None
    if meta_mode in ("customer_sandbox", "controlled_mock"):
        scen = session.execute(
            select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.workspace_id == workspace_id,
                ScenarioVersionRecord.meta_mode == meta_mode,
                ScenarioVersionRecord.meta_connection_id == connection_id,
                ScenarioVersionRecord.is_default == True,
            )
        ).scalar_one_or_none()
        if scen is None:
            raise AdapterResolutionError(
                f"No default scenario for connection={connection_id}"
            )
        scenario_version_id = scen.id
        fault_config = scen.scenario_config.get("faults") if meta_mode == "controlled_mock" else None
        adapter = FakeMetaAdapter(
            session=session,
            workspace_id=workspace_id,
            meta_mode=meta_mode,
            scenario_version_id=scenario_version_id,
            fault_config=fault_config,
        )
    elif meta_mode == "internal_validation":
        if not read_only:
            raise AdapterResolutionError(
                "meta_read_only_mode: internal_validation only reads Meta; it has no write adapter",
                code="meta_read_only_mode", permanent=True,
            )
        # ADR 0009: real reads belong to the platform Workspace alone. The
        # connection could not have been created elsewhere; this refuses a
        # read anyway rather than trusting that.
        workspace = session.get(WorkspaceRecord, workspace_id)
        if workspace is None or not workspace.is_platform_workspace:
            raise AdapterResolutionError(
                "meta_platform_workspace_required: internal_validation reads Meta only in the "
                f"platform Workspace, not {workspace_id}",
                code="meta_platform_workspace_required", permanent=True,
            )
        if not binding.access_token_version_id or not binding.app_secret_version_id:
            raise AdapterResolutionError(
                f"meta_read_credential_unavailable: binding {binding.id} names no Meta access token "
                "or app secret version",
                code="meta_read_credential_unavailable", permanent=True,
            )
        adapter = build_internal_validation_adapter(
            session,
            workspace_id=workspace_id,
            access_token_version_id=binding.access_token_version_id,
            app_secret_version_id=binding.app_secret_version_id,
        )
    else:
        raise AdapterResolutionError(f"Unknown meta_mode: {meta_mode}")

    return ResolvedAdapter(
        adapter=adapter,
        binding=binding,
        access_token_version_id=binding.access_token_version_id,
        app_secret_version_id=binding.app_secret_version_id,
        scenario_version_id=scenario_version_id,
    )


def create_call_intent(
    session: Session,
    *,
    workspace_id: str,
    meta_mode: str,
    connection: MetaConnectionRecord,
    binding: MetaDataSourceBindingRecord,
    operation_id: str,
    meta_api_method: str,
    meta_api_endpoint: str,
    request_parameters: dict[str, Any],
    call_type: str,
    scenario_version_id: str | None,
    executor: str,
) -> MetaCallIntentRecord:
    from app.platform.tenancy import is_postgres
    if is_postgres(session):
        session.execute(text(
            "SELECT id FROM meta_connection_records "
            "WHERE id = :cid AND workspace_id = :wid AND meta_mode = :mode "
            "AND state = 'active' FOR SHARE"
        ), {"cid": connection.id, "wid": workspace_id, "mode": meta_mode})
        session.execute(text(
            "SELECT id FROM meta_data_source_binding_records "
            "WHERE id = :bid AND workspace_id = :wid AND meta_mode = :mode "
            "AND state = 'active' FOR SHARE"
        ), {"bid": binding.id, "wid": workspace_id, "mode": meta_mode})

    fingerprint = _compute_request_fingerprint(
        meta_api_method, meta_api_endpoint, request_parameters,
    )
    intent = MetaCallIntentRecord(
        id=new_id("mci"),
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=connection.id,
        meta_binding_id=binding.id,
        ad_account_id=binding.ad_account_id,
        access_token_version_id=binding.access_token_version_id,
        app_secret_version_id=binding.app_secret_version_id,
        operation_id=operation_id,
        meta_api_method=meta_api_method,
        meta_api_endpoint=meta_api_endpoint,
        request_parameters=request_parameters,
        request_fingerprint_hash=fingerprint,
        connection_state_at_intent=connection.state,
        binding_state_at_intent=binding.state,
        call_type=call_type,
        scenario_version_id=scenario_version_id,
        write_permitted=False,
        executor=executor,
    )
    session.add(intent)
    session.flush()
    return intent
