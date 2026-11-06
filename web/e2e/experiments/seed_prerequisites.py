"""Seed PostgreSQL prerequisites for the Experiments E2E suite (issue #88).

Mirrors `web/e2e/localization/seed_prerequisites.py`: this script never
creates a Workspace or Team -- it mutates durable domain data *inside* the
existing E2E Workspace the pre-authenticated `owner` Playwright storage state
already belongs to (`e2e/workspace/fixtures.ts` / `playwright.config.ts`'s
`chromium` project). Every invocation is tagged with a fresh random suffix so
repeated runs never collide. A Workspace holds one active Meta connection
per mode, so each invocation retires the previous one: run the suite on one
Playwright worker and one project, as `npm run test:e2e:localization` does.

Creates:
  - A Product with governed, effective/eligible Product and Offer
    LibraryVersions (what `create_experiment_v2`'s `draft_context.
    product_version_id`/`offer_version_id` needs).
  - Three approved, QC-passed AssetVersions. Two of them share one
    DimensionConceptKey (a real freeze-time duplicate pair); the third
    carries a distinct concept key (an independent candidate). This gives
    `analyze_duplicates`/`freeze_experiment_v2` a genuine duplicate group to
    resolve, exercised through `RunItemRecord.combination_identity_json` the
    same way `tests/experiment_governance_fixtures.py` and
    `tests/test_freeze_gates.py::_seed_asset_versions` do -- this script does
    not patch or bypass a service gate.
  - A `customer_sandbox` MetaConnection + default ScenarioVersion + active
    binding for `act_<tag>_sbx`, through `app.meta.connections`.
  - A `controlled_mock` MetaConnection + binding for `act_<tag>_mock` whose
    default ScenarioVersion's `scenario_config.faults` carries the optional
    `--fault-config` payload (`{"rules": [...]}`).

Required environment (see also `web/e2e/experiments/helpers.ts`,
`web/e2e/experiments/fixtures.ts`):
  - E2E_EXPERIMENTS_ALLOW_MUTATIONS=1   (explicit opt-in; refuses otherwise)
  - E2E_EXPERIMENTS_WORKSPACE_ID        (domain Workspace id, e.g. workspace-a's)
  - E2E_EXPERIMENTS_WORKSPACE_SLUG      (its BFF slug, e.g. "workspace-a")
  - E2E_OWNER_USER_ID                   (the storage-state owner's user id)
  - MARKETAI_DATABASE_URL               (same PostgreSQL FastAPI/Worker use)
  - MARKETAI_TENANT_CONTEXT_SECRET
  - E2E_EXPERIMENTS_AUTHORIZATION_EPOCH (optional, default "0"; must match
    the domain Workspace projection's current `authorization_epoch`)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

REQUIRED_ENVIRONMENT = (
    "E2E_EXPERIMENTS_ALLOW_MUTATIONS",
    "E2E_EXPERIMENTS_WORKSPACE_ID",
    "E2E_EXPERIMENTS_WORKSPACE_SLUG",
    "E2E_OWNER_USER_ID",
    "MARKETAI_DATABASE_URL",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)
MUTATION_OPT_IN = "E2E_EXPERIMENTS_ALLOW_MUTATIONS"
SAFE_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
EXPECTED_DATABASE = "marketai_domain"


def _database_url_problems(name: str, database_url: str) -> list[str]:
    parsed = urlsplit(database_url)
    problems: list[str] = []
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        problems.append(f"{name} must use PostgreSQL")
    host = (parsed.hostname or "").lower()
    if host not in SAFE_DATABASE_HOSTS:
        display_host = host or "<missing>"
        problems.append(f"{name} host is not an approved local/compose target: {display_host}")
    database = parsed.path.removeprefix("/")
    if database != EXPECTED_DATABASE:
        problems.append(f"{name} must target the {EXPECTED_DATABASE} database")
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

    database_url = values.get("MARKETAI_DATABASE_URL", "")
    if database_url:
        problems.extend(_database_url_problems("MARKETAI_DATABASE_URL", database_url))

    if problems:
        raise RuntimeError("Experiments E2E mutation preflight failed; " + "; ".join(problems))
    return values


def seed(*, fault_config: dict | None = None) -> dict:
    """Seed one fresh, tag-scoped set of Experiments prerequisites."""
    environment = _required_environment()

    # Import only after validating environment/connection inputs so a
    # misconfigured run fails once with the complete missing-variable list,
    # before the application creates its database engine (mirrors the
    # Localization seed script).
    from app.adapters.persistence.database import engine, tenant_session
    from app.adapters.persistence.models import WorkspaceRecord
    from app.platform.tenancy import TenantContext

    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "Experiments E2E setup requires PostgreSQL; set MARKETAI_DATABASE_URL "
            "to the same database used by FastAPI"
        )

    workspace_id = environment["E2E_EXPERIMENTS_WORKSPACE_ID"]
    workspace_slug = environment["E2E_EXPERIMENTS_WORKSPACE_SLUG"]
    initiator_id = environment["E2E_OWNER_USER_ID"]
    authorization_epoch = int(os.environ.get("E2E_EXPERIMENTS_AUTHORIZATION_EPOCH", "0"))
    ctx = TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=initiator_id,
        initiator_role="owner",
        request_id="e2e-experiments-seed",
        trace_id="e2e-experiments-seed",
        authorization_epoch=authorization_epoch,
    )

    with tenant_session(ctx) as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        if workspace is None:
            raise RuntimeError(f"Workspace {workspace_id!r} is missing from the domain database")
        if workspace.authorization_epoch != authorization_epoch:
            raise RuntimeError(
                "E2E_EXPERIMENTS_AUTHORIZATION_EPOCH does not match the domain "
                f"Workspace projection ({workspace.authorization_epoch})"
            )

    return seed_workspace(ctx, workspace_slug=workspace_slug, fault_config=fault_config)


def seed_workspace(
    ctx, *, workspace_slug: str, fault_config: dict | None = None,
) -> dict:
    """Write one tag-scoped set of prerequisites into ``ctx``'s Workspace.

    ``seed`` owns the environment, PostgreSQL and authorization-epoch checks;
    this part only writes, so `tests/test_experiments_e2e_seed.py` can run it
    against the SQLite compatibility database.
    """
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        AssetRecord,
        AssetVersionRecord,
        DimensionConceptKeyRecord,
        GenerationRunRecord,
        LibraryEntryRecord,
        LibraryVersionRecord,
        PlanRecord,
        ProductRecord,
        QualityReviewRecord,
    )
    from app.generation.models import AssetQCAttemptRecord, RunItemRecord
    from app.meta.connections import create_binding, create_connection, retire_connection
    from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
    from app.platform.canonical import _canonical_json, _sha256
    from sqlalchemy import select

    workspace_id = ctx.workspace_id
    initiator_id = ctx.initiator_id

    # A random tag makes every Playwright project/run independent and lets
    # the test prove ids came from this run rather than a long-lived seed.
    tag = f"e2e88{uuid4().hex[:8]}"
    product_id = f"prod_{tag}"
    product_version_id = f"pv_{tag}"
    offer_version_id = f"ov_{tag}"
    product_entry_id = f"le_pv_{tag}"
    offer_entry_id = f"le_ov_{tag}"
    shared_concept_key_id = f"concept_{tag}_shared"
    distinct_concept_key_id = f"concept_{tag}_distinct"

    with tenant_session(ctx) as session:
        # --- Product + governed Product/Offer LibraryVersions --------------
        session.add(ProductRecord(
            id=product_id, workspace_id=workspace_id, name=f"E2E experiments product {tag}",
        ))
        session.flush()

        session.add(LibraryEntryRecord(
            id=product_entry_id, workspace_id=workspace_id, entry_type="product",
            display_name=f"E2E product {tag}",
        ))
        session.add(LibraryEntryRecord(
            id=offer_entry_id, workspace_id=workspace_id, entry_type="offer",
            display_name=f"E2E offer {tag}",
        ))
        session.flush()

        product_content: dict = {}
        offer_content = {
            "product_version_id": product_version_id,
            "market_scope": ["US"],
            "landing_page_url": "https://example.test/experiments-e2e",
            "valid_from": "2026-01-01",
            "valid_to": "2027-12-31",
            "source_market_snapshot": {
                "market": "US",
                "placement": "feed",
                "locale": "en-US",
                "country": "US",
                "currency": "USD",
            },
        }
        session.add(LibraryVersionRecord(
            id=product_version_id, workspace_id=workspace_id, entry_id=product_entry_id,
            entry_type="product", version_no=1, content_json=product_content,
            content_hash=_sha256(_canonical_json(product_content).encode("utf-8")),
            state="effective", eligibility="eligible", published_by="e2e-seed",
        ))
        session.add(LibraryVersionRecord(
            id=offer_version_id, workspace_id=workspace_id, entry_id=offer_entry_id,
            entry_type="offer", version_no=1, content_json=offer_content,
            content_hash=_sha256(_canonical_json(offer_content).encode("utf-8")),
            state="effective", eligibility="eligible", published_by="e2e-seed",
        ))
        session.flush()

        product = session.get(ProductRecord, product_id)
        assert product is not None
        product.product_profile_entry_id = product_entry_id
        session.flush()

        # --- Three AssetVersions: a duplicate pair + one independent -------
        plan_id = f"plan_{tag}"
        session.add(PlanRecord(
            id=plan_id, workspace_id=workspace_id, product_id=product_id,
            mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
        ))
        session.flush()
        run_id = f"run_{tag}"
        session.add(GenerationRunRecord(
            id=run_id, workspace_id=workspace_id, product_id=product_id, plan_id=plan_id,
            provider="mock", model="mock", aspect_ratio="1:1", image_size="1024",
            variants=1, use_ref=False, status="done", total=3, done=3, failed=0,
            est_cost=Decimal("0"), currency="USD", execution_mode="sandbox",
        ))
        session.flush()

        session.add(DimensionConceptKeyRecord(
            id=shared_concept_key_id, workspace_id=workspace_id, scope="workspace",
            name=f"Shared concept {tag}", created_by="e2e-seed",
        ))
        session.add(DimensionConceptKeyRecord(
            id=distinct_concept_key_id, workspace_id=workspace_id, scope="workspace",
            name=f"Distinct concept {tag}", created_by="e2e-seed",
        ))
        session.flush()

        # Index 0 and 1 share `shared_concept_key_id` -- the real duplicate
        # pair freeze/analyze_duplicates must detect. Index 2 carries the
        # distinct concept key -- an independent, non-duplicate candidate.
        concept_plan = [shared_concept_key_id, shared_concept_key_id, distinct_concept_key_id]
        asset_version_ids: list[str] = []
        for index, concept_key_id in enumerate(concept_plan):
            run_item_id = f"ritem_{tag}_{index}"
            session.add(RunItemRecord(
                id=run_item_id, workspace_id=workspace_id, generation_run_id=run_id,
                ordinal=index,
                combination_identity_json={
                    "concepts": [{
                        "dimension_key": "creative_angle",
                        "concept_key_id": concept_key_id,
                        "stable_dimension_signature": "creative_angle:v1",
                        "value": {"fixture_identity": f"{tag}:{index}"},
                    }],
                },
                combination_fingerprint=_sha256(f"combo:{tag}:{index}".encode()),
                stable_dimension_signature="creative_angle:v1",
                semantic_fingerprint=_sha256(f"semantic:{tag}:{index}".encode()),
                execution_mode="sandbox", state="succeeded",
            ))
            session.flush()

            asset_id = f"asset_{tag}_{index}"
            av_id = f"av_{tag}_{index}"
            session.add(AssetRecord(
                id=asset_id, workspace_id=workspace_id, product_id=product_id, run_id=run_id,
                combo_id=f"C0{index}", combo_code=f"A{index}", tags_json={},
                filename=f"{tag}_{index}.png", provider="mock", model="mock",
                prompt="e2e experiments duplicate-resolution fixture",
                fingerprint=f"fp_{tag}_{index}", status="ok",
            ))
            session.flush()

            truth_evidence = {"expected": False, "source": "e2e-seed"}
            session.add(AssetVersionRecord(
                id=av_id, workspace_id=workspace_id, asset_id=asset_id, kind="generated",
                aspect_ratio="1:1", language="en", object_key=f"{tag}_{index}.png",
                content_sha256=f"{index:0>64x}", byte_size=1024, mime_type="image/png",
                producing_run_item_id=run_item_id, origin_kind="generation",
                execution_mode="sandbox", truth_dependency_expected=False,
                truth_dependency_policy_version="truth-dependency-v1",
                truth_dependency_evidence_json=truth_evidence,
                truth_dependency_evidence_hash=_sha256(
                    _canonical_json(truth_evidence).encode("utf-8")
                ),
            ))
            session.flush()

            session.add(AssetQCAttemptRecord(
                id=f"qc_{tag}_{index}", workspace_id=workspace_id, asset_version_id=av_id,
                run_item_id=run_item_id, qc_policy_version="qc-v1", rule_set_version="rules-v1",
                input_hash=f"{index:0>64x}", execution_mode="sandbox", state="passed",
                attempt_sequence=1,
            ))
            session.add(QualityReviewRecord(
                id=f"review_{tag}_{index}", workspace_id=workspace_id, product_id=product_id,
                asset_id=asset_id, asset_version_id=av_id, review_value="approved",
                legacy_unscoped=False, actor_id="e2e-seed",
            ))
            session.flush()
            asset_version_ids.append(av_id)

    # --- Meta connections, bindings + default ScenarioVersions ---------------
    # A Workspace holds one active connection per meta_mode
    # (`uq_meta_connection_active`), so retire the previous run's connection
    # and create this run's through the same lifecycle service the Owner's
    # Meta settings use. Launch preview resolves an active binding for the
    # exact connection + ad account the spec types, so create that too.
    meta_seeds = {}
    for meta_mode, ad_account_id, scenario_config in (
        ("customer_sandbox", f"act_{tag}_sbx", {}),
        # `resolve_adapter` hands `scenario_config["faults"]` to FakeMetaAdapter.
        ("controlled_mock", f"act_{tag}_mock", {"faults": fault_config} if fault_config else {}),
    ):
        mode_ctx = replace(ctx, meta_mode=meta_mode)
        with tenant_session(mode_ctx) as session:
            active_connection_ids = session.scalars(select(MetaConnectionRecord.id).where(
                MetaConnectionRecord.workspace_id == workspace_id,
                MetaConnectionRecord.meta_mode == meta_mode,
                MetaConnectionRecord.state == "active",
            )).all()
            for connection_id in active_connection_ids:
                retire_connection(session, context=mode_ctx, connection_id=connection_id)
            connection = create_connection(
                session, context=mode_ctx, ad_account_id=ad_account_id,
                scenario_config=scenario_config,
            )
            create_binding(
                session, context=mode_ctx, connection_id=connection.id,
                ad_account_id=ad_account_id,
            )
            scenario_version_id = session.scalar(select(ScenarioVersionRecord.id).where(
                ScenarioVersionRecord.workspace_id == workspace_id,
                ScenarioVersionRecord.meta_mode == meta_mode,
                ScenarioVersionRecord.meta_connection_id == connection.id,
                ScenarioVersionRecord.is_default.is_(True),
            ))
            meta_seeds[meta_mode] = (connection.id, scenario_version_id, ad_account_id)
    sandbox_connection_id, sandbox_scenario_id, sandbox_ad_account_id = meta_seeds["customer_sandbox"]
    mock_connection_id, mock_scenario_id, mock_ad_account_id = meta_seeds["controlled_mock"]

    return {
        "workspaceId": workspace_id,
        "workspaceSlug": workspace_slug,
        "userId": initiator_id,
        "tag": tag,
        "productId": product_id,
        "productVersionId": product_version_id,
        "offerVersionId": offer_version_id,
        "assetVersionIds": asset_version_ids,
        "duplicatePairAssetVersionIds": asset_version_ids[:2],
        "independentAssetVersionId": asset_version_ids[2],
        "sharedConceptKeyId": shared_concept_key_id,
        "distinctConceptKeyId": distinct_concept_key_id,
        "customerSandboxConnectionId": sandbox_connection_id,
        "customerSandboxScenarioVersionId": sandbox_scenario_id,
        "customerSandboxAdAccountId": sandbox_ad_account_id,
        "controlledMockConnectionId": mock_connection_id,
        "controlledMockScenarioVersionId": mock_scenario_id,
        "controlledMockAdAccountId": mock_ad_account_id,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault-config", type=str, default=None)
    args = parser.parse_args()
    fc = json.loads(args.fault_config) if args.fault_config else None
    result = seed(fault_config=fc)
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
