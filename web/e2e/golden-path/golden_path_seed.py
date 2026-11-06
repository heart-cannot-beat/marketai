"""Seed platform and test infrastructure for the golden-path E2E journey
(issue #60).

This script seeds ONLY platform/test infrastructure — controlled-Mock
scenarios, sandbox connections, metric clocks, and published platform
policies.  It MUST NOT create Products, Assets, reviews, Winners,
batches, enrollments, advice, drafts, or any other domain business
objects.  The two exceptions are stand-ins for product surfaces that do
not exist yet, documented on _seed_scale_infrastructure: the review
approval of the Winner-market MarketAssetBinding and the landing_page
LibraryVersion behind the Winner's frozen landing page evidence.

Subcommands:

  seed-platform-infrastructure
    Given a workspace_id, creates the controlled-Mock MetaConnection
    and ScenarioVersion.  Emits JSON with all infrastructure IDs the
    journey's later steps need.

  seed-localization-infrastructure
    Given a workspace_id and an asset_version_id (from a confirmed
    Winner), creates the market policy versions (including one for the
    Winner's own experiment market), truth resolution policy, data
    source version, and claim inventory.

  seed-scale-infrastructure
    Given a workspace_id and a decision_id (confirmed Winner), approves
    the Winner-market MarketAssetBinding the journey's Localization batch
    published and creates the Scale DeliveryTarget, activation evidence,
    metric-semantic contract set, and policy version.

  seed-experiment-metrics
    Drives experiment metrics through the real sync/projection
    pipeline using the governed evidence pages and the production
    _upsert_metric projection function.

Required environment:
  - E2E_GOLDEN_PATH_ALLOW_MUTATIONS=1
  - E2E_GOLDEN_PATH_WORKSPACE_ID
  - E2E_OWNER_USER_ID
  - MARKETAI_DATABASE_URL
  - MARKETAI_TENANT_CONTEXT_SECRET
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

REQUIRED_ENVIRONMENT = (
    "E2E_GOLDEN_PATH_ALLOW_MUTATIONS",
    "E2E_GOLDEN_PATH_WORKSPACE_ID",
    "E2E_OWNER_USER_ID",
    "MARKETAI_DATABASE_URL",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)
MUTATION_OPT_IN = "E2E_GOLDEN_PATH_ALLOW_MUTATIONS"
SAFE_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
EXPECTED_DATABASE = "marketai_domain"


def _database_url_problems(name: str, database_url: str) -> list[str]:
    parsed = urlsplit(database_url)
    problems: list[str] = []
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        problems.append(f"{name} must use PostgreSQL")
    host = (parsed.hostname or "").lower()
    if host not in SAFE_DATABASE_HOSTS:
        problems.append(f"{name} host is not local/compose: {host or '<missing>'}")
    database = parsed.path.removeprefix("/")
    if database != EXPECTED_DATABASE:
        problems.append(f"{name} must target {EXPECTED_DATABASE}")
    username = (parsed.username or "").lower()
    if username != "owner" and not username.endswith("_owner"):
        problems.append(f"{name} must use an owner role")
    return problems


def _required_environment() -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENVIRONMENT}
    missing = [name for name, value in values.items() if not value]
    problems: list[str] = []
    if missing:
        problems.append("missing: " + ", ".join(missing))

    opt_in = values.get(MUTATION_OPT_IN, "")
    if opt_in and opt_in != "1":
        problems.append(f"{MUTATION_OPT_IN} must be exactly 1")

    db_url = values.get("MARKETAI_DATABASE_URL", "")
    if db_url:
        problems.extend(_database_url_problems("MARKETAI_DATABASE_URL", db_url))

    if problems:
        raise RuntimeError("Golden-path E2E preflight failed: " + "; ".join(problems))
    return values


def _tenant_context(env: dict[str, str], *, meta_mode: str | None = None):
    from app.platform.tenancy import TenantContext

    return TenantContext(
        workspace_id=env["E2E_GOLDEN_PATH_WORKSPACE_ID"],
        role="owner",
        initiator_id=env["E2E_OWNER_USER_ID"],
        initiator_role="owner",
        executor=f"user:{env['E2E_OWNER_USER_ID']}",
        request_id="e2e-golden-path-seed",
        trace_id="e2e-golden-path-seed",
        authorization_epoch=0,
        meta_mode=meta_mode,
    )


# ---------------------------------------------------------------------------
# seed-platform-infrastructure
# ---------------------------------------------------------------------------

def _ensure_admission_infrastructure(session, workspace_id: str, actor_id: str) -> None:
    """Create the durable rows required by paid domain command admission."""
    from app.adapters.persistence.models import utcnow
    from app.entitlements.cost_models import SpendGuardrailRecord
    from app.entitlements.models import EntitlementProjectionRecord

    now = utcnow()
    projection = session.get(EntitlementProjectionRecord, workspace_id)
    if projection is None:
        session.add(
            EntitlementProjectionRecord(
                workspace_id=workspace_id,
                entitlement_version=0,
                lease_sequence=1,
                projection_version=1,
                schema_version=2,
                period_start=now,
                period_end=now + timedelta(days=30),
                resolved_entitlements_json={},
                semantic_hash=f"gp-entitlement-{workspace_id}",
                issued_at=now,
                valid_until=now + timedelta(days=30),
                applied_at=now,
            )
        )
    guardrail = session.get(SpendGuardrailRecord, workspace_id)
    if guardrail is None:
        session.add(
            SpendGuardrailRecord(
                workspace_id=workspace_id,
                revision=1,
                budget_currency="USD",
                per_operation_limit=Decimal("100"),
                rolling_24h_limit=Decimal("500"),
                period_limit=Decimal("5000"),
                outstanding_exposure_limit=Decimal("1000"),
                platform_policy_version=1,
                created_at=now,
                updated_at=now,
                updated_by=actor_id,
            )
        )

def _seed_platform_infrastructure(env: dict[str, str], args: argparse.Namespace) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import new_id
    from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
    from app.platform.canonical import canonical_hash

    workspace_id = args.workspace_id
    ctx = _tenant_context(env)
    tag = f"gp{uuid4().hex[:6]}"
    target_meta_mode = "controlled_mock"
    sandbox_meta_mode = "customer_sandbox"

    with tenant_session(ctx) as session:
        _ensure_admission_infrastructure(
            session, workspace_id, env["E2E_OWNER_USER_ID"]
        )

        mock_connection = MetaConnectionRecord(
            id=f"mcon_{tag}_mock",
            workspace_id=workspace_id,
            meta_mode=target_meta_mode,
            ad_account_id=f"act_{tag}_mock",
            state="active",
        )
        session.add(mock_connection)
        session.flush()

        mock_scenario_config: dict = {}
        mock_scenario = ScenarioVersionRecord(
            id=f"scen_{tag}_mock",
            workspace_id=workspace_id,
            meta_mode=target_meta_mode,
            meta_connection_id=mock_connection.id,
            scenario_config=mock_scenario_config,
            scenario_hash=canonical_hash(mock_scenario_config),
            is_default=True,
        )
        session.add(mock_scenario)
        session.flush()

        sandbox_connection = MetaConnectionRecord(
            id=f"mcon_{tag}_sandbox",
            workspace_id=workspace_id,
            meta_mode=sandbox_meta_mode,
            ad_account_id=f"act_{tag}_sandbox",
            state="active",
        )
        session.add(sandbox_connection)
        session.flush()

        sandbox_scenario_config: dict = {}
        sandbox_scenario = ScenarioVersionRecord(
            id=f"scen_{tag}_sandbox",
            workspace_id=workspace_id,
            meta_mode=sandbox_meta_mode,
            meta_connection_id=sandbox_connection.id,
            scenario_config=sandbox_scenario_config,
            scenario_hash=canonical_hash(sandbox_scenario_config),
            is_default=True,
        )
        session.add(sandbox_scenario)
        session.flush()

    # Mirror the platform bootstrap (app/meta/platform_api.py): a Meta
    # connection is provisioned together with its active Data Source Binding.
    # Launch previews resolve that (connection, ad account) binding under the
    # request's meta_mode and refuse to plan without one, so the binding is
    # created through the product service under a mode-bound context.
    from app.meta.connections import create_binding

    binding_ids: dict[str, str] = {}
    for connection, mode in (
        (mock_connection, target_meta_mode),
        (sandbox_connection, sandbox_meta_mode),
    ):
        mode_ctx = _tenant_context(env, meta_mode=mode)
        with tenant_session(mode_ctx) as session:
            binding = create_binding(
                session,
                context=mode_ctx,
                connection_id=connection.id,
                ad_account_id=connection.ad_account_id,
            )
            binding_ids[mode] = binding.id

    return {
        "controlledMockConnectionId": mock_connection.id,
        "controlledMockScenarioVersionId": mock_scenario.id,
        "controlledMockBindingId": binding_ids[target_meta_mode],
        "customerSandboxConnectionId": sandbox_connection.id,
        "customerSandboxAdAccountId": sandbox_connection.ad_account_id,
        "customerSandboxScenarioVersionId": sandbox_scenario.id,
        "customerSandboxBindingId": binding_ids[sandbox_meta_mode],
    }


# ---------------------------------------------------------------------------
# seed-localization-infrastructure
# ---------------------------------------------------------------------------

def _winner_experiment_market(session, workspace_id: str, asset_version_id: str) -> str:
    """The market frozen on the confirmed Winner's own ExperimentVersion.

    Scale enrollment re-runs the full Winner eligibility gate, whose check
    9 (app/experiments/winner_eligibility.py) requires an experiment-ready
    MarketAssetBinding for exactly this AssetVersion, the Winner's market
    and its Offer.  Localization is the only producer of bindings
    (app/localization/worker.py finalization -> binding.publish_binding),
    so the journey's batch must also target the Winner's own market.
    """
    from sqlalchemy import select

    from app.adapters.persistence.models import (
        ExperimentDecisionRecord,
        ExperimentVersionCandidateRecord,
        ExperimentVersionRecord,
    )

    markets = set(
        session.execute(
            select(ExperimentVersionRecord.market)
            .join(
                ExperimentDecisionRecord,
                (ExperimentDecisionRecord.workspace_id == ExperimentVersionRecord.workspace_id)
                & (ExperimentDecisionRecord.experiment_version_id == ExperimentVersionRecord.id),
            )
            .join(
                ExperimentVersionCandidateRecord,
                (ExperimentVersionCandidateRecord.workspace_id == ExperimentDecisionRecord.workspace_id)
                & (
                    ExperimentVersionCandidateRecord.id
                    == ExperimentDecisionRecord.experiment_version_candidate_id
                ),
            )
            .where(
                ExperimentDecisionRecord.workspace_id == workspace_id,
                ExperimentDecisionRecord.verdict == "win",
                ExperimentDecisionRecord.confirmation_id.is_not(None),
                ExperimentVersionCandidateRecord.asset_version_id == asset_version_id,
            )
        ).scalars()
    )
    if len(markets) != 1:
        raise RuntimeError(
            f"AssetVersion {asset_version_id!r} must have exactly one confirmed "
            f"Winner market, found {sorted(markets)!r}"
        )
    return markets.pop()


def _seed_localization_infrastructure(env: dict[str, str], args: argparse.Namespace) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.localization.claims import create_inventory_from_analysis
    from tests.support.scale_candidate_fixtures import (
        _make_market_policy_version as _reusable_market_policy_version,
    )
    from tests.test_localization_batch import (
        _make_market_policy_version,
        _make_version,
    )

    ctx = _tenant_context(env)
    workspace_id = ctx.workspace_id
    asset_version_id = args.asset_version_id
    tag = f"gploc{uuid4().hex[:6]}"

    with tenant_session(ctx) as session:
        _ensure_admission_infrastructure(
            session, workspace_id, env["E2E_OWNER_USER_ID"]
        )
        market_specs = (
            ("GB", "ready", "complete_with_discount", False),
            ("CA", "degraded", "standard_9_market", True),
            ("AU", "unavailable", "unavailable", False),
        )
        markets = []
        for country, readiness, scenario, fails_qc in market_specs:
            market = f"{country}{uuid4().hex[:6].upper()}"
            mp = _make_market_policy_version(session, market)
            if fails_qc:
                mp.qc_rules_json = {"forbidden_terms": ["durable"]}
            markets.append({
                "market": market,
                "country": country,
                "expectedReadiness": readiness,
                "mockScenario": scenario,
                "marketPolicyVersionId": mp.id,
                "failsLockedCopyQc": fails_qc,
                "isWinnerMarket": False,
            })

        # The Winner's own market id is fixed ("US" in the journey), and
        # market policies are platform-global (uq_mp_market_id), so reuse an
        # effective policy left by an earlier run instead of colliding.
        winner_market = _winner_experiment_market(session, workspace_id, asset_version_id)
        winner_policy = _reusable_market_policy_version(session, winner_market)
        markets.append({
            "market": winner_market,
            "country": winner_policy.country,
            "expectedReadiness": "ready",
            "mockScenario": "complete_with_discount",
            "marketPolicyVersionId": winner_policy.id,
            "failsLockedCopyQc": False,
            "isWinnerMarket": True,
        })

    # Same versions as the Localization E2E seed (web/e2e/localization/
    # seed_prerequisites.py). The Localization truth engine reads
    # rules[].source_priority as adapter kinds ("mock"), while Library
    # publish validation resolves each entry as a DataSource version id and
    # rejects "mock" as dependency_not_found -- so a policy published through
    # the Library service could never drive Localization.
    with tenant_session(ctx) as session:
        truth_policy_id = _make_version(
            session, workspace_id, "truth_resolution_policy", tag,
            content={
                "rules": [
                    {"fact_type": "price", "source_priority": ["mock"], "on_failure": "block", "max_staleness_seconds": 86400},
                    {"fact_type": "availability", "source_priority": ["mock"], "on_failure": "block", "max_staleness_seconds": 86400},
                    {"fact_type": "discount", "source_priority": ["mock"], "on_failure": "skip", "max_staleness_seconds": 86400},
                ],
            },
        )
        data_source_id = _make_version(
            session, workspace_id, "data_source", tag,
            content={
                "adapter_kind": "mock",
                "execution_mode": "mock",
                "config": {
                    "mock_scenario": "standard_9_market",
                    "mock_scenarios_by_country": {m["country"]: m["mockScenario"] for m in markets},
                },
            },
        )
        inventory = create_inventory_from_analysis(
            session, ctx,
            asset_version_id=asset_version_id,
            # The control room starts preparation with the fixed recipe
            # 'localization-analysis-v1' (LocalizationPreparation.tsx) and
            # plan_inventories() reuses only an Inventory recorded for that
            # exact recipe; any other hash forces a fresh analysis instead.
            analysis_recipe_hash="localization-analysis-v1",
            claims=[{
                "claim_type": "headline",
                "scope_key": f"headline:{tag}",
                "ocr_text": "A durable source headline",
                "confidence": 0.99,
                "risk": "low",
                "conflicting": False,
                "market_sensitive": False,
            }],
            provider="controlled-mock-vision",
            model="localization-analysis-v1",
            rule_version="claims-rule-v1",
            analysis_execution_mode="mock",
            adapter_version="mock-v1",
            mock_scenario="default",
            mock_version="v1",
        )

    return {
        "markets": markets,
        "truthPolicyVersionId": truth_policy_id,
        "dataSourceVersionId": data_source_id,
        "inventoryId": inventory.id,
    }


# ---------------------------------------------------------------------------
# seed-scale-infrastructure
# ---------------------------------------------------------------------------

def _scale_suite_seed_module():
    """The Scale E2E suite's seed module (web/e2e/scale/seed_prerequisites.py).

    Its `advance-manifest` command is what the journey drives Scale
    evidence through, so the activation evidence it expects is seeded by
    that same module rather than re-implemented here.
    """
    import importlib.util

    path = REPOSITORY_ROOT / "web" / "e2e" / "scale" / "seed_prerequisites.py"
    spec = importlib.util.spec_from_file_location("scale_e2e_seed_prerequisites", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_scale_infrastructure(env: dict[str, str], args: argparse.Namespace) -> dict:
    """Seed Scale infrastructure for the journey's own confirmed Winner.

    Enrollment re-runs the complete lineage gate
    (app/scale/enrollment_lineage.py): all eleven Winner eligibility checks
    plus a field-for-field match between the Winner's frozen
    ExperimentVersion and the DeliveryTarget -- AssetVersion, market,
    placement, Offer, landing page and attribution semantics.  Mirroring
    tests/support/scale_candidate_fixtures.py::build_ready_winner_and_target,
    this seeds only what no product surface creates yet:

      * review approval of the Winner-market MarketAssetBinding that the
        journey's Localization batch published -- bindings always start
        pending_review and no binding review command exists;
      * a landing_page LibraryVersion keyed by the frozen
        landing_page_evidence_hash -- no Landing Page entity ships, the
        lineage gate uses that hash as landing_page_version_id and
        scale_delivery_targets references library_versions;
      * the curated-scenario DeliveryTarget with its delivery_configuration
        contract generation, activation evidence and contract set;
      * a published ScalePolicyVersion.
    """
    import hashlib

    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        ExperimentDecisionRecord,
        ExperimentVersionCandidateRecord,
        ExperimentVersionRecord,
        LibraryEntryRecord,
        LibraryVersionRecord,
        MarketAssetBindingRecord,
        new_id,
    )
    from app.experiments.winner_eligibility import _versioned_hash
    from app.localization.binding import _append_event, _snapshot
    from app.meta.models import (
        MetaConnectionRecord,
        ObservationContractGenerationRecord,
        ScenarioVersionRecord,
    )
    from app.meta.observation import create_contract_generation
    from app.scale.delivery_targets import DeliveryTargetRequest, create_delivery_target
    from app.scale.models import ScalePolicyVersionRecord
    from tests.support.scale_candidate_fixtures import seed_activation_evidence_prereqs

    ctx = _tenant_context(env)
    owner_user_id = env["E2E_OWNER_USER_ID"]
    sandbox_mode = "customer_sandbox"
    target_ctx = _tenant_context(env, meta_mode=sandbox_mode)
    decision_id = args.decision_id
    tag = f"gpsc{uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)

    with tenant_session(ctx) as session:
        decision = session.scalar(
            select(ExperimentDecisionRecord).where(
                ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
                ExperimentDecisionRecord.id == decision_id,
            )
        )
        if decision is None:
            raise RuntimeError(f"Decision {decision_id!r} not found")
        if decision.verdict != "win":
            raise RuntimeError(f"Decision {decision_id!r} verdict is {decision.verdict!r}")
        candidate = session.get(
            ExperimentVersionCandidateRecord, decision.experiment_version_candidate_id
        )
        version = session.get(ExperimentVersionRecord, decision.experiment_version_id)

        # The Winner's frozen delivery slot, resolved exactly as
        # validate_winner_eligibility() check 8 resolves it.
        asset_version_id = candidate.asset_version_id
        market_code = version.market
        placement = version.placement
        offer_version_id = version.offer_version_id
        landing_page_version_id = version.landing_page_evidence_hash
        attribution_window = {
            "click_window_days": version.attribution_click_window_days,
            "view_window_days": version.attribution_view_window_days,
        }
        attribution_semantics_hash = _versioned_hash(
            "attribution-semantics-v1", attribution_window,
        )

        binding = session.scalar(
            select(MarketAssetBindingRecord).where(
                MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
                MarketAssetBindingRecord.source_asset_version_id == asset_version_id,
                MarketAssetBindingRecord.target_market == market_code,
                MarketAssetBindingRecord.offer_version_id == offer_version_id,
                MarketAssetBindingRecord.active.is_(True),
            )
        )
        if binding is None:
            raise RuntimeError(
                "The journey's Localization batch published no active "
                f"MarketAssetBinding for Winner AssetVersion {asset_version_id!r} "
                f"in its own market {market_code!r}"
            )
        if binding.truth_freshness != "current":
            raise RuntimeError(
                f"MarketAssetBinding {binding.id} Truth is {binding.truth_freshness!r}"
            )
        if binding.review_state != "approved" or not binding.ready_for_experiment:
            previous_state = _snapshot(binding)
            binding.review_state = "approved"
            # binding.py derives readiness as review_state == 'approved'
            # AND truth_freshness == 'current'.
            binding.ready_for_experiment = True
            binding.revision += 1
            binding.updated_at = now
            session.flush()
            _append_event(
                session, ctx, binding=binding, event_type="review_approved",
                dependency_change_id=None, previous_state=previous_state,
                new_state=_snapshot(binding), actor_id=owner_user_id, now=now,
            )

        if session.get(LibraryVersionRecord, landing_page_version_id) is None:
            landing_entry = LibraryEntryRecord(
                id=new_id("le"),
                workspace_id=ctx.workspace_id,
                entry_type="landing_page",
                display_name=f"Landing page {landing_page_version_id[:12]}",
            )
            session.add(landing_entry)
            session.flush()
            session.add(LibraryVersionRecord(
                id=landing_page_version_id,
                workspace_id=ctx.workspace_id,
                entry_id=landing_entry.id,
                entry_type="landing_page",
                version_no=1,
                content_json={
                    "source": "experiment_landing_page_evidence",
                    "landing_page_evidence_hash": landing_page_version_id,
                    "offer_version_id": offer_version_id,
                },
                content_hash=landing_page_version_id,
                published_by="golden-path-setup",
            ))
            session.flush()
        binding_id = binding.id

    with tenant_session(target_ctx) as session:
        connection = session.scalar(
            select(MetaConnectionRecord)
            .where(
                MetaConnectionRecord.workspace_id == ctx.workspace_id,
                MetaConnectionRecord.meta_mode == sandbox_mode,
                MetaConnectionRecord.state == "active",
            )
            .order_by(MetaConnectionRecord.id)
        )
        if connection is None:
            raise RuntimeError(f"No active {sandbox_mode} MetaConnection found")
        scenario = session.scalar(
            select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.workspace_id == ctx.workspace_id,
                ScenarioVersionRecord.meta_mode == sandbox_mode,
                ScenarioVersionRecord.meta_connection_id == connection.id,
                ScenarioVersionRecord.is_default.is_(True),
            )
        )
        if scenario is None:
            raise RuntimeError(f"No default ScenarioVersion found for {sandbox_mode}")

        generation = session.scalar(
            select(ObservationContractGenerationRecord).where(
                ObservationContractGenerationRecord.workspace_id == ctx.workspace_id,
                ObservationContractGenerationRecord.meta_mode == sandbox_mode,
                ObservationContractGenerationRecord.meta_connection_id == connection.id,
                ObservationContractGenerationRecord.contract_type == "delivery_configuration",
                ObservationContractGenerationRecord.state == "active",
            )
        )
        if generation is None:
            generation = create_contract_generation(
                session,
                workspace_id=ctx.workspace_id,
                meta_mode=sandbox_mode,
                connection_id=connection.id,
                contract_type="delivery_configuration",
                ad_account_id=connection.ad_account_id,
                fields=[
                    "effective_status", "daily_budget", "optimization_goal",
                    "bid_strategy", "attribution_setting",
                ],
                attribution_window=attribution_window,
                breakdowns=[],
                time_window={"kind": "daily"},
                local_timezone="UTC",
            )

        target = create_delivery_target(
            session,
            target_ctx,
            DeliveryTargetRequest(
                meta_connection_id=connection.id,
                ad_account_id=connection.ad_account_id,
                # Only controlled_mock targets are scenario-scoped
                # (ck_sdt_scenario_biconditional).
                scenario_scope_key="__no_scenario__",
                campaign_remote_id=f"gp_scale_campaign_{tag}",
                adset_remote_id=f"gp_scale_adset_{tag}",
                ad_remote_id=f"gp_scale_ad_{tag}",
                ad_creative_id=f"gp_scale_creative_{tag}",
                creative_asset_version_id=asset_version_id,
                creative_binding_evidence_hash=hashlib.sha256(
                    f"{binding_id}:{asset_version_id}".encode()
                ).hexdigest(),
                market_code=market_code,
                placement=placement,
                attribution_setting=attribution_semantics_hash,
                contract_generation_id=generation.id,
                offer_version_id=offer_version_id,
                landing_page_version_id=landing_page_version_id,
                source_type="curated_scenario",
                source_manifest={
                    "schema_version": 1,
                    "source_type": "curated_scenario",
                    "scenario_version_id": scenario.id,
                    "winner_decision_id": decision_id,
                },
                observed_at=now,
                expires_at=now + timedelta(days=90),
                scenario_version_id=scenario.id,
            ),
        )
        delivery_target_id = target.id
        contract_generation_id = generation.id

    # Attestation plus the delivery_configuration contract-set member that
    # activation resolves, then the daily/exact_window members advance-manifest
    # needs -- all keyed to the target's own contract generation.
    seed_activation_evidence_prereqs(
        target_ctx, delivery_target_id=delivery_target_id, meta_mode=sandbox_mode, tag=tag,
    )
    scale_suite = _scale_suite_seed_module()
    with tenant_session(target_ctx) as session:
        contract_set_id = scale_suite._seed_full_activation_evidence(
            session, target_ctx, delivery_target_id=delivery_target_id,
            meta_mode=sandbox_mode, tag=tag,
        )

    with tenant_session(target_ctx) as session:
        policy = ScalePolicyVersionRecord(
            id=f"spol_{tag}",
            algorithm_version="beta-v1",
            canonical_schema_version="scale-snapshot-v1",
            decimal_precision=28,
            decimal_rounding="round_half_even",
            z_value=Decimal("1.96"),
            evidence_floor_minimum_spend=Decimal("50"),
            evidence_floor_minimum_days=14,
            evidence_floor_minimum_conversions=1,
            activity_floor_minimum_impressions=500,
            sample_floor_minimum_clicks=5,
            spend_floor_minimum_daily_spend=Decimal("5"),
            freshness_floor_maximum_lag_hours=48,
            signal_thresholds={
                "confidence_level": "0.95",
                "confidence_z": "1.96",
                "frequency_level_threshold": "3",
                "frequency_growth_threshold": "0.2",
                "ctr_decline_threshold": "0.2",
                "ctr_stability_tolerance": "0.05",
                "cvr_decline_threshold": "0.2",
                "cvr_stability_tolerance": "0.05",
                "cpm_increase_threshold": "0.2",
                "delivery_decline_threshold": "0.2",
                "cpa_worsening_threshold": "0.2",
                "roas_worsening_threshold": "0.2",
                "bootstrap_lower_percentile": "0.025",
                "bootstrap_upper_percentile": "0.975",
                "breakdown_min_spend_share": "0.05",
                "active_day_floor": 3,
                "consecutive_composite_fatigue_hits_required": 2,
                "consecutive_healthy_evaluations_required": 2,
                "breakdown_min_sample_impressions": 500,
                "breakdown_min_sample_clicks": 10,
                "breakdown_min_sample_purchases": 2,
                "quality_warning_permitted_codes": ["freshness_slo_exceeded"],
                "streak_reset_reason_codes": ["policy_version_changed"],
                "advice_construction_parameters": [],
            },
            streak_rules={},
            bootstrap_prng_algorithm="bootstrap-prng-v1",
            bootstrap_draw_count=200,
            bootstrap_valid_draw_ratio=Decimal("0.80"),
            bootstrap_percentile_method="nearest_rank",
            bootstrap_draw_order="sequential",
            breakdown_families={"age_gender": {"breakdowns": ["age", "gender"]}},
            breakdown_sample_gate_minimum=100,
            breakdown_share_gate_minimum_ratio=Decimal("0.05"),
            bh_fdr_alpha=Decimal("0.05"),
            advice_severity_thresholds={"fatigue": "warning", "quality_gate": "critical"},
            notification_cooldown_hours=24,
            conversion_safe_values={"currency": "USD"},
            mode_permissions={"customer_sandbox": True, "controlled_mock": False},
            is_published=True,
            published_at=now,
            created_by=owner_user_id,
        )
        session.add(policy)
        session.flush()
        policy_version_id = policy.id

    return {
        "deliveryTargetId": delivery_target_id,
        "policyVersionId": policy_version_id,
        "contractGenerationId": contract_generation_id,
        "contractSetId": contract_set_id,
        "marketAssetBindingId": binding_id,
        "marketCode": market_code,
    }


# ---------------------------------------------------------------------------
# seed-experiment-metrics
# ---------------------------------------------------------------------------

def _seed_experiment_metrics(env: dict[str, str], args: argparse.Namespace) -> dict:
    """Drive experiment metrics through the governed sync pipeline.

    Uses create_sync_run → record_page → complete_sync_run from
    app.meta.sync_pipeline, writing raw metric data as compressed
    evidence pages — the same path a real observation sync takes.

    Each evidence page gets FK-valid MetaCallIntentRecord +
    MetaCallOutcomeRecord rows.  After the sync run completes, the
    function projects the raw data into AdDailyMetricRecord so the
    experiment judgement has rows to read.
    """
    import hashlib
    import zlib
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        ExperimentAdBindingRecord,
        ExperimentEntryRecord,
        MetaAccountRecord,
        MetaSyncRunRecord,
        new_id,
        OperationRecord,
        utcnow,
    )
    from app.meta.models import (
        MetaCallIntentRecord,
        MetaCallOutcomeRecord,
        MetaConnectionRecord,
        MetaDataSourceBindingRecord,
        ObservationContractGenerationRecord,
        ScenarioVersionRecord,
    )
    from app.meta.sync_pipeline import create_sync_run, record_page, complete_sync_run
    from app.meta.service import _upsert_metric

    ctx = _tenant_context(env)
    workspace_id = ctx.workspace_id
    experiment_id = args.experiment_id
    days = args.days
    day_offset = args.day_offset
    candidate_metrics = json.loads(args.candidate_metrics_json)
    sandbox_mode = "customer_sandbox"
    executor = f"user:{env['E2E_OWNER_USER_ID']}"

    today = date.today() - timedelta(days=day_offset)
    total_rows = 0
    projected_candidates = 0

    with tenant_session(ctx) as session:
        sandbox_connection = (
            session.query(MetaConnectionRecord)
            .filter_by(workspace_id=workspace_id, meta_mode=sandbox_mode, state="active")
            .first()
        )
        if sandbox_connection is None:
            raise RuntimeError(f"No active {sandbox_mode} MetaConnection found")

        sandbox_scenario = (
            session.query(ScenarioVersionRecord)
            .filter_by(
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                meta_connection_id=sandbox_connection.id,
                is_default=True,
            )
            .first()
        )
        if sandbox_scenario is None:
            raise RuntimeError(f"No default ScenarioVersion for {sandbox_mode}")

        binding = (
            session.query(MetaDataSourceBindingRecord)
            .filter_by(
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                meta_connection_id=sandbox_connection.id,
                state="active",
            )
            .first()
        )
        if binding is None:
            binding = MetaDataSourceBindingRecord(
                id=new_id("mdsb"),
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                meta_connection_id=sandbox_connection.id,
                ad_account_id=sandbox_connection.ad_account_id,
                state="active",
            )
            session.add(binding)
            session.flush()

        experiment_binding = (
            session.query(ExperimentAdBindingRecord)
            .join(
                ExperimentEntryRecord,
                ExperimentAdBindingRecord.experiment_entry_id == ExperimentEntryRecord.id,
            )
            .filter(
                ExperimentAdBindingRecord.workspace_id == workspace_id,
                ExperimentEntryRecord.workspace_id == workspace_id,
                ExperimentEntryRecord.experiment_id == experiment_id,
            )
            .order_by(ExperimentEntryRecord.position)
            .first()
        )
        experiment_ad_ids: dict[str, str] = {}
        for bound_ad, entry in (
            session.query(ExperimentAdBindingRecord, ExperimentEntryRecord)
            .join(
                ExperimentEntryRecord,
                ExperimentAdBindingRecord.experiment_entry_id == ExperimentEntryRecord.id,
            )
            .filter(
                ExperimentAdBindingRecord.workspace_id == workspace_id,
                ExperimentEntryRecord.workspace_id == workspace_id,
                ExperimentEntryRecord.experiment_id == experiment_id,
            )
            .all()
        ):
            # The browser journey supplies asset-version refs, while launch
            # binds the corresponding frozen candidate IDs.  Metrics must use
            # the exact bound ad_id that the experiment judge joins on.
            experiment_ad_ids[entry.asset_version_id] = bound_ad.ad_id
            if bound_ad.experiment_version_candidate_id:
                experiment_ad_ids[bound_ad.experiment_version_candidate_id] = bound_ad.ad_id
        projection_ad_account_id = (
            experiment_binding.ad_account_id
            if experiment_binding is not None
            else sandbox_connection.ad_account_id
        )

        ocg = (
            session.query(ObservationContractGenerationRecord)
            .filter_by(
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                meta_connection_id=sandbox_connection.id,
                state="active",
            )
            .first()
        )
        contract_hash = hashlib.sha256(
            f"gp-metrics-contract-{experiment_id}".encode()
        ).hexdigest()
        if ocg is None:
            ocg = ObservationContractGenerationRecord(
                id=new_id("ocg"),
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                meta_connection_id=sandbox_connection.id,
                contract_type="experiment_metrics",
                generation_number=1,
                ad_account_id=sandbox_connection.ad_account_id,
                fields=["spend", "impressions", "inline_link_clicks", "actions"],
                attribution_window={"default_attribution_setting": "7d_click"},
                breakdowns=[],
                time_window={"kind": "daily"},
                contract_hash=contract_hash,
                local_timezone="UTC",
                state="active",
            )
            session.add(ocg)
            session.flush()

        operation_id = new_id("op")
        operation = OperationRecord(
            id=operation_id,
            workspace_id=workspace_id,
            initiator_id=env["E2E_OWNER_USER_ID"],
            command_type="metrics.controlled_sync",
            state="succeeded",
            semantic_key_hash=hashlib.sha256(
                f"metrics-sync-{experiment_id}-{uuid4().hex[:8]}".encode()
            ).hexdigest(),
        )
        session.add(operation)
        session.flush()

        occurrence_id = f"gp-metrics-{experiment_id[:8]}-{uuid4().hex[:8]}"

        sync_run = create_sync_run(
            session,
            workspace_id=workspace_id,
            meta_mode=sandbox_mode,
            contract_generation_id=ocg.id,
            occurrence_id=occurrence_id,
            operation_id=operation_id,
            executor=executor,
        )

        all_page_items: list[tuple[date, list[dict]]] = []

        for day_offset in range(days):
            metric_date = today - timedelta(days=days - day_offset)
            page_items = []
            for cm in candidate_metrics:
                candidate_id = cm["candidateId"]
                ad_id = experiment_ad_ids.get(candidate_id)
                if ad_id is None:
                    raise RuntimeError(
                        f"No experiment ad binding found for candidate ref {candidate_id!r}"
                    )
                raw_item = {
                    "ad_id": ad_id,
                    "ad_name": f"GP Ad {ad_id}",
                    "date_start": metric_date.isoformat(),
                    "spend": str(cm.get("dailySpend", "10.00")),
                    "impressions": str(cm.get("dailyImpressions", 100)),
                    "inline_link_clicks": str(cm.get("dailyClicks", 10)),
                    "attribution_setting": "7d_click",
                    "actions": [
                        {
                            "action_type": "offsite_conversion.fb_pixel_purchase",
                            "value": str(cm.get("dailyPurchases", 1)),
                        }
                    ],
                }
                page_items.append(raw_item)
                total_rows += 1
            all_page_items.append((metric_date, page_items))

            payload_json = json.dumps(page_items, sort_keys=True).encode()
            payload_compressed = zlib.compress(payload_json)
            payload_checksum = hashlib.sha256(payload_compressed).hexdigest()

            is_terminal = (day_offset == days - 1)

            intent_id = new_id("mcir")
            fingerprint = hashlib.sha256(
                f"page_fetch:{sync_run.id}:{day_offset}".encode()
            ).hexdigest()
            intent = MetaCallIntentRecord(
                id=intent_id,
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                meta_connection_id=sandbox_connection.id,
                meta_binding_id=binding.id,
                ad_account_id=sandbox_connection.ad_account_id,
                operation_id=operation_id,
                meta_api_method="GET",
                meta_api_endpoint=f"/v21.0/{sandbox_connection.ad_account_id}/insights",
                request_parameters={"date_start": metric_date.isoformat()},
                request_fingerprint_hash=fingerprint,
                connection_state_at_intent="active",
                binding_state_at_intent="active",
                call_type="page_fetch",
                scenario_version_id=sandbox_scenario.id,
                write_permitted=False,
                executor=executor,
            )
            session.add(intent)
            session.flush()

            outcome_id = new_id("mcor")
            outcome = MetaCallOutcomeRecord(
                id=outcome_id,
                workspace_id=workspace_id,
                meta_mode=sandbox_mode,
                intent_id=intent_id,
                operation_id=operation_id,
                network_attempted=False,
                executor=executor,
            )
            session.add(outcome)
            session.flush()

            record_page(
                session,
                sync_run_id=sync_run.id,
                page_order=day_offset + 1,
                payload_checksum=payload_checksum,
                payload_compressed=payload_compressed,
                row_count=len(page_items),
                intent_id=intent_id,
                outcome_id=outcome_id,
                output_cursor=metric_date.isoformat() if not is_terminal else None,
                is_terminal=is_terminal,
            )

        complete_sync_run(
            session,
            sync_run_id=sync_run.id,
            total_pages=days,
            total_rows=total_rows,
        )

        # -- Project into AdDailyMetricRecord for experiment judgement --------
        account = (
            session.query(MetaAccountRecord)
            .filter_by(workspace_id=workspace_id, ad_account_id=projection_ad_account_id)
            .first()
        )
        if account is None:
            account = MetaAccountRecord(
                id=new_id("macc"),
                workspace_id=workspace_id,
                ad_account_id=projection_ad_account_id,
                name=f"GP Sandbox Account",
                currency="USD",
                timezone_name="UTC",
                api_version="v21.0",
            )
            session.add(account)
            session.flush()

        # The experiment judge requires a fresh successful sync whenever the
        # frozen experiment keeps ``require_fresh_data`` enabled. This
        # controlled projection is the sync's durable freshness marker.
        account.observation_contract_hash = contract_hash
        account.last_successful_sync_at = utcnow()

        legacy_sync_run = MetaSyncRunRecord(
            id=new_id("msr"),
            workspace_id=workspace_id,
            account_id=account.id,
            state="completed",
            date_start=today - timedelta(days=days),
            date_stop=today,
            contract_hash=contract_hash,
            contract_json={
                "source": "controlled_sync",
                "experiment_id": experiment_id,
                "purchase_action_type": "offsite_conversion.fb_pixel_purchase",
            },
            api_version="v21.0",
            account_currency="USD",
            account_timezone="UTC",
            idempotency_key=f"gp-metrics-legacy-{experiment_id}-{uuid4().hex[:8]}",
        )
        session.add(legacy_sync_run)
        session.flush()

        for _metric_date, page_items in all_page_items:
            for raw_item in page_items:
                _upsert_metric(session, account, legacy_sync_run, raw_item)
                projected_candidates += 1
        session.flush()

    return {
        "syncRunId": sync_run.id,
        "totalPages": days,
        "totalRows": total_rows,
        "projectedCandidates": projected_candidates,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    env = _required_environment()

    parser = argparse.ArgumentParser(description="Golden-path E2E infrastructure seeder")
    commands = parser.add_subparsers(dest="command")

    plat = commands.add_parser("seed-platform-infrastructure")
    plat.add_argument("--workspace-id", required=True)
    plat.add_argument("--workspace-slug", required=True)

    loc = commands.add_parser("seed-localization-infrastructure")
    loc.add_argument("--workspace-id", required=True)
    loc.add_argument("--asset-version-id", required=True)

    scale = commands.add_parser("seed-scale-infrastructure")
    scale.add_argument("--workspace-id", required=True)
    scale.add_argument("--decision-id", required=True)

    metrics = commands.add_parser("seed-experiment-metrics")
    metrics.add_argument("--workspace-id", required=True)
    metrics.add_argument("--experiment-id", required=True)
    metrics.add_argument("--days", type=int, required=True)
    metrics.add_argument("--day-offset", type=int, default=0)
    metrics.add_argument("--candidate-metrics-json", required=True)

    args = parser.parse_args(argv)

    if args.command == "seed-platform-infrastructure":
        result = _seed_platform_infrastructure(env, args)
    elif args.command == "seed-localization-infrastructure":
        result = _seed_localization_infrastructure(env, args)
    elif args.command == "seed-scale-infrastructure":
        result = _seed_scale_infrastructure(env, args)
    elif args.command == "seed-experiment-metrics":
        result = _seed_experiment_metrics(env, args)
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}")

    json.dump(result, sys.stdout, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
