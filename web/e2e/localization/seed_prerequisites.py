"""Create only the durable prerequisites for the Localization browser journey.

The browser still creates, prepares, approves, and confirms the batch through
the authenticated Next.js BFF.  This helper exists because those prerequisite
objects (a confirmed Winner and published configuration versions) do not have a
Localization-page creation surface. Because it mutates durable Winner and
platform-policy state through an owner connection, it refuses to run without
an explicit opt-in and a local/Compose PostgreSQL target.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import create_engine, text

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

REQUIRED_ENVIRONMENT = (
    "E2E_LOCALIZATION_ALLOW_MUTATIONS",
    "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL",
    "E2E_LOCALIZATION_WORKSPACE_ID",
    "E2E_MEMBER_EMAIL",
    "E2E_OWNER_EMAIL",
    "E2E_OWNER_USER_ID",
    "MARKETAI_DATABASE_URL",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)

MUTATION_OPT_IN = "E2E_LOCALIZATION_ALLOW_MUTATIONS"
SAFE_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
DATABASE_TARGETS = {
    "MARKETAI_DATABASE_URL": "marketai_domain",
    "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL": "marketai_control",
}
FOREIGN_WORKSPACE_ID = "80808080-8080-4080-8080-808080800011"
FOREIGN_WORKSPACE_SLUG = "workspace-b"
FOREIGN_OWNER_USER_ID = "80808080-8080-4080-8080-808080800012"
FOREIGN_OWNER_MEMBER_ID = "80808080-8080-4080-8080-808080800013"
FOREIGN_OWNER_EMAIL = "loc80-foreign-owner@example.test"
TRUTH_POLICY_ENTRY_TYPE = "truth_resolution_policy"
WITHDRAW_REASON = "Localization E2E stale-Truth recovery fixture"


def _library_version_id(value: str) -> str:
    """A Creative Library version id, as the seed hands it over.

    The Library mints ``libver_`` ids (``app/library/service.py``); the
    shorter ``lv_`` prefix is what hand-built test fixtures use. Either way
    the id is at most 64 safe characters, so it cannot smuggle a path or a
    shell token into the command.
    """
    version_id = value.strip()
    if len(version_id) > 64 or not re.fullmatch(
        r"(?:libver|lv)_[A-Za-z0-9_-]+", version_id
    ):
        raise argparse.ArgumentTypeError(
            "version id must start with libver_ or lv_ and contain at most 64 safe characters"
        )
    return version_id


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed or mutate guarded Localization E2E prerequisites"
    )
    commands = parser.add_subparsers(dest="command")
    withdraw = commands.add_parser(
        "withdraw-version",
        help="withdraw one exact Truth policy version in the E2E Workspace",
    )
    withdraw.add_argument("--version-id", required=True, type=_library_version_id)
    return parser.parse_args(argv)


def _database_url_problems(
    name: str,
    database_url: str,
    *,
    expected_database: str,
) -> list[str]:
    parsed = urlsplit(database_url)
    problems: list[str] = []
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        problems.append(f"{name} must use PostgreSQL")
    host = (parsed.hostname or "").lower()
    if host not in SAFE_DATABASE_HOSTS:
        display_host = host or "<missing>"
        problems.append(
            f"{name} host is not an approved local/compose target: {display_host}"
        )
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
            problems.extend(
                _database_url_problems(
                    name,
                    database_url,
                    expected_database=expected_database,
                )
            )

    if problems:
        raise RuntimeError(
            "Localization E2E mutation preflight failed; " + "; ".join(problems)
        )
    return values


def _seed_foreign_control_workspace(control_engine, environment: dict[str, str]) -> None:
    """Create a real foreign Workspace without granting either test actor access."""
    owner_email = environment["E2E_OWNER_EMAIL"]
    member_email = environment["E2E_MEMBER_EMAIL"]
    if FOREIGN_OWNER_EMAIL in {owner_email, member_email}:
        raise RuntimeError("Foreign Workspace owner must be a distinct E2E account")

    with control_engine.begin() as connection:
        actors = connection.execute(
            text(
                'SELECT "id", "email" FROM "User" '
                'WHERE "email" IN (:owner_email, :member_email)'
            ),
            {"owner_email": owner_email, "member_email": member_email},
        ).mappings()
        actors_by_email = {row["email"]: row["id"] for row in actors}
        missing = sorted({owner_email, member_email} - actors_by_email.keys())
        if missing:
            raise RuntimeError(
                "Localization E2E control accounts are missing; seed the login "
                "fixtures before Localization prerequisites"
            )
        if actors_by_email[owner_email] != environment["E2E_OWNER_USER_ID"]:
            raise RuntimeError(
                "E2E_OWNER_USER_ID does not match the control-plane owner account"
            )

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
            {
                "id": FOREIGN_OWNER_USER_ID,
                "name": "Localization Foreign Owner",
                "email": FOREIGN_OWNER_EMAIL,
            },
        )
        connection.execute(
            text(
                'INSERT INTO "Team" '
                '("id", "name", "slug", "authorizationEpoch", '
                '"entitlementVersion", "suspended", "projectionVersion", '
                '"lifecycle", "createdAt", "updatedAt") '
                'VALUES (:id, :name, :slug, 0, 0, FALSE, 0, '
                "'ACTIVE', NOW(), NOW()) "
                'ON CONFLICT ("id") DO UPDATE SET '
                '"name" = EXCLUDED."name", "slug" = EXCLUDED."slug", '
                '"authorizationEpoch" = 0, "entitlementVersion" = 0, '
                '"suspended" = FALSE, "projectionVersion" = 0, '
                '"lifecycle" = EXCLUDED."lifecycle", '
                '"updatedAt" = EXCLUDED."updatedAt"'
            ),
            {
                "id": FOREIGN_WORKSPACE_ID,
                "name": "Localization Foreign Workspace",
                "slug": FOREIGN_WORKSPACE_SLUG,
            },
        )
        connection.execute(
            text(
                'INSERT INTO "TeamMember" '
                '("id", "teamId", "userId", "role", "createdAt", "updatedAt") '
                "VALUES (:id, :team_id, :user_id, 'OWNER', NOW(), NOW()) "
                'ON CONFLICT ("teamId", "userId") DO UPDATE SET '
                '"role" = EXCLUDED."role", "updatedAt" = EXCLUDED."updatedAt"'
            ),
            {
                "id": FOREIGN_OWNER_MEMBER_ID,
                "team_id": FOREIGN_WORKSPACE_ID,
                "user_id": FOREIGN_OWNER_USER_ID,
            },
        )

        leaked_accounts = connection.execute(
            text(
                'SELECT u."email" FROM "TeamMember" tm '
                'JOIN "User" u ON u."id" = tm."userId" '
                'WHERE tm."teamId" = :team_id '
                'AND u."email" IN (:owner_email, :member_email)'
            ),
            {
                "team_id": FOREIGN_WORKSPACE_ID,
                "owner_email": owner_email,
                "member_email": member_email,
            },
        ).scalars().all()
        if leaked_accounts:
            raise RuntimeError(
                "Localization test actors unexpectedly belong to the foreign Workspace"
            )


def _seed_foreign_domain_workspace(domain_engine) -> None:
    """Mirror the foreign control Workspace into the domain projection."""
    with domain_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces "
                "(id, name, default_currency, authorization_epoch, "
                "entitlement_version, projection_version, lifecycle, "
                "budget_currency, created_at) "
                "VALUES (:id, :name, 'USD', 0, 0, 0, 'active', 'USD', NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "name = EXCLUDED.name, authorization_epoch = 0, "
                "entitlement_version = 0, projection_version = 0, "
                "lifecycle = 'active'"
            ),
            {"id": FOREIGN_WORKSPACE_ID, "name": "Localization Foreign Workspace"},
        )
        connection.execute(
            text(
                "INSERT INTO entitlement_projections "
                "(workspace_id, entitlement_version, lease_sequence, "
                "projection_version, schema_version, period_start, period_end, "
                "resolved_entitlements_json, semantic_hash, issued_at, "
                "valid_until, applied_at) "
                "VALUES (:id, 0, 1, 1, 2, NOW() - INTERVAL '1 day', "
                "NOW() + INTERVAL '29 days', '{}'::jsonb, :semantic_hash, "
                "NOW() - INTERVAL '1 day', NOW() + INTERVAL '30 days', NOW()) "
                "ON CONFLICT (workspace_id) DO UPDATE SET "
                "valid_until = EXCLUDED.valid_until, applied_at = EXCLUDED.applied_at"
            ),
            {"id": FOREIGN_WORKSPACE_ID, "semantic_hash": "e2e80-foreign-entitlement"},
        )
        connection.execute(
            text(
                "INSERT INTO spend_guardrails "
                "(workspace_id, revision, budget_currency, per_operation_limit, "
                "rolling_24h_limit, period_limit, outstanding_exposure_limit, "
                "created_at, updated_at, updated_by) "
                "VALUES (:id, 1, 'USD', 100, 500, 5000, 1000, "
                "NOW(), NOW(), 'e2e80-setup') "
                "ON CONFLICT (workspace_id) DO NOTHING"
            ),
            {"id": FOREIGN_WORKSPACE_ID},
        )


def _withdraw_truth_policy_version(
    context,
    version_id: str,
    *,
    session_factory=None,
    version_model=None,
    withdraw_command=None,
) -> dict[str, object]:
    """Withdraw one exact in-Workspace Truth policy through the domain service."""
    if session_factory is None or version_model is None:
        from app.adapters.persistence.database import tenant_session
        from app.adapters.persistence.models import LibraryVersionRecord

        session_factory = session_factory or tenant_session
        version_model = version_model or LibraryVersionRecord
    if withdraw_command is None:
        from app.library.service import withdraw_version

        withdraw_command = withdraw_version

    with session_factory(context) as session:
        version = session.get(version_model, version_id)
        if version is None or version.workspace_id != context.workspace_id:
            raise RuntimeError(
                "Truth policy version does not exist in the configured E2E Workspace"
            )
        if version.entry_type != TRUTH_POLICY_ENTRY_TYPE:
            raise RuntimeError(
                "Version is not a truth_resolution_policy Library version"
            )
        previous_state = version.state

    withdraw_command(
        context,
        version_id,
        reason=WITHDRAW_REASON,
    )

    with session_factory(context) as session:
        withdrawn = session.get(version_model, version_id)
        if withdrawn is None:
            raise RuntimeError("Withdrawn Truth policy version disappeared")
        return {
            "action": "withdraw_truth_policy_version",
            "entryType": withdrawn.entry_type,
            "lifecycleRevision": withdrawn.lifecycle_revision,
            "previousState": previous_state,
            "state": withdrawn.state,
            "versionId": withdrawn.id,
            "workspaceId": withdrawn.workspace_id,
        }


def main(argv: list[str] | None = None) -> None:
    arguments = _arguments(argv)
    environment = _required_environment()

    # Import only after validating connection and tenant-signing inputs so a
    # misconfigured run fails once with the complete missing-variable list,
    # before the application creates its database engine.
    from app.adapters.persistence.database import engine, tenant_session
    from app.adapters.persistence.models import WorkspaceRecord
    from app.localization.claims import create_inventory_from_analysis
    from app.platform.tenancy import TenantContext
    from tests.support.truth_sources import publish_truth_sources
    from tests.test_localization_batch import (
        _build_confirmed_winner,
        _make_market_policy_version,
        _seed_review,
    )

    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "Localization E2E setup requires PostgreSQL; set MARKETAI_DATABASE_URL "
            "to the same database used by FastAPI"
        )

    workspace_id = environment["E2E_LOCALIZATION_WORKSPACE_ID"]
    initiator_id = environment["E2E_OWNER_USER_ID"]
    authorization_epoch = int(
        os.environ.get("E2E_LOCALIZATION_AUTHORIZATION_EPOCH", "0")
    )
    ctx = TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=initiator_id,
        initiator_role="owner",
        authorization_epoch=authorization_epoch,
    )

    with tenant_session(ctx) as session:
        workspace = session.get(WorkspaceRecord, workspace_id)
        if workspace is None:
            raise RuntimeError(
                f"Workspace {workspace_id!r} is missing from the domain database"
            )
        if workspace.authorization_epoch != authorization_epoch:
            raise RuntimeError(
                "E2E_LOCALIZATION_AUTHORIZATION_EPOCH does not match the domain "
                f"Workspace projection ({workspace.authorization_epoch})"
            )

    if arguments.command == "withdraw-version":
        result = _withdraw_truth_policy_version(
            ctx,
            arguments.version_id,
        )
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return

    control_engine = create_engine(
        _sqlalchemy_postgres_url(
            environment["E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL"]
        ),
        pool_pre_ping=True,
    )
    try:
        if control_engine.dialect.name != "postgresql":
            raise RuntimeError(
                "Localization E2E control setup requires PostgreSQL"
            )
        _seed_foreign_control_workspace(control_engine, environment)
    finally:
        control_engine.dispose()
    _seed_foreign_domain_workspace(engine)

    # A random tag makes every Playwright project/run independent and lets the
    # test prove IDs came from this run rather than a long-lived final-state seed.
    tag = f"e2e80{uuid4().hex[:8]}"
    product_id = f"prod_{tag}"
    winner = _build_confirmed_winner(
        workspace_id, ctx, tag, product_id=product_id
    )
    _seed_review(ctx, winner["asset_version_id"], product_id=product_id)

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
                # The source-locale projection contains "durable". This one
                # market therefore exercises a real blocking deterministic-QC
                # record and item-scoped retry without a browser/API mock.
                market_policy.qc_rules_json = {"forbidden_terms": ["durable"]}
            markets.append(
                {
                    "market": market,
                    "country": country,
                    "expectedReadiness": readiness,
                    "mockScenario": scenario,
                    "marketPolicyVersionId": market_policy.id,
                    "failsLockedCopyQc": fails_locked_copy_qc,
                }
            )
    # The policy lists the Mock DataSource by entry id (#70); both pass Library
    # publish, and start-preparation derives the source from the policy.
    truth_sources = publish_truth_sources(
        ctx,
        tag,
        data_source_content={
            "adapter_kind": "mock",
            "execution_mode": "mock",
            "config": {
                "mock_scenario": "standard_9_market",
                "mock_scenarios_by_country": {
                    market["country"]: market["mockScenario"] for market in markets
                },
            },
        },
        fact_rules={"price": "block", "availability": "block", "discount": "skip"},
    )
    with tenant_session(ctx) as session:
        inventory = create_inventory_from_analysis(
            session,
            ctx,
            asset_version_id=winner["asset_version_id"],
            analysis_recipe_hash="localization-analysis-v1",
            claims=[
                {
                    "claim_type": "headline",
                    "scope_key": f"headline:{tag}",
                    "ocr_text": "A durable source headline",
                    "confidence": 0.99,
                    "risk": "low",
                    "conflicting": False,
                    "market_sensitive": False,
                }
            ],
            provider="controlled-mock-vision",
            model="localization-analysis-v1",
            rule_version="claims-rule-v1",
            analysis_execution_mode="mock",
            adapter_version="mock-v1",
            mock_scenario="default",
            mock_version="v1",
        )
        result = {
            "assetVersionId": winner["asset_version_id"],
            "decisionId": winner["decision_id"],
            "foreignWorkspaceId": FOREIGN_WORKSPACE_ID,
            "foreignWorkspaceSlug": FOREIGN_WORKSPACE_SLUG,
            "inventoryId": inventory.id,
            # Keep the singular fields for old callers while the real #80
            # journey consumes the explicit three-market readiness matrix.
            "market": markets[0]["market"],
            "marketPolicyVersionId": markets[0]["marketPolicyVersionId"],
            "markets": markets,
            "truthPolicyVersionId": truth_sources.policy_version_id,
            "dataSourceVersionId": truth_sources.data_source_version_id,
        }

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
