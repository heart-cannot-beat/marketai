"""Mode-driven adapter selection and credential resolution (spec §4).

Resolution sequence:
1. Determine adapter from meta_mode
2. Resolve binding metadata (composite FK query)
3. For internal_validation: dereference credential versions via KMS
4. Construct ephemeral credential bundle (never persisted)

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
from app.adapters.persistence.models import new_id, utcnow
from app.meta.models import (
    MetaCallIntentRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)


class AdapterResolutionError(RuntimeError):
    pass


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
) -> ResolvedAdapter:
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
        raise AdapterResolutionError(
            "internal_validation adapter requires HttpMetaReadAdapter (not yet in scope)"
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
