"""Shared fixtures for the scheduled Meta observation sync tests (#107).

The SQLite suite (``tests/test_meta_observation_sync.py``) and the
PostgreSQL one (``tests/test_meta_observation_sync_pg.py``) create the same
connection, binding and daily contract through the API, and seed the same
sandbox ads, so both prove the same path.
"""
from __future__ import annotations

from dataclasses import replace

from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.persistence.database import tenant_session
from app.meta.models import ScenarioVersionRecord
from app.platform.tenancy import TenantContext, system_context

TIMEZONE = "America/Los_Angeles"
AD_ACCOUNT = "act_acme_charge_sandbox"
DAILY_FIELDS = ["impressions", "inline_link_clicks", "spend", "actions", "action_values"]
ATTRIBUTION = {"click": "7d", "view": "1d"}


def daily_contract_body(connection_id: str, meta_mode: str, *, timezone: str = TIMEZONE,
                        contract_type: str = "daily") -> dict:
    return {
        "connection_id": connection_id,
        "meta_mode": meta_mode,
        "contract_type": contract_type,
        "ad_account_id": AD_ACCOUNT,
        "fields": DAILY_FIELDS,
        "attribution_window": ATTRIBUTION,
        "breakdowns": [],
        "time_window": {"granularity": "daily", "lookback_days": 14},
        "local_timezone": timezone,
    }


def create_through_api(test, client, meta_mode: str = "customer_sandbox", *,
                       timezone: str = TIMEZONE, contract_type: str = "daily") -> tuple[dict, dict]:
    """Connection, binding and contract generation, each through its route."""
    connection = client.post(
        "/api/v1/meta/connections", json={"ad_account_id": AD_ACCOUNT, "meta_mode": meta_mode},
    )
    test.assertEqual(connection.status_code, 201, connection.text)
    connection = connection.json()
    binding = client.post(
        f"/api/v1/meta/connections/{connection['id']}/bindings",
        json={"ad_account_id": AD_ACCOUNT, "meta_mode": meta_mode},
    )
    test.assertEqual(binding.status_code, 201, binding.text)
    generation = client.post(
        "/api/v1/meta/contracts",
        json=daily_contract_body(connection["id"], meta_mode, timezone=timezone, contract_type=contract_type),
    )
    test.assertEqual(generation.status_code, 201, generation.text)
    return connection, generation.json()


def worker_context(workspace_id: str, meta_mode: str, *, authorization_epoch: int = 0) -> TenantContext:
    return replace(
        system_context(workspace_id, initiator_id=None, authorization_epoch=authorization_epoch),
        meta_mode=meta_mode,
    )


def seed_sandbox_ads(context: TenantContext, connection_id: str, count: int) -> list[str]:
    """What a sandbox Launch leaves behind: ``count`` ads in the default scenario."""
    with tenant_session(context) as session:
        scenario = session.query(ScenarioVersionRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode,
            meta_connection_id=connection_id, is_default=True,
        ).one()
        adapter = FakeMetaAdapter(session, context.workspace_id, context.meta_mode, scenario.id)
        return [
            adapter.ensure_object("ad", f"acme-charge-ad-{index}", {"name": f"Acme Charge ad {index}"})
            .remote_object_id
            for index in range(count)
        ]
