"""Seed Localization and Scale prerequisites from a confirmed Experiment
Winner for the cross-area browser journey (issue #28 closeout).

Unlike the per-area seeds that create their own Winners, this script
accepts a ``decision_id`` as its authoritative input and derives every
downstream prerequisite from the persisted Experiment records.  It
validates the full lineage: AssetVersion, ProductVersion, OfferVersion,
market, experiment context, and confirmed verdict.

Subcommands:

  seed-localization-from-decision
    Given a confirmed ``decision_id`` in the E2E Workspace, creates the
    Localization prerequisites (market policy versions, truth policy,
    data source, claim inventory) and the foreign-Workspace isolation
    fixture.  Emits JSON with all ids the Localization browser steps
    need.

  seed-scale-from-decision
    Given a confirmed ``decision_id``, creates the Scale prerequisites
    (DeliveryTarget, activation-evidence, metric-semantic contracts,
    policy version) without creating another Winner.  Validates the
    complete Scale eligibility chain.  Emits JSON with all ids the
    Scale browser steps need.

  inspect-canonical-snapshot
    Returns stable identities and links across all three areas for the
    before/after restart comparison: Winner decision/verdict,
    Localization batch/items/attempts/cost/lineage, Scale
    enrollment/episode/evaluations/advice/draft, mock provenance, and
    zero-write evidence.

Required environment:
  - E2E_CROSS_AREA_ALLOW_MUTATIONS=1
  - E2E_CROSS_AREA_WORKSPACE_ID
  - E2E_CROSS_AREA_WORKSPACE_SLUG
  - E2E_OWNER_USER_ID
  - E2E_MEMBER_EMAIL
  - E2E_OWNER_EMAIL
  - E2E_CROSS_AREA_CONTROL_OWNER_DATABASE_URL
  - MARKETAI_DATABASE_URL
  - MARKETAI_TENANT_CONTEXT_SECRET
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

REQUIRED_ENVIRONMENT = (
    "E2E_CROSS_AREA_ALLOW_MUTATIONS",
    "E2E_CROSS_AREA_WORKSPACE_ID",
    "E2E_CROSS_AREA_WORKSPACE_SLUG",
    "E2E_OWNER_USER_ID",
    "E2E_MEMBER_EMAIL",
    "E2E_OWNER_EMAIL",
    "MARKETAI_DATABASE_URL",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)
MUTATION_OPT_IN = "E2E_CROSS_AREA_ALLOW_MUTATIONS"
SAFE_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
DATABASE_TARGETS = {
    "MARKETAI_DATABASE_URL": "marketai_domain",
}

FOREIGN_WORKSPACE_ID = "80808080-8080-4080-8080-808080802811"
FOREIGN_WORKSPACE_SLUG = "workspace-b-cross-area"
FOREIGN_OWNER_USER_ID = "80808080-8080-4080-8080-808080802812"
FOREIGN_OWNER_MEMBER_ID = "80808080-8080-4080-8080-808080802813"
FOREIGN_OWNER_EMAIL = "cross-area-foreign-owner@example.test"


def _database_url_problems(name: str, database_url: str, *, expected_database: str) -> list[str]:
    parsed = urlsplit(database_url)
    problems: list[str] = []
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        problems.append(f"{name} must use PostgreSQL")
    host = (parsed.hostname or "").lower()
    if host not in SAFE_DATABASE_HOSTS:
        problems.append(f"{name} host is not an approved local/compose target: {host or '<missing>'}")
    database = parsed.path.removeprefix("/")
    if database != expected_database:
        problems.append(f"{name} must target the {expected_database} database")
    username = (parsed.username or "").lower()
    if username != "owner" and not username.endswith("_owner"):
        problems.append(f"{name} must use an owner role")
    return problems


def _sqlalchemy_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def _required_environment() -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENVIRONMENT}
    missing = [name for name, value in values.items() if not value]
    problems: list[str] = []
    if missing:
        problems.append("missing: " + ", ".join(missing))

    opt_in = values.get(MUTATION_OPT_IN, "")
    if opt_in and opt_in != "1":
        problems.append(f"{MUTATION_OPT_IN} must be exactly 1")

    for name, expected_database in DATABASE_TARGETS.items():
        database_url = values.get(name, "")
        if database_url:
            problems.extend(_database_url_problems(name, database_url, expected_database=expected_database))

    if problems:
        raise RuntimeError("Cross-area E2E mutation preflight failed; " + "; ".join(problems))
    return values


def _tenant_context(environment: dict[str, str], *, meta_mode: str | None = None):
    from app.platform.tenancy import TenantContext

    authorization_epoch = int(os.environ.get("E2E_CROSS_AREA_AUTHORIZATION_EPOCH", "0"))
    return TenantContext(
        workspace_id=environment["E2E_CROSS_AREA_WORKSPACE_ID"],
        role="owner",
        initiator_id=environment["E2E_OWNER_USER_ID"],
        initiator_role="owner",
        executor=f"user:{environment['E2E_OWNER_USER_ID']}",
        request_id="e2e-cross-area-seed",
        trace_id="e2e-cross-area-seed",
        authorization_epoch=authorization_epoch,
        meta_mode=meta_mode,
    )


def _resolve_decision_lineage(ctx, decision_id: str) -> dict:
    """Derive and validate the full Winner lineage from a confirmed decision_id."""
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        AssetVersionRecord,
        LibraryVersionRecord,
        QualityReviewRecord,
    )
    from app.experiments.models import (
        ExperimentDecisionRecord,
        ExperimentVersionCandidateRecord,
        ExperimentVersionRecord,
    )

    with tenant_session(ctx) as session:
        decision = session.get(ExperimentDecisionRecord, decision_id)
        if decision is None:
            raise RuntimeError(f"Decision {decision_id!r} not found in workspace")
        if decision.workspace_id != ctx.workspace_id:
            raise RuntimeError(f"Decision {decision_id!r} belongs to a different workspace")
        if decision.verdict not in ("win", "winner"):
            raise RuntimeError(f"Decision {decision_id!r} verdict is {decision.verdict!r}, not a Winner")

        candidate = session.get(ExperimentVersionCandidateRecord, decision.experiment_version_candidate_id)
        if candidate is None:
            raise RuntimeError("Decision's experiment version candidate is missing")
        asset_version_id = candidate.asset_version_id

        experiment_version = session.get(ExperimentVersionRecord, decision.experiment_version_id)
        if experiment_version is None:
            raise RuntimeError("Decision's experiment version is missing")

        asset_version = session.get(AssetVersionRecord, asset_version_id)
        if asset_version is None:
            raise RuntimeError(f"AssetVersion {asset_version_id!r} is missing")

        review = (
            session.query(QualityReviewRecord)
            .filter_by(workspace_id=ctx.workspace_id, asset_version_id=asset_version_id, review_value="approved")
            .first()
        )
        if review is None:
            raise RuntimeError(f"AssetVersion {asset_version_id!r} has no approved review")

        product_version_id = experiment_version.product_version_id
        offer_version_id = experiment_version.offer_version_id
        market = experiment_version.market

        product_version = session.get(LibraryVersionRecord, product_version_id)
        if product_version is None or product_version.state != "effective":
            raise RuntimeError(f"ProductVersion {product_version_id!r} is not effective")

        offer_version = session.get(LibraryVersionRecord, offer_version_id)
        if offer_version is None or offer_version.state != "effective":
            raise RuntimeError(f"OfferVersion {offer_version_id!r} is not effective")

        return {
            "decision_id": decision_id,
            "verdict": decision.verdict,
            "asset_version_id": asset_version_id,
            "product_id": asset_version.product_id,
            "product_version_id": product_version_id,
            "offer_version_id": offer_version_id,
            "market": market,
            "experiment_id": experiment_version.experiment_id,
            "experiment_version_id": experiment_version.id,
            "landing_page_evidence_hash": experiment_version.landing_page_evidence_hash,
        }


# ---------------------------------------------------------------------------
# seed-localization-from-decision
# ---------------------------------------------------------------------------


def _seed_foreign_workspace(environment: dict[str, str]) -> None:
    """Create a real foreign Workspace in both control and domain databases."""
    from sqlalchemy import create_engine, text
    from app.adapters.persistence.database import engine

    owner_email = environment["E2E_OWNER_EMAIL"]
    member_email = environment["E2E_MEMBER_EMAIL"]
    if FOREIGN_OWNER_EMAIL in {owner_email, member_email}:
        raise RuntimeError("Foreign Workspace owner must be a distinct E2E account")

    control_url = environment.get("E2E_CROSS_AREA_CONTROL_OWNER_DATABASE_URL", "").strip()
    if control_url:
        control_engine = create_engine(_sqlalchemy_postgres_url(control_url), pool_pre_ping=True)
        try:
            with control_engine.begin() as connection:
                connection.execute(
                    text(
                        'INSERT INTO "User" '
                        '("id", "name", "email", "emailVerified", "createdAt", "updatedAt") '
                        'VALUES (:id, :name, :email, NOW(), NOW(), NOW()) '
                        'ON CONFLICT ("id") DO UPDATE SET '
                        '"name" = EXCLUDED."name", "email" = EXCLUDED."email", '
                        '"emailVerified" = EXCLUDED."emailVerified", '
                        '"updatedAt" = EXCLUDED."updatedAt"'
                    ),
                    {"id": FOREIGN_OWNER_USER_ID, "name": "Cross-Area Foreign Owner", "email": FOREIGN_OWNER_EMAIL},
                )
                connection.execute(
                    text(
                        'INSERT INTO "Team" '
                        '("id", "name", "slug", "authorizationEpoch", '
                        '"entitlementVersion", "suspended", "projectionVersion", '
                        '"lifecycle", "createdAt", "updatedAt") '
                        "VALUES (:id, :name, :slug, 0, 0, FALSE, 0, 'ACTIVE', NOW(), NOW()) "
                        'ON CONFLICT ("id") DO UPDATE SET '
                        '"name" = EXCLUDED."name", "slug" = EXCLUDED."slug", '
                        '"updatedAt" = EXCLUDED."updatedAt"'
                    ),
                    {"id": FOREIGN_WORKSPACE_ID, "name": "Cross-Area Foreign Workspace", "slug": FOREIGN_WORKSPACE_SLUG},
                )
                connection.execute(
                    text(
                        'INSERT INTO "TeamMember" '
                        '("id", "teamId", "userId", "role", "createdAt", "updatedAt") '
                        "VALUES (:id, :team_id, :user_id, 'OWNER', NOW(), NOW()) "
                        'ON CONFLICT ("teamId", "userId") DO UPDATE SET '
                        '"role" = EXCLUDED."role", "updatedAt" = EXCLUDED."updatedAt"'
                    ),
                    {"id": FOREIGN_OWNER_MEMBER_ID, "team_id": FOREIGN_WORKSPACE_ID, "user_id": FOREIGN_OWNER_USER_ID},
                )
                leaked = connection.execute(
                    text(
                        'SELECT u."email" FROM "TeamMember" tm '
                        'JOIN "User" u ON u."id" = tm."userId" '
                        'WHERE tm."teamId" = :team_id '
                        'AND u."email" IN (:owner_email, :member_email)'
                    ),
                    {"team_id": FOREIGN_WORKSPACE_ID, "owner_email": owner_email, "member_email": member_email},
                ).scalars().all()
                if leaked:
                    raise RuntimeError("Test actors unexpectedly belong to the foreign Workspace")
        finally:
            control_engine.dispose()

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces "
                "(id, name, default_currency, authorization_epoch, "
                "entitlement_version, projection_version, lifecycle, "
                "budget_currency, created_at) "
                "VALUES (:id, :name, 'USD', 0, 0, 0, 'active', 'USD', NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "name = EXCLUDED.name, lifecycle = 'active'"
            ),
            {"id": FOREIGN_WORKSPACE_ID, "name": "Cross-Area Foreign Workspace"},
        )
        connection.execute(
            text(
                "INSERT INTO entitlement_projections "
                "(workspace_id, entitlement_version, lease_sequence, "
                "projection_version, schema_version, period_start, period_end, "
                "resolved_entitlements_json, semantic_hash, issued_at, "
                "valid_until, applied_at) "
                "VALUES (:id, 0, 1, 1, 2, NOW() - INTERVAL '1 day', "
                "NOW() + INTERVAL '29 days', '{}'::jsonb, :hash, "
                "NOW() - INTERVAL '1 day', NOW() + INTERVAL '30 days', NOW()) "
                "ON CONFLICT (workspace_id) DO UPDATE SET "
                "valid_until = EXCLUDED.valid_until, applied_at = EXCLUDED.applied_at"
            ),
            {"id": FOREIGN_WORKSPACE_ID, "hash": "e2e28-foreign-entitlement"},
        )
        connection.execute(
            text(
                "INSERT INTO spend_guardrails "
                "(workspace_id, revision, budget_currency, per_operation_limit, "
                "rolling_24h_limit, period_limit, outstanding_exposure_limit, "
                "created_at, updated_at, updated_by) "
                "VALUES (:id, 1, 'USD', 100, 500, 5000, 1000, NOW(), NOW(), 'e2e28-setup') "
                "ON CONFLICT (workspace_id) DO NOTHING"
            ),
            {"id": FOREIGN_WORKSPACE_ID},
        )


def _seed_localization_from_decision(environment: dict[str, str], decision_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.localization.claims import create_inventory_from_analysis
    from tests.test_localization_batch import _make_market_policy_version, _make_version, _seed_review

    ctx = _tenant_context(environment)
    lineage = _resolve_decision_lineage(ctx, decision_id)
    tag = f"e2e28loc{uuid4().hex[:6]}"

    _seed_review(ctx, lineage["asset_version_id"], product_id=lineage["product_id"])
    _seed_foreign_workspace(environment)

    with tenant_session(ctx) as session:
        market_specs = (
            ("GB", "ready", "complete_with_discount", False),
            ("CA", "degraded", "standard_9_market", True),
            ("AU", "unavailable", "unavailable", False),
        )
        markets = []
        for country, readiness, scenario, fails_locked_copy_qc in market_specs:
            market = f"{country}{uuid4().hex[:6].upper()}"
            market_policy = _make_market_policy_version(session, market)
            if fails_locked_copy_qc:
                market_policy.qc_rules_json = {"forbidden_terms": ["durable"]}
            markets.append({
                "market": market,
                "country": country,
                "expectedReadiness": readiness,
                "mockScenario": scenario,
                "marketPolicyVersionId": market_policy.id,
                "failsLockedCopyQc": fails_locked_copy_qc,
            })
        truth_policy_id = _make_version(
            session, ctx.workspace_id, "truth_resolution_policy", tag,
            content={
                "rules": [
                    {"fact_type": "price", "source_priority": ["mock"], "on_failure": "block", "max_staleness_seconds": 86400},
                    {"fact_type": "availability", "source_priority": ["mock"], "on_failure": "block", "max_staleness_seconds": 86400},
                    {"fact_type": "discount", "source_priority": ["mock"], "on_failure": "skip", "max_staleness_seconds": 86400},
                ],
            },
        )
        data_source_id = _make_version(
            session, ctx.workspace_id, "data_source", tag,
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
            asset_version_id=lineage["asset_version_id"],
            analysis_recipe_hash="cross-area-analysis-v1",
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
        "decisionId": decision_id,
        "assetVersionId": lineage["asset_version_id"],
        "productId": lineage["product_id"],
        "productVersionId": lineage["product_version_id"],
        "offerVersionId": lineage["offer_version_id"],
        "market": lineage["market"],
        "foreignWorkspaceId": FOREIGN_WORKSPACE_ID,
        "foreignWorkspaceSlug": FOREIGN_WORKSPACE_SLUG,
        "inventoryId": inventory.id,
        "markets": markets,
        "truthPolicyVersionId": truth_policy_id,
        "dataSourceVersionId": data_source_id,
    }


# ---------------------------------------------------------------------------
# seed-scale-from-decision
# ---------------------------------------------------------------------------


def _seed_scale_from_decision(environment: dict[str, str], decision_id: str) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.meta.models import ScenarioVersionRecord
    from app.platform.canonical import canonical_hash
    from tests.support.scale_candidate_fixtures import (
        seed_activation_evidence_prereqs,
    )

    ctx = _tenant_context(environment)
    lineage = _resolve_decision_lineage(ctx, decision_id)
    tag = f"e2e28sc{uuid4().hex[:6]}"
    target_meta_mode = "controlled_mock"

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        AssetVersionRecord,
        LibraryVersionRecord,
        QualityReviewRecord,
    )
    from app.experiments.models import ExperimentDecisionRecord
    from app.localization.models import MarketAssetBindingRecord
    from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
    from app.scale.models import ScaleDeliveryTargetRecord

    target_ctx = _tenant_context(environment, meta_mode=target_meta_mode)

    with tenant_session(target_ctx) as session:
        # Validate the asset version is approved and has a current binding
        av = session.get(AssetVersionRecord, lineage["asset_version_id"])
        if av is None:
            raise RuntimeError(f"AssetVersion {lineage['asset_version_id']!r} missing")

        review = (
            session.query(QualityReviewRecord)
            .filter_by(workspace_id=ctx.workspace_id, asset_version_id=av.id, review_value="approved")
            .first()
        )
        if review is None:
            raise RuntimeError("Winner AssetVersion has no approved review for Scale eligibility")

        # Find or create controlled_mock MetaConnection
        mock_connection = (
            session.query(MetaConnectionRecord)
            .filter_by(workspace_id=ctx.workspace_id, meta_mode=target_meta_mode, state="active")
            .first()
        )
        if mock_connection is None:
            mock_connection = MetaConnectionRecord(
                id=f"mcon_{tag}_mock",
                workspace_id=ctx.workspace_id,
                meta_mode=target_meta_mode,
                ad_account_id=f"act_{tag}_mock",
                state="active",
            )
            session.add(mock_connection)
            session.flush()

        # Ensure ScenarioVersion
        existing_scenario = (
            session.query(ScenarioVersionRecord)
            .filter_by(workspace_id=ctx.workspace_id, meta_mode=target_meta_mode, meta_connection_id=mock_connection.id, is_default=True)
            .first()
        )
        if existing_scenario is None:
            scenario_config: dict = {}
            existing_scenario = ScenarioVersionRecord(
                id=f"scen_{tag}_mock",
                workspace_id=ctx.workspace_id,
                meta_mode=target_meta_mode,
                meta_connection_id=mock_connection.id,
                scenario_config=scenario_config,
                scenario_hash=canonical_hash(scenario_config),
                is_default=True,
            )
            session.add(existing_scenario)
            session.flush()

        # Create DeliveryTarget
        from app.adapters.persistence.models import new_id

        delivery_target_id = new_id("sdt")
        contract_generation_id = f"gen_{tag}"
        target = ScaleDeliveryTargetRecord(
            id=delivery_target_id,
            workspace_id=ctx.workspace_id,
            meta_mode=target_meta_mode,
            decision_id=decision_id,
            asset_version_id=lineage["asset_version_id"],
            product_version_id=lineage["product_version_id"],
            offer_version_id=lineage["offer_version_id"],
            market=lineage["market"],
            ad_account_id=mock_connection.ad_account_id,
            meta_connection_id=mock_connection.id,
            scenario_version_id=existing_scenario.id,
            contract_generation_id=contract_generation_id,
            currency="USD",
            timezone_name="UTC",
            delivery_target_state="ready",
        )
        session.add(target)
        session.flush()

    # Seed activation evidence prerequisites
    seed_activation_evidence_prereqs(
        target_ctx, delivery_target_id=delivery_target_id,
        meta_mode=target_meta_mode, tag=tag,
    )

    # Extend with full contract set and policy
    with tenant_session(target_ctx) as session:
        from sqlalchemy import select
        from app.scale.models import (
            ScaleMetricSemanticContractSetMemberRecord,
            ScalePolicyVersionRecord,
        )

        target = session.get(ScaleDeliveryTargetRecord, delivery_target_id)
        cgen = target.contract_generation_id

        contract_set_id = session.execute(
            select(ScaleMetricSemanticContractSetMemberRecord.contract_set_id).filter_by(
                observation_contract_generation=cgen,
                contract_type="delivery_configuration",
            )
        ).scalar_one()

        for contract_type in ("daily", "exact_window"):
            session.add(ScaleMetricSemanticContractSetMemberRecord(
                id=f"smcm_{tag}_{contract_type}",
                contract_set_id=contract_set_id,
                contract_type=contract_type,
                observation_contract_generation=cgen,
                contract_hash=f"{contract_type}-hash-{tag}",
            ))
        session.flush()

        # Publish policy version
        policy = ScalePolicyVersionRecord(
            id=f"spol_{tag}",
            algorithm_version="beta-v1",
            canonical_schema_version="scale-snapshot-v1",
            decimal_precision=28,
            decimal_rounding="round_half_even",
            z_value=Decimal("1.96"),
            evidence_floor_minimum_spend=Decimal("50"),
            evidence_floor_minimum_days=3,
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
            bootstrap_valid_draw_ratio=Decimal("0.5"),
            bootstrap_percentile_method="nearest_rank",
            bootstrap_draw_order="sequential",
            breakdown_families={},
            breakdown_sample_gate_minimum=1,
            breakdown_share_gate_minimum_ratio=Decimal("0.05"),
            bh_fdr_alpha=Decimal("0.10"),
            advice_severity_thresholds={},
            notification_cooldown_hours=24,
            conversion_safe_values={},
            mode_permissions={},
            is_published=True,
            created_by="e2e-seed",
            published_at=datetime.now(timezone.utc),
        )
        session.add(policy)
        session.flush()
        policy_version_id = policy.id

    return {
        "decisionId": decision_id,
        "assetVersionId": lineage["asset_version_id"],
        "productId": lineage["product_id"],
        "productVersionId": lineage["product_version_id"],
        "offerVersionId": lineage["offer_version_id"],
        "marketCode": lineage["market"],
        "metaMode": target_meta_mode,
        "deliveryTargetId": delivery_target_id,
        "contractSetId": contract_set_id,
        "policyVersionId": policy_version_id,
    }


# ---------------------------------------------------------------------------
# inspect-canonical-snapshot
# ---------------------------------------------------------------------------


def _inspect_canonical_snapshot(environment: dict[str, str], args: argparse.Namespace) -> dict:
    from app.adapters.persistence.database import tenant_session

    ctx = _tenant_context(environment)
    result: dict = {
        "inspectedAt": datetime.now(timezone.utc).isoformat(),
        "workspaceId": ctx.workspace_id,
    }

    # Winner decision
    if args.decision_id:
        from app.experiments.models import ExperimentDecisionRecord

        with tenant_session(ctx) as session:
            decision = session.get(ExperimentDecisionRecord, args.decision_id)
            result["winner"] = {
                "decisionId": decision.id if decision else None,
                "verdict": decision.verdict if decision else None,
                "assetVersionId": None,
            }
            if decision:
                from app.experiments.models import ExperimentVersionCandidateRecord

                candidate = session.get(ExperimentVersionCandidateRecord, decision.experiment_version_candidate_id)
                if candidate:
                    result["winner"]["assetVersionId"] = candidate.asset_version_id

    # Localization batch
    if args.batch_id:
        from app.localization.models import LocalizationBatchRecord, LocalizationItemRecord
        from sqlalchemy import select

        with tenant_session(ctx) as session:
            batch = session.get(LocalizationBatchRecord, args.batch_id)
            items = session.execute(
                select(LocalizationItemRecord).filter_by(
                    workspace_id=ctx.workspace_id, batch_id=args.batch_id,
                )
            ).scalars().all()
            result["localization"] = {
                "batchId": batch.id if batch else None,
                "phase": batch.phase if batch else None,
                "outcome": batch.outcome if batch else None,
                "truthPackageId": batch.truth_package_id if batch else None,
                "items": [
                    {
                        "id": item.id,
                        "state": item.state,
                        "targetMarket": item.target_market,
                        "attemptCount": item.attempt_count,
                        "reasonCode": item.reason_code,
                    }
                    for item in items
                ],
            }

    # Scale enrollment
    if args.enrollment_id and args.episode_id:
        meta_mode = args.meta_mode or "controlled_mock"
        scale_ctx = _tenant_context(environment, meta_mode=meta_mode)
        from app.scale.models import (
            ScaleEnrollmentRecord,
            ScaleEpisodeRecord,
            ScaleEvaluationRecord,
        )
        from sqlalchemy import func, select

        with tenant_session(scale_ctx) as session:
            enrollment = session.get(ScaleEnrollmentRecord, args.enrollment_id)
            episode = session.get(ScaleEpisodeRecord, args.episode_id)
            eval_count = session.execute(
                select(func.count()).select_from(ScaleEvaluationRecord).filter_by(
                    workspace_id=ctx.workspace_id, meta_mode=meta_mode, episode_id=args.episode_id,
                )
            ).scalar_one()

            result["scale"] = {
                "enrollmentId": enrollment.id if enrollment else None,
                "lifecycle": enrollment.lifecycle if enrollment else None,
                "revision": enrollment.revision if enrollment else None,
                "episodeId": episode.id if episode else None,
                "episodePhase": episode.phase if episode else None,
                "evaluationCount": eval_count,
            }

    # Zero Meta writes
    from app.adapters.persistence.database import tenant_session as ts2
    from sqlalchemy import select, text

    with ts2(ctx) as session:
        offending = session.execute(
            text(
                "SELECT id, meta_api_method FROM call_intents "
                "WHERE workspace_id = :ws AND meta_api_method IN ('POST', 'PUT', 'DELETE', 'PATCH')"
            ),
            {"ws": ctx.workspace_id},
        ).mappings().all()
        result["zeroMetaWrites"] = {
            "offending": [{"id": row["id"], "method": row["meta_api_method"]} for row in offending],
        }

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross-area E2E prerequisite seeder")
    commands = parser.add_subparsers(dest="command")

    loc = commands.add_parser("seed-localization-from-decision")
    loc.add_argument("--decision-id", required=True)

    scale = commands.add_parser("seed-scale-from-decision")
    scale.add_argument("--decision-id", required=True)

    snap = commands.add_parser("inspect-canonical-snapshot")
    snap.add_argument("--decision-id", default=None)
    snap.add_argument("--batch-id", default=None)
    snap.add_argument("--enrollment-id", default=None)
    snap.add_argument("--episode-id", default=None)
    snap.add_argument("--meta-mode", default="controlled_mock")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    environment = _required_environment()

    from app.adapters.persistence.database import engine

    if engine.dialect.name != "postgresql":
        raise RuntimeError("Cross-area E2E setup requires PostgreSQL")

    if args.command == "seed-localization-from-decision":
        result = _seed_localization_from_decision(environment, args.decision_id)
    elif args.command == "seed-scale-from-decision":
        result = _seed_scale_from_decision(environment, args.decision_id)
    elif args.command == "inspect-canonical-snapshot":
        result = _inspect_canonical_snapshot(environment, args)
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}")

    json.dump(result, sys.stdout, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
