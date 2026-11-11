"""Seed and advance PostgreSQL prerequisites for the Scale E2E suite
(issue #87 Task 9).

Mirrors `web/e2e/experiments/seed_prerequisites.py` and
`web/e2e/localization/seed_prerequisites.py`: this script never creates a
Workspace or Team -- it mutates durable domain data *inside* the existing
E2E Workspace the pre-authenticated `owner` Playwright storage state
already belongs to (`web/e2e/workspace/fixtures.ts` /
`playwright.config.ts`'s `chromium` project). Every invocation is tagged
with a fresh random suffix so concurrent or repeated runs never collide.

Unlike the Experiments/Localization scripts, this one is a small CLI with
four subcommands rather than one bare entry point, because the
Controlled-Mock golden journey (`controlled-mock-journey.spec.ts`) needs to
mutate durable evidence *between* browser steps, not just once up front:

  seed-controlled-mock
    Builds a confirmed Winner (frozen/judged/confirmed Experiment, approved
    AssetVersion), governed Product/OfferVersions, a `controlled_mock`
    MetaConnection + fault-injectable ScenarioVersion, a matching
    ScaleDeliveryTargetRecord, the activation-evidence prerequisites
    (EvidenceAttestation + a metric-semantic contract set carrying all
    three contract types this suite's manifests need). It publishes no
    ScalePolicyVersion: the suite evaluates under the Beta default the
    migrations published (#127). Returns every id the Controlled-Mock
    journey needs to drive the real UI from "create Draft Enrollment"
    onward.

  seed-customer-sandbox
    The same chain, bound to `customer_sandbox` instead, for
    `customer-sandbox-smoke.spec.ts`. Injects no fault config (spec §4.5:
    the Sandbox smoke "does not inject faults or rewrite the fixed Sandbox
    scenario").

  advance-manifest
    Called once per evidence step in the golden journey. Given an already
    real, browser-activated Enrollment/Episode (there is no service module
    for evidence ingestion in this codebase yet -- see
    `app/scale/v1_router.py`'s module docstring -- so this is the same
    out-of-band, test-harness-only seam `tests/test_scale_evidence.py`'s
    own fixture builders use, never the public Scale OpenAPI), seeds one
    additional day of daily/exact-window/delivery-configuration evidence
    sources plus a source manifest, then drives the *real* durable
    orchestration entry points
    (`app.scale.service.request_scale_evaluation` /
    `run_scale_evaluation` -- the exact two calls
    `app.scale.worker.ScaleWorkerRuntime._scale_evaluate` makes) to produce
    a real Evaluation, Episode projection update, and Advice lifecycle
    transition. `--kind blocked` instead marks the manifest `blocked`
    (spec §4.5 step 5's terminal blocked-manifest scenario) and skips
    building any evidence at all, mirroring
    `tests/test_scale_evidence.py::TestSealSnapshotBlockedManifest`. Either
    kind moves the Episode's readiness projection through the source
    collector's own step (`app.scale.collector._advance_readiness`, #114);
    a blocked one first records a `transport_exhausted` source issue.

Required environment (see also `web/e2e/scale/helpers.ts`,
`web/e2e/scale/fixtures.ts`):
  - E2E_SCALE_ALLOW_MUTATIONS=1     (explicit opt-in; refuses otherwise)
  - E2E_SCALE_WORKSPACE_ID          (domain Workspace id, e.g. workspace-a's)
  - E2E_SCALE_WORKSPACE_SLUG        (its BFF slug, e.g. "workspace-a")
  - E2E_OWNER_USER_ID               (the storage-state owner's user id)
  - MARKETAI_DATABASE_URL           (same PostgreSQL FastAPI/Worker use)
  - MARKETAI_TENANT_CONTEXT_SECRET
  - E2E_SCALE_AUTHORIZATION_EPOCH   (optional, default "0"; must match the
    domain Workspace projection's current `authorization_epoch`)
"""
from __future__ import annotations

