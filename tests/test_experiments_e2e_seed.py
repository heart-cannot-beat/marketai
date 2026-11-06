"""The Experiments browser-test seed writes what the current schema and gates need.

`web/e2e/experiments/seed_prerequisites.py` only runs against the compose
PostgreSQL stack, so a schema change can silently break it. This runs its
write path against the SQLite compatibility database (foreign keys and the
one-active-connection index are enforced there too), twice in one Workspace,
the way consecutive specs call it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.experiments.service import add_entry_v2, analyze_duplicates, create_experiment_v2
from app.meta.models import (
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    ScenarioVersionRecord,
)
from app.platform.tenancy import TenantContext


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "web" / "e2e" / "experiments" / "seed_prerequisites.py"
SPEC = importlib.util.spec_from_file_location("experiments_e2e_seed_prerequisites", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
seed_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed_module)

CTX = TenantContext(
    workspace_id="ws_e2e_exp_seed",
    role="owner",
    initiator_id="user_e2e_exp_seed",
    initiator_role="owner",
    request_id="req_e2e_exp_seed",
    trace_id="trace_e2e_exp_seed",
)
FAULT_CONFIG = {"rules": [{"object_type": "campaign", "match_ordinal": 1}]}


@pytest.fixture(scope="module")
def seeds() -> tuple[dict, dict]:
    command.upgrade(Config(str(REPOSITORY_ROOT / "alembic.ini")), "head")
    with tenant_session(CTX) as session:
        if session.get(WorkspaceRecord, CTX.workspace_id) is None:
            session.add(WorkspaceRecord(id=CTX.workspace_id, name="E2E experiments seed"))
    first = seed_module.seed_workspace(CTX, workspace_slug="e2e-exp-seed")
    second = seed_module.seed_workspace(
        CTX, workspace_slug="e2e-exp-seed", fault_config=FAULT_CONFIG,
    )
    return first, second


def test_repeated_seed_replaces_the_active_meta_connections(seeds) -> None:
    first, second = seeds
    with tenant_session(CTX) as session:
        for key in ("customerSandboxConnectionId", "controlledMockConnectionId"):
            assert session.get(MetaConnectionRecord, first[key]).state == "retired"
            assert session.get(MetaConnectionRecord, second[key]).state == "active"


@pytest.mark.parametrize(
    ("mode", "connection_key", "scenario_key", "account_key", "scenario_config"),
    [
        ("customer_sandbox", "customerSandboxConnectionId",
         "customerSandboxScenarioVersionId", "customerSandboxAdAccountId", {}),
        ("controlled_mock", "controlledMockConnectionId",
         "controlledMockScenarioVersionId", "controlledMockAdAccountId",
         {"faults": FAULT_CONFIG}),
    ],
)
def test_launch_can_resolve_the_seeded_binding_and_default_scenario(
    seeds, mode, connection_key, scenario_key, account_key, scenario_config,
) -> None:
    _, second = seeds
    connection_id = second[connection_key]
    with tenant_session(CTX) as session:
        binding = session.scalar(select(MetaDataSourceBindingRecord).where(
            MetaDataSourceBindingRecord.workspace_id == CTX.workspace_id,
            MetaDataSourceBindingRecord.meta_mode == mode,
            MetaDataSourceBindingRecord.meta_connection_id == connection_id,
            MetaDataSourceBindingRecord.ad_account_id == second[account_key],
            MetaDataSourceBindingRecord.state == "active",
        ))
        assert binding is not None
        scenario = session.get(ScenarioVersionRecord, second[scenario_key])
        assert scenario.meta_connection_id == connection_id
        assert scenario.is_default is True
        assert scenario.scenario_config == scenario_config


def test_seeded_duplicate_pair_is_the_only_duplicate_group(seeds) -> None:
    _, second = seeds
    experiment = create_experiment_v2(
        CTX,
        product_id=second["productId"],
        name=f"Seed contract {second['tag']}",
        draft_context={
            "market": "US",
            "currency": "USD",
            "placement": "feed",
            "product_version_id": second["productVersionId"],
            "offer_version_id": second["offerVersionId"],
            "budget": {"daily_amount": "5.00", "total_amount": "150.00"},
            "schedule": {
                "start_at": "2026-09-01T00:00:00+00:00",
                "end_at": "2026-12-01T00:00:00+00:00",
                "timezone": "UTC",
            },
            "attribution": {"click_window_days": 7, "view_window_days": 1},
            "statistical_plan": {
                "primary_metric": "ctr",
                "ctr_threshold": "0.01",
                "alpha": "0.05",
                "bootstrap_samples": 2000,
            },
        },
    )
    revision = experiment["revision"]
    for asset_version_id in second["assetVersionIds"]:
        revision = add_entry_v2(
            CTX, experiment["id"], asset_version_id=asset_version_id,
            expected_revision=revision,
        )["revision"]

    analysis = analyze_duplicates(CTX, experiment["id"])

    assert analysis["has_duplicates"] is True
    [group] = analysis["duplicate_groups"]
    assert group["concept_key_id"] == second["sharedConceptKeyId"]
    assert sorted(member["asset_version_id"] for member in group["members"]) == sorted(
        second["duplicatePairAssetVersionIds"]
    )