import argparse
import hashlib
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
    "E2E_SCALE_ALLOW_MUTATIONS",
    "E2E_SCALE_WORKSPACE_ID",
    "E2E_SCALE_WORKSPACE_SLUG",
    "E2E_OWNER_USER_ID",
    "MARKETAI_DATABASE_URL",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)
MUTATION_OPT_IN = "E2E_SCALE_ALLOW_MUTATIONS"
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
        raise RuntimeError("Scale E2E mutation preflight failed; " + "; ".join(problems))
    return values


def _tenant_context(environment: dict[str, str], *, meta_mode: str | None = None):
    from app.platform.tenancy import TenantContext

    authorization_epoch = int(os.environ.get("E2E_SCALE_AUTHORIZATION_EPOCH", "0"))
    return TenantContext(
        workspace_id=environment["E2E_SCALE_WORKSPACE_ID"],
        role="owner",
        initiator_id=environment["E2E_OWNER_USER_ID"],
        initiator_role="owner",
        executor=f"user:{environment['E2E_OWNER_USER_ID']}",
        request_id="e2e-scale-seed",
        trace_id="e2e-scale-seed",
        authorization_epoch=authorization_epoch,
        meta_mode=meta_mode,
    )


def _assert_workspace(environment: dict[str, str]) -> None:
    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import WorkspaceRecord

    ctx = _tenant_context(environment)
    with tenant_session(ctx) as session:
        workspace = session.get(WorkspaceRecord, ctx.workspace_id)
        if workspace is None:
            raise RuntimeError(f"Workspace {ctx.workspace_id!r} is missing from the domain database")
        if workspace.authorization_epoch != ctx.authorization_epoch:
            raise RuntimeError(
                "E2E_SCALE_AUTHORIZATION_EPOCH does not match the domain "
                f"Workspace projection ({workspace.authorization_epoch})"
            )


# ---------------------------------------------------------------------------
# seed-controlled-mock / seed-customer-sandbox
# ---------------------------------------------------------------------------


def _seed_full_activation_evidence(session, ctx, *, delivery_target_id: str, meta_mode: str, tag: str) -> str:
    """Extends `tests.support.scale_candidate_fixtures.
    seed_activation_evidence_prereqs`'s single `delivery_configuration`
    contract member with the `daily` and `exact_window` members
    `advance-manifest` also needs -- all three keyed to the *same*
    `ScaleDeliveryTargetRecord.contract_generation_id`, matching
    `app.scale.enrollment._resolve_metric_semantic_contract_set_id`'s
    requirement that exactly one contract set's `delivery_configuration`
    member wrap that generation. Returns the contract_set_id."""
    from sqlalchemy import select

    from app.scale.models import (
        ScaleDeliveryTargetRecord,
        ScaleMetricSemanticContractSetMemberRecord,
    )

    target = session.get(ScaleDeliveryTargetRecord, delivery_target_id)
    contract_generation_id = target.contract_generation_id

    contract_set_id = session.execute(
        select(ScaleMetricSemanticContractSetMemberRecord.contract_set_id).filter_by(
            observation_contract_generation=contract_generation_id,
            contract_type="delivery_configuration",
        )
    ).scalar_one()

    for contract_type in ("daily", "exact_window"):
        session.add(ScaleMetricSemanticContractSetMemberRecord(
            id=f"smcm_{tag}_{contract_type}",
            contract_set_id=contract_set_id,
            contract_type=contract_type,
            observation_contract_generation=contract_generation_id,
            contract_hash=f"{contract_type}-hash-{tag}",
        ))
    session.flush()
    return contract_set_id


def _platform_policy_version_id(session) -> str:
    """The Policy a live ``customer_sandbox`` run freezes: the Beta default
    the migrations published (#127). ``advance-manifest`` evaluates under it
    explicitly in either mode -- explicit evaluation does not read
    ``mode_permissions``, which keep ``controlled_mock`` out of live runs."""
    from app.scale.live_trigger import eligible_live_policy

    policy = eligible_live_policy(session, "customer_sandbox")
    if policy is None:
        raise SystemExit(
            "no published ScalePolicyVersion permits customer_sandbox: "
            "migrate the domain database to head (#127)"
        )
    return policy.id


WORKER_WAIT_SECONDS = 90


def _approve_binding_the_worker_leaves(ctx, fixture_binding_id: str) -> str:
    """Approve the binding that is active once the stack's Worker is done.

    ``build_ready_winner_and_target`` confirms a Localization batch, which
    enqueues the item's Jobs, and then publishes and approves a binding
    itself -- the Worker's part in pytest, where no Worker runs. On a Compose
    stack the real Worker runs those Jobs too: it publishes the localized
    child, whose pending-review binding replaces the fixture's, and the Winner
    then fails ``binding_not_ready`` (#19). So wait until every Job of the
    batch and its items has finished, ``localization.fold_batch`` included,
    and give the binding active then the review the fixture gave its own.
    Returns that binding's id.
    """
    import time

    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        JobRecord,
        LocalizationItemRecord,
        MarketAssetBindingRecord,
    )

    with tenant_session(ctx) as session:
        fixture_binding = session.get(MarketAssetBindingRecord, fixture_binding_id)
        batch_id = fixture_binding.batch_id
        binding_key = {
            "source_asset_version_id": fixture_binding.source_asset_version_id,
            "target_market": fixture_binding.target_market,
            "offer_version_id": fixture_binding.offer_version_id,
        }

    deadline = time.monotonic() + WORKER_WAIT_SECONDS
    while True:
        with tenant_session(ctx) as session:
            resource_ids = [batch_id, *session.scalars(
                select(LocalizationItemRecord.id).where(
                    LocalizationItemRecord.workspace_id == ctx.workspace_id,
                    LocalizationItemRecord.batch_id == batch_id,
                )
            )]
            jobs = session.execute(
                select(JobRecord.type, JobRecord.finished_at).where(
                    JobRecord.workspace_id == ctx.workspace_id,
                    JobRecord.resource_id.in_(resource_ids),
                )
            ).all()
        folded = any(
            job.type == "localization.fold_batch" and job.finished_at is not None
            for job in jobs
        )
        if folded and all(job.finished_at is not None for job in jobs):
            break
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Localization batch {batch_id} still has unfinished Jobs after "
                f"{WORKER_WAIT_SECONDS}s; the Scale seed needs the stack's Worker running"
            )
        time.sleep(0.5)

    with tenant_session(ctx) as session:
        binding = session.scalars(
            select(MarketAssetBindingRecord).filter_by(
                workspace_id=ctx.workspace_id, active=True, **binding_key,
            )
        ).one()
        binding.review_state = "approved"
        binding.ready_for_experiment = True
        session.flush()
        return binding.id


def _seed_chain(environment: dict[str, str], *, target_meta_mode: str, fault_config: dict | None) -> dict:
    from app.adapters.persistence.database import tenant_session
    from app.meta.models import ScenarioVersionRecord
    from app.platform.canonical import canonical_hash
    from tests.support.scale_candidate_fixtures import (
        build_ready_winner_and_target,
        ensure_governance_prereqs,
        seed_activation_evidence_prereqs,
    )

    ctx = _tenant_context(environment)
    tag = f"e87t9{uuid4().hex[:8]}"
    product_id = f"prod_{tag}"

    ensure_governance_prereqs(ctx, product_id)

    chain = build_ready_winner_and_target(ctx, product_id, tag, target_meta_mode=target_meta_mode)
    chain["binding_id"] = _approve_binding_the_worker_leaves(ctx, chain["binding_id"])

    target_ctx = _tenant_context(environment, meta_mode=target_meta_mode)
    seed_activation_evidence_prereqs(
        target_ctx, delivery_target_id=chain["delivery_target_id"], meta_mode=target_meta_mode, tag=tag,
    )

    with tenant_session(target_ctx) as session:
        contract_set_id = _seed_full_activation_evidence(
            session, target_ctx, delivery_target_id=chain["delivery_target_id"],
            meta_mode=target_meta_mode, tag=tag,
        )
        policy_version_id = _platform_policy_version_id(session)

        if fault_config and target_meta_mode == "controlled_mock":
            from app.scale.models import ScaleDeliveryTargetRecord

            target_row = session.get(ScaleDeliveryTargetRecord, chain["delivery_target_id"])
            scenario = session.get(ScenarioVersionRecord, target_row.scenario_version_id)
            scenario.scenario_config = fault_config
            scenario.scenario_hash = canonical_hash(fault_config)
            session.flush()

    return {
        "workspaceId": ctx.workspace_id,
        "workspaceSlug": environment["E2E_SCALE_WORKSPACE_SLUG"],
        "userId": environment["E2E_OWNER_USER_ID"],
        "tag": tag,
        "metaMode": target_meta_mode,
        "productId": product_id,
        "productVersionId": chain["product_version_id"],
        "offerVersionId": chain["offer_version_id"],
        "marketCode": chain["market"],
        "assetVersionId": chain["asset_version_id"],
        "decisionId": chain["decision_id"],
        "deliveryTargetId": chain["delivery_target_id"],
        "bindingId": chain["binding_id"],
        "contractSetId": contract_set_id,
        "policyVersionId": policy_version_id,
    }


# ---------------------------------------------------------------------------
# advance-manifest
# ---------------------------------------------------------------------------


def _get_or_create_daily(session, *, ctx, episode, local_day: date, revision: int, metrics: dict):
    from sqlalchemy import select

    from app.adapters.persistence.models import new_id
    from app.scale.models import ScaleDailyMetricSourceRecord

    existing = session.execute(
        select(ScaleDailyMetricSourceRecord).filter_by(
            workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
            episode_id=episode.id, local_day=local_day,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = ScaleDailyMetricSourceRecord(
        id=new_id("sdms"),
        workspace_id=ctx.workspace_id,
        meta_mode=ctx.meta_mode,
        meta_connection_id=episode.meta_connection_id,
        scenario_scope_key=episode.scenario_scope_key,
        ad_account_id=episode.ad_account_id,
        episode_id=episode.id,
        source_revision=revision,
        contract_generation_id="gen-e2e",
        local_day=local_day,
        state="present",
        delivered=True,
        spend=Decimal(str(metrics.get("spend", "20"))),
        impressions=int(metrics.get("impressions", 2000)),
        link_clicks=int(metrics.get("clicks", 60)),
        purchases=int(metrics.get("purchases", 2)),
        purchase_value=Decimal(str(metrics.get("purchase_value", "80"))),
        currency=episode.currency,
        timezone_name=episode.timezone_name,
        raw_response_hash=hashlib.sha256(f"daily:{episode.id}:{local_day}".encode()).hexdigest(),
        sync_run_id=f"sync_{episode.id}_{local_day}",
        occurrence_id=f"occ_daily_{episode.id}_{local_day}",
        call_intent_id=f"call_daily_{episode.id}_{local_day}",
        observed_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _exact_window(session, *, ctx, episode, role: str, start: date, end: date, revision: int, metrics: dict):
    from app.adapters.persistence.models import new_id
    from app.scale.models import ScaleExactWindowSourceRecord

    row = ScaleExactWindowSourceRecord(
        id=new_id("sews"),
        workspace_id=ctx.workspace_id,
        meta_mode=ctx.meta_mode,
        meta_connection_id=episode.meta_connection_id,
        scenario_scope_key=episode.scenario_scope_key,
        ad_account_id=episode.ad_account_id,
        episode_id=episode.id,
        source_revision=revision,
        contract_generation_id="gen-e2e",
        window_role=role,
        window_start_local_day=start,
        window_end_local_day=end,
        reach=int(metrics.get("reach", 1000)),
        frequency=Decimal(str(metrics.get("frequency", "1.5"))),
        spend=Decimal(str(metrics.get("spend", "140"))),
        impressions=int(metrics.get("impressions", 14000)),
        link_clicks=int(metrics.get("clicks", 420)),
        purchases=int(metrics.get("purchases", 14)),
        purchase_value=Decimal(str(metrics.get("purchase_value", "560"))),
        currency=episode.currency,
        timezone_name=episode.timezone_name,
        raw_response_hash=hashlib.sha256(f"exact:{episode.id}:{role}:{revision}".encode()).hexdigest(),
        sync_run_id=f"sync_{episode.id}_{role}_{revision}",
        occurrence_id=f"occ_exact_{episode.id}_{role}_{revision}",
        call_intent_id=f"call_exact_{episode.id}_{role}_{revision}",
        observed_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _delivery_config(session, *, ctx, episode, revision: int):
    from sqlalchemy import select

    from app.adapters.persistence.models import new_id
    from app.scale.models import ScaleDeliveryConfigSourceRecord

    # Activation already recorded the Episode's baseline delivery
    # configuration as source revision 1 (app/scale/enrollment.py), and
    # uq_sdcs_revision allows one row per Episode revision: reuse it, as
    # _get_or_create_daily does for daily rows.
    existing = session.execute(
        select(ScaleDeliveryConfigSourceRecord).filter_by(
            workspace_id=ctx.workspace_id, meta_mode=ctx.meta_mode,
            episode_id=episode.id, source_revision=revision,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = ScaleDeliveryConfigSourceRecord(
        id=new_id("sdcs"),
        workspace_id=ctx.workspace_id,
        meta_mode=ctx.meta_mode,
        meta_connection_id=episode.meta_connection_id,
        scenario_scope_key=episode.scenario_scope_key,
        ad_account_id=episode.ad_account_id,
        episode_id=episode.id,
        source_revision=revision,
        contract_generation_id="gen-e2e",
        ad_creative_id=f"creative_{episode.id}",
        campaign_remote_id=episode.campaign_remote_id or f"camp_{episode.id}",
        ad_set_remote_id=episode.ad_set_remote_id or f"adset_{episode.id}",
        ad_remote_id=episode.ad_remote_id or f"ad_{episode.id}",
        campaign_status="ACTIVE",
        ad_set_status="ACTIVE",
        ad_status="ACTIVE",
        budget_source="ad_set",
        budget_amount=Decimal("50"),
        currency=episode.currency,
        optimization_goal="OFFSITE_CONVERSIONS",
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        attribution_setting="7d_click_1d_view",
        targeting_safety_hash="t" * 64,
        evidence_hash=episode.delivery_evidence_hash,
        comparability_hash=episode.delivery_comparability_hash,
        raw_response_hash=hashlib.sha256(f"delivery:{episode.id}:{revision}".encode()).hexdigest(),
        sync_run_id=f"sync_{episode.id}_delivery_{revision}",
        occurrence_id=f"occ_delivery_{episode.id}_{revision}",
        call_intent_id=f"call_delivery_{episode.id}_{revision}",
        observed_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def advance_manifest(
    environment: dict[str, str], *, meta_mode: str, enrollment_id: str, episode_id: str,
    policy_version_id: str, kind: str, day_index: int, trigger_kind: str,
    target_version_id: str | None, metrics: dict, baseline_metrics: dict | None,
) -> dict:
    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import new_id
    from app.platform.tenancy import commit_and_rebind
    from app.scale.collector import TRANSPORT_EXHAUSTED, _advance_readiness, _blocked
    from app.scale.models import (
        ScaleAdviceThreadRecord,
        ScaleEpisodeRecord,
        ScaleEvaluationRecord,
        ScaleEvidenceSourceManifestRecord,
        ScaleManifestItemRecord,
        ScaleSourceQualityIssueRecord,
    )
    from app.scale.service import request_scale_evaluation, run_scale_evaluation, ScaleEvaluationRequest

    ctx = _tenant_context(environment, meta_mode=meta_mode)
    as_of: date | None = None

    with tenant_session(ctx) as session:
        episode = session.execute(
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=ctx.workspace_id, meta_mode=meta_mode, id=episode_id,
            )
        ).scalar_one()
        as_of = episode.started_local_day + timedelta(days=day_index - 1)

        manifest = ScaleEvidenceSourceManifestRecord(
            id=new_id("sesm"),
            workspace_id=ctx.workspace_id,
            meta_mode=meta_mode,
            meta_connection_id=episode.meta_connection_id,
            scenario_scope_key=episode.scenario_scope_key,
            episode_id=episode.id,
            target_local_day=as_of,
            source_revision=day_index,
            source_generation=1,
            state="blocked" if kind == "blocked" else "complete",
            contract_request_set_hash=hashlib.sha256(f"crs:{episode.id}:{day_index}".encode()).hexdigest(),
            last_complete_local_day=None if kind == "blocked" else as_of,
            safe_watermark=as_of,
            terminal_at=datetime.now(timezone.utc),
            canonical_hash=hashlib.sha256(f"manifest:{episode.id}:{day_index}:{kind}".encode()).hexdigest(),
        )
        session.add(manifest)
        session.flush()

        if kind != "blocked":
            items = []
            for offset in range(day_index):
                day = episode.started_local_day + timedelta(days=offset)
                day_metrics = metrics if offset == day_index - 1 else {}
                daily = _get_or_create_daily(
                    session, ctx=ctx, episode=episode, local_day=day, revision=day_index,
                    metrics=day_metrics,
                )
                items.append(ScaleManifestItemRecord(
                    id=new_id("smi"), workspace_id=ctx.workspace_id, meta_mode=meta_mode,
                    manifest_id=manifest.id, contract_type="daily", local_day=day,
                    state="fulfilled", source_record_id=daily.id,
                ))

            current_start = as_of - timedelta(days=6)
            current_end = as_of + timedelta(days=1)
            baseline_start = current_start - timedelta(days=7)
            for role, start, end, role_metrics in (
                ("current_7d", current_start, current_end, metrics),
                ("baseline_7d", baseline_start, current_start, baseline_metrics or metrics),
                ("rolling_14d", baseline_start, current_end, metrics),
            ):
                exact = _exact_window(
                    session, ctx=ctx, episode=episode, role=role, start=start, end=end,
                    revision=day_index, metrics=role_metrics,
                )
                items.append(ScaleManifestItemRecord(
                    id=new_id("smi"), workspace_id=ctx.workspace_id, meta_mode=meta_mode,
                    manifest_id=manifest.id, contract_type="exact_window", window_role=role,
                    state="fulfilled", source_record_id=exact.id,
                ))

            delivery = _delivery_config(session, ctx=ctx, episode=episode, revision=day_index)
            items.append(ScaleManifestItemRecord(
                id=new_id("smi"), workspace_id=ctx.workspace_id, meta_mode=meta_mode,
                manifest_id=manifest.id, contract_type="delivery_configuration",
                state="fulfilled", source_record_id=delivery.id,
            ))
            session.add_all(items)
            session.flush()

        # The source collector's readiness step (#109, #114): a blocked
        # manifest carries its typed issue and moves the Episode's readiness
        # to insufficient/blocked with the recovery action; a complete one
        # moves it on again. The same function the collector calls, so the
        # evidence page reads what production writes.
        issues = [] if kind != "blocked" else [
            _blocked(TRANSPORT_EXHAUSTED, f"e2e: the read of {as_of.isoformat()} failed after its retries"),
        ]
        session.add_all([
            ScaleSourceQualityIssueRecord(
                id=new_id("ssqi"), workspace_id=ctx.workspace_id, meta_mode=meta_mode,
                manifest_id=manifest.id, code=issue.code, severity="blocking", is_blocking=True,
                contract_type=issue.contract_type, window_role=issue.window_role, local_day=issue.local_day,
                detail=issue.detail, recovery_action=issue.recovery_action,
            )
            for issue in issues
        ])
        session.flush()
        _advance_readiness(session, ctx, episode, manifest, issues)

        request = ScaleEvaluationRequest(
            enrollment_id=enrollment_id,
            episode_id=episode_id,
            target_local_day=as_of,
            trigger_kind=trigger_kind,
            source_manifest_id=manifest.id,
            source_manifest_hash=manifest.canonical_hash,
            source_revision=day_index,
            freshness_reference_at=datetime.now(timezone.utc),
            policy_version_id=policy_version_id,
            algorithm_version="beta-v1",
            target_version_id=target_version_id,
            semantic_event_sequence=0,
            replay_generation=0,
            original_initiator_id="e2e-harness",
        )
        run_id = request_scale_evaluation(session, ctx, request)
        # The request is durable before a Worker runs it, so commit here as
        # the API does. A blocked manifest makes `run_scale_evaluation` roll
        # back its open transaction, which would otherwise discard this
        # manifest and run too (#19).
        commit_and_rebind(session)
        outcome = run_scale_evaluation(session, ctx, run_id)

        evaluation = None
        if outcome.evaluation_id:
            evaluation = session.execute(
                select(ScaleEvaluationRecord).filter_by(
                    workspace_id=ctx.workspace_id, meta_mode=meta_mode, id=outcome.evaluation_id,
                )
            ).scalar_one_or_none()

        threads = session.execute(
            select(ScaleAdviceThreadRecord)
            .filter_by(workspace_id=ctx.workspace_id, meta_mode=meta_mode, enrollment_id=enrollment_id)
            .order_by(ScaleAdviceThreadRecord.created_at)
        ).scalars().all()

        session.refresh(episode)

        return {
            "runId": run_id,
            "outcomeKind": outcome.kind.value,
            "outcomeDetail": outcome.detail,
            "evaluationId": outcome.evaluation_id,
            "asOf": as_of.isoformat(),
            "episodePhase": episode.phase,
            "maturity": evaluation.maturity if evaluation else None,
            "health": evaluation.health if evaluation else None,
            "healthReason": evaluation.health_reason if evaluation else None,
            "dataQualityState": evaluation.data_quality_state if evaluation else None,
            "adviceThreadIds": [t.id for t in threads],
            "adviceThreadLifecycles": {t.id: t.lifecycle for t in threads},
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    seed_mock = sub.add_parser("seed-controlled-mock")
    seed_mock.add_argument("--fault-config", type=str, default=None)

    sub.add_parser("seed-customer-sandbox")

    advance = sub.add_parser("advance-manifest")
    advance.add_argument("--meta-mode", required=True)
    advance.add_argument("--enrollment-id", required=True)
    advance.add_argument("--episode-id", required=True)
    advance.add_argument("--policy-version-id", required=True)
    advance.add_argument("--kind", choices=["complete", "blocked"], default="complete")
    advance.add_argument("--day-index", type=int, required=True)
    advance.add_argument("--trigger-kind", default="live")
    advance.add_argument("--target-version-id", default=None)
    advance.add_argument("--metrics-json", default="{}")
    advance.add_argument("--baseline-metrics-json", default=None)

    args = parser.parse_args()
    environment = _required_environment()
    _assert_workspace(environment)

    if args.command == "seed-controlled-mock":
        fc = json.loads(args.fault_config) if args.fault_config else None
        result = _seed_chain(environment, target_meta_mode="controlled_mock", fault_config=fc)
    elif args.command == "seed-customer-sandbox":
        result = _seed_chain(environment, target_meta_mode="customer_sandbox", fault_config=None)
    elif args.command == "advance-manifest":
        metrics = json.loads(args.metrics_json)
        baseline = json.loads(args.baseline_metrics_json) if args.baseline_metrics_json else None
        result = advance_manifest(
            environment, meta_mode=args.meta_mode, enrollment_id=args.enrollment_id,
            episode_id=args.episode_id, policy_version_id=args.policy_version_id,
            kind=args.kind, day_index=args.day_index, trigger_kind=args.trigger_kind,
            target_version_id=args.target_version_id, metrics=metrics, baseline_metrics=baseline,
        )
    else:  # pragma: no cover
        raise RuntimeError(f"Unknown command {args.command!r}")

    json.dump(result, sys.stdout, sort_keys=True, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
