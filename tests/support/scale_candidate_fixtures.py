"""Shared builder for a fully-ready Winner Decision + matching
``ScaleDeliveryTargetRecord`` pair (spec §4.2-§4.4, issue #86 Task 9).

Every Scale Enrollment Candidate/Enrollment creation path now runs the
complete authoritative lineage gate (``app.scale.enrollment_lineage.
lock_and_validate_enrollment_lineage``): a genuine, fully-eligible Winner
(the light identity chain plus a ready MarketAssetBinding/TruthPackage) and
a field-for-field compatible DeliveryTarget. Building that pair is
expensive and identical machinery whether the caller is
``tests/test_scale_enrollment_candidates.py`` (domain-level tests) or
``tests/test_scale_router.py`` (HTTP-level tests, one fresh Workspace per
test method) -- unlike this repo's usual per-test-file fixture duplication
convention, this one genuinely lives in ``tests/support/`` (alongside
``tests/support/library_fixtures.py``) because both call sites need it
*parameterized by an arbitrary caller-owned Workspace*, not a fixed one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    OperationRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.admission import PaidOperationAdmission
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.policy import EntitlementPolicy
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import ReservationBook
from app.experiments.service import (
    add_entry_v2,
    bind_candidate_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.experiments.winner_eligibility import _versioned_hash
from app.localization.batch import (
    BatchMarketTargetInput,
    BatchSourceInput,
    approve_copy,
    confirm,
    create_draft,
    start_preparation,
    submit_claim_decisions,
)
from app.localization.binding import publish_binding
from app.localization.claims import ClaimDecisionInput, create_inventory_from_analysis
from app.localization.evidence_tier import EvidenceTier
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
)
from app.meta.evidence import create_attestation, publish_evidence
from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
from app.meta.observation import create_contract_generation
from app.meta.sync_pipeline import create_sync_run
from app.platform.tenancy import TenantContext
from app.scale.delivery_targets import DeliveryTargetRequest, create_delivery_target
from tests.experiment_governance_fixtures import govern_asset_versions, govern_product_offer_versions
from tests.support.market_codes import next_market_code as _next_market

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}

OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
FAR_FUTURE = OBSERVED_AT + timedelta(days=365)


def ensure_governance_prereqs(ctx: TenantContext, product_id: str) -> None:
    """Idempotently seed the Entitlement/SpendGuardrail rows
    ``app.experiments.service``'s paid-operation gates require, plus the
    owning Workspace/Product. Safe to call once per test class/module."""
    now = datetime.now(timezone.utc)
    with tenant_session(ctx) as s:
        if s.get(WorkspaceRecord, ctx.workspace_id) is None:
            s.add(WorkspaceRecord(id=ctx.workspace_id, name=f"Scale candidate fixture {ctx.workspace_id}"))
            s.flush()
        if s.get(ProductRecord, product_id) is None:
            s.add(ProductRecord(id=product_id, workspace_id=ctx.workspace_id, name="P Scale Candidate"))
            s.flush()
        if s.get(EntitlementProjectionRecord, ctx.workspace_id) is None:
            s.add(EntitlementProjectionRecord(
                workspace_id=ctx.workspace_id, entitlement_version=1, lease_sequence=1,
                projection_version=1, schema_version=2, semantic_hash="h" * 8,
                valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                resolved_entitlements_json={},
            ))
        if s.get(SpendGuardrailRecord, ctx.workspace_id) is None:
            s.add(SpendGuardrailRecord(
                workspace_id=ctx.workspace_id, revision=1, budget_currency="USD",
                per_operation_limit=Decimal("100.00"), rolling_24h_limit=Decimal("500.00"),
                period_limit=Decimal("5000.00"), outstanding_exposure_limit=Decimal("1000.00"),
                created_at=now, updated_at=now, updated_by="user_1",
            ))


def _seed_library_versions(ctx: TenantContext, tag: str, market: str) -> tuple[str, str]:
    pv_entry, ov_entry = f"le_pv_{tag}", f"le_ov_{tag}"
    pv_id, ov_id = f"pv_{tag}", f"ov_{tag}"
    with tenant_session(ctx) as session:
        for entry_id, ver_id, etype in [(pv_entry, pv_id, "product"), (ov_entry, ov_id, "offer")]:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=ctx.workspace_id,
                entry_type=etype, display_name=f"Test {etype} {tag}",
            ))
            session.flush()
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=ctx.workspace_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json={}, content_hash="c" * 64,
                state="effective", published_by="system",
            ))
            session.flush()
    return pv_id, ov_id


def _seed_asset_versions(ctx: TenantContext, product_id: str, tag: str, count: int = 2) -> list[str]:
    with tenant_session(ctx) as s:
        plan_id = f"plan_{tag}"
        if s.get(PlanRecord, plan_id) is None:
            s.add(PlanRecord(
                id=plan_id, workspace_id=ctx.workspace_id,
                product_id=product_id, mode="full", cap=0, total_full=1,
                snapshot_hash="a" * 64,
            ))
            s.flush()

        run_id = f"run_{tag}"
        if s.get(GenerationRunRecord, run_id) is None:
            s.add(GenerationRunRecord(
                id=run_id, workspace_id=ctx.workspace_id,
                product_id=product_id, plan_id=plan_id,
                provider="mock", model="mock", aspect_ratio="1:1",
                image_size="1024", variants=1, use_ref=False,
                status="done", total=1, done=1, failed=0,
                est_cost=Decimal("0"), currency="USD",
            ))
            s.flush()

        av_ids = []
        for i in range(count):
            asset_id = f"asset_{tag}_{i}"
            av_id = f"av_{tag}_{i}"
            if s.get(AssetRecord, asset_id) is None:
                s.add(AssetRecord(
                    id=asset_id, workspace_id=ctx.workspace_id,
                    product_id=product_id, run_id=run_id,
                    combo_id="C01", combo_code="A1", tags_json={},
                    filename=f"{tag}_{i}.png", provider="mock", model="mock",
                    prompt="test", fingerprint=f"fp_{tag}_{i}", status="ok",
                ))
                s.flush()
            if s.get(AssetVersionRecord, av_id) is None:
                s.add(AssetVersionRecord(
                    id=av_id, workspace_id=ctx.workspace_id,
                    asset_id=asset_id, kind="generated", aspect_ratio="1:1",
                    language="en", object_key=f"{tag}_{i}.png",
                    content_sha256=f"{i:0>64x}", byte_size=1, mime_type="image/png",
                ))
                s.flush()
            av_ids.append(av_id)
        govern_asset_versions(s, ctx, product_id=product_id, asset_version_ids=av_ids, tag=tag)
    return av_ids


def _seed_metrics(ctx: TenantContext, tag: str, ad_ids: list[str]) -> None:
    from app.adapters.persistence.models import AdDailyMetricRecord, MetaAccountRecord, MetaSyncRunRecord

    with tenant_session(ctx) as s:
        acct_id = f"macct_{tag}"
        if s.get(MetaAccountRecord, acct_id) is None:
            s.add(MetaAccountRecord(
                id=acct_id, workspace_id=ctx.workspace_id,
                ad_account_id=f"act_{tag}", currency="USD",
                timezone_name="UTC", api_version="v23.0",
            ))
            s.flush()

        sync_id = f"sync_{tag}"
        if s.get(MetaSyncRunRecord, sync_id) is None:
            s.add(MetaSyncRunRecord(
                id=sync_id, workspace_id=ctx.workspace_id,
                account_id=acct_id, state="completed",
                date_start=date(2026, 7, 1), date_stop=date(2026, 8, 1),
                contract_hash="b" * 64, contract_json={},
                api_version="v23.0", account_currency="USD",
                account_timezone="UTC", idempotency_key=f"sync_{tag}",
            ))
            s.flush()

        for idx, ad_id in enumerate(ad_ids):
            for day_offset in range(7):
                d = date(2026, 7, 25) + timedelta(days=day_offset)
                mid = f"met_{tag}_{idx}_{day_offset}"
                if s.get(AdDailyMetricRecord, mid) is None:
                    imp = 1000 * (idx + 1)
                    clk = 50 * (idx + 1) + (10 if idx == 0 else 0)
                    s.add(AdDailyMetricRecord(
                        id=mid, account_id=acct_id, sync_run_id=sync_id,
                        ad_id=ad_id, metric_date=d,
                        spend=Decimal("10.00"), impressions=imp, clicks=clk,
                        purchases=Decimal("2"), purchase_value=Decimal("40"),
                        currency="USD", contract_hash="b" * 64, raw_json={},
                    ))
            s.flush()


def _build_confirmed_winner(ctx: TenantContext, product_id: str, tag: str) -> dict:
    market = _next_market()
    pv_id, ov_id = _seed_library_versions(ctx, tag, market)
    avs = _seed_asset_versions(ctx, product_id, tag)
    with tenant_session(ctx) as s:
        govern_product_offer_versions(
            s, ctx, product_id=product_id,
            product_version_id=pv_id, offer_version_id=ov_id,
            markets=(market,),
        )

    draft_context = {
        "market": market, "currency": "USD", "placement": "feed",
        "product_version_id": pv_id, "offer_version_id": ov_id,
        "budget": {"daily_amount": "100", "total_amount": "3000"},
        "schedule": {
            "start_at": "2026-09-01T00:00:00+00:00",
            "end_at": "2026-12-01T00:00:00+00:00",
            "timezone": "UTC",
        },
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {
            "primary_metric": "ctr", "ctr_threshold": "0.01",
            "alpha": "0.05", "bootstrap_samples": 2000,
        },
    }
    exp = create_experiment_v2(ctx, product_id=product_id, name=f"CAND-{tag}", draft_context=draft_context)
    add_entry_v2(ctx, exp["id"], asset_version_id=avs[0], expected_revision=1)
    add_entry_v2(ctx, exp["id"], asset_version_id=avs[1], expected_revision=2)
    frozen = freeze_experiment_v2(ctx, exp["id"], expected_revision=3)
    rev = frozen["revision"]
    ad_ids = []
    for cand in frozen["candidates"]:
        ad_id = f"ad_{tag}_{cand['position']}"
        ad_ids.append(ad_id)
        bind_candidate_v2(
            ctx, exp["id"], cand["id"],
            ad_account_id=f"act_{tag}_{cand['position']}",
            ad_id=ad_id, expected_revision=rev,
        )
    running = start_experiment_v2(ctx, exp["id"], expected_revision=rev)
    _seed_metrics(ctx, tag, ad_ids)
    j_result = run_judgement_v2(ctx, running["id"], expected_revision=running["revision"], as_of=date(2026, 8, 1))
    conf = confirm_proposal_v2(ctx, running["id"], j_result["proposal_id"], expected_proposal_sequence=1)
    real_decision_id = conf["decisions"][0]["id"]

    with tenant_session(ctx) as s:
        real_decision = s.get(ExperimentDecisionRecord, real_decision_id)
        candidate = s.get(ExperimentVersionCandidateRecord, real_decision.experiment_version_candidate_id)
        experiment_version = s.get(ExperimentVersionRecord, real_decision.experiment_version_id)
        return {
            "tag": tag,
            "market": market,
            "decision_id": real_decision.id,
            "asset_version_id": candidate.asset_version_id,
            "product_version_id": pv_id,
            "offer_version_id": ov_id,
            "landing_page_evidence_hash": experiment_version.landing_page_evidence_hash,
            "attribution_semantics_hash": _versioned_hash(
                "attribution-semantics-v1",
                {
                    "click_window_days": experiment_version.attribution_click_window_days,
                    "view_window_days": experiment_version.attribution_view_window_days,
                },
            ),
        }


def _seed_review(ctx: TenantContext, product_id: str, asset_version_id: str, review_value: str) -> None:
    with tenant_session(ctx) as s:
        av = s.get(AssetVersionRecord, asset_version_id)
        s.add(QualityReviewRecord(
            id=new_id("qr"), workspace_id=ctx.workspace_id,
            product_id=product_id, asset_id=av.asset_id,
            asset_version_id=asset_version_id, review_value=review_value,
            source="human", actor_id="reviewer_1",
        ))
        s.flush()


def _make_market_policy_version(session, market_id: str) -> MarketPolicyVersionRecord:
    # Market policies are platform-global, not Workspace-owned. Reuse an
    # existing fixture across repeated processes against the same PostgreSQL
    # database instead of colliding on ``uq_mp_market_id``.
    existing_policy = session.scalar(
        select(MarketPolicyRecord).where(MarketPolicyRecord.market_id == market_id)
    )
    if existing_policy is not None:
        existing_version = session.scalar(
            select(MarketPolicyVersionRecord)
            .where(
                MarketPolicyVersionRecord.market_policy_id == existing_policy.id,
                MarketPolicyVersionRecord.version_state == "effective",
            )
            .order_by(MarketPolicyVersionRecord.version_number.desc())
        )
        if existing_version is None:
            raise AssertionError(
                f"test market policy {market_id!r} has no effective version"
            )
        return existing_version

    policy = MarketPolicyRecord(
        id=new_id("mp"), market_id=market_id, locale="en-US",
        country=market_id[:2], currency="USD",
    )
    session.add(policy)
    session.flush()
    version = MarketPolicyVersionRecord(
        id=new_id("mpv"), market_policy_id=policy.id, version_number=1, version_state="effective",
        locale="en-US", country=policy.country, currency="USD",
        pricing_context_json={}, digit_format_json={}, base_tone="neutral",
        reserved_words_json=[], restricted_claim_types_json=[],
        requires_local_copy=False, shipping_address_granularity="country",
        max_fact_staleness_seconds=86400, qc_rules_json={},
    )
    session.add(version)
    session.flush()
    return version


def _make_version(ctx: TenantContext, session, entry_type: str, tag: str) -> str:
    entry_id = new_id("le")
    session.add(LibraryEntryRecord(
        id=entry_id, workspace_id=ctx.workspace_id, entry_type=entry_type,
        display_name=f"{entry_type}-{tag}",
    ))
    session.flush()
    version_id = new_id("lv")
    session.add(LibraryVersionRecord(
        id=version_id, workspace_id=ctx.workspace_id, entry_id=entry_id, entry_type=entry_type,
        version_no=1, content_json={}, content_hash=f"hash-{version_id}",
        published_by="tester",
    ))
    session.flush()
    return version_id


def _truth_command_for(
    ctx: TenantContext, tag: str, pv_id: str, ov_id: str, mpv: MarketPolicyVersionRecord, market: str,
) -> TruthResolutionCommand:
    with tenant_session(ctx) as s:
        policy_version_id = _make_version(ctx, s, "truth_resolution_policy", tag)
        data_source_version_id = _make_version(ctx, s, "data_source", tag)
        manual_fact_set_version_id = _make_version(ctx, s, "manual_fact_set", tag)

    now = datetime.now(timezone.utc)
    window = (now - timedelta(days=1), now + timedelta(days=365))
    facts = {"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}}

    step = SourceStep(
        data_source_version_id=data_source_version_id, adapter=ManualFactSourceAdapter(),
        adapter_kind="manual", adapter_version="manual-v1", truth_data_mode="real",
        manual_fact_set=ManualFactSet(
            version_id=manual_fact_set_version_id, facts=facts,
            valid_from=window[0], valid_to=window[1],
        ),
        manual_fact_set_version_id=manual_fact_set_version_id,
    )
    market_cmd = MarketResolutionCommand(
        market=market, locale=mpv.locale, country=mpv.country, currency=mpv.currency,
        market_policy_version_id=mpv.id, sources=(step,),
        fact_policies=(FactPolicy("price"), FactPolicy("availability")),
    )
    return TruthResolutionCommand(
        product_version_id=pv_id, offer_version_id=ov_id,
        truth_resolution_policy_version_id=policy_version_id, truth_data_mode="real",
        max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
        markets=(market_cmd,), now=now,
    )


def _make_curated_scenario_context(ctx: TenantContext, session, tag: str, meta_mode: str) -> dict:
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ctx.workspace_id,
        meta_mode=meta_mode, ad_account_id=f"act_sdt_{tag}",
    )
    session.add(conn)
    session.flush()
    gen = create_contract_generation(
        session, workspace_id=ctx.workspace_id, meta_mode=meta_mode,
        connection_id=conn.id, contract_type="delivery_configuration",
        ad_account_id=f"act_sdt_{tag}", fields=["status"], attribution_window={},
        breakdowns=[], time_window={}, local_timezone="UTC",
    )
    scenario = ScenarioVersionRecord(
        id=new_id("scv"), workspace_id=ctx.workspace_id, meta_mode=meta_mode,
        meta_connection_id=conn.id, scenario_config={}, scenario_hash="h" * 64,
    )
    session.add(scenario)
    session.flush()
    return {
        "meta_connection_id": conn.id,
        "ad_account_id": f"act_sdt_{tag}",
        "contract_generation_id": gen.id,
        "scenario_version_id": scenario.id,
    }


def _make_evidence_observation_context(ctx: TenantContext, session, tag: str) -> dict:
    """``internal_validation``'s source provenance: a real sync run,
    publication and attestation (mirrors
    ``tests/test_scale_delivery_targets.py``'s identical helper)."""
    meta_mode = "internal_validation"
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=ctx.workspace_id,
        meta_mode=meta_mode, ad_account_id=f"act_sdt_{tag}",
    )
    session.add(conn)
    session.flush()
    gen = create_contract_generation(
        session, workspace_id=ctx.workspace_id, meta_mode=meta_mode,
        connection_id=conn.id, contract_type="delivery_configuration",
        ad_account_id=f"act_sdt_{tag}", fields=["status"], attribution_window={},
        breakdowns=[], time_window={}, local_timezone="UTC",
    )
    op = OperationRecord(
        id=new_id("op"), workspace_id=ctx.workspace_id,
        command_type="meta_sync", state="succeeded",
    )
    session.add(op)
    session.flush()
    run = create_sync_run(
        session, workspace_id=ctx.workspace_id, meta_mode=meta_mode,
        contract_generation_id=gen.id, occurrence_id=f"occ_{new_id('x')}",
        operation_id=op.id, executor="system:worker",
    )
    pub = publish_evidence(session, sync_run_id=run.id, operation_id=op.id)
    attestation = create_attestation(
        session, contract_generation_id=gen.id, publication_id=pub.id,
        evidence_watermark="[2026-09-01,2026-09-01]",
    )
    return {
        "meta_connection_id": conn.id,
        "ad_account_id": f"act_sdt_{tag}",
        "contract_generation_id": gen.id,
        "source_sync_run_id": run.id,
        "source_publication_id": pub.id,
        "source_attestation_id": attestation.id,
    }


def seed_landing_page_version(session, ctx: TenantContext, chain: dict, tag: str) -> None:
    """The Landing Page version the Winner's experiment names, which a
    matching DeliveryTarget refers to (#63 makes it in the product)."""
    landing_entry_id = new_id("le")
    session.add(LibraryEntryRecord(
        id=landing_entry_id, workspace_id=ctx.workspace_id,
        entry_type="landing_page", display_name=f"landing-{tag}",
    ))
    session.flush()
    session.add(LibraryVersionRecord(
        id=chain["landing_page_evidence_hash"], workspace_id=ctx.workspace_id,
        entry_id=landing_entry_id, entry_type="landing_page",
        version_no=1, content_json={}, content_hash="d" * 64,
        published_by="test-fixture",
    ))
    session.flush()


def _make_matching_delivery_target(ctx: TenantContext, chain: dict, tag: str, meta_mode: str) -> str:
    is_curated = meta_mode in ("customer_sandbox", "controlled_mock")
    with tenant_session(ctx) as s:
        upstream = (
            _make_curated_scenario_context(ctx, s, tag, meta_mode)
            if is_curated
            else _make_evidence_observation_context(ctx, s, tag)
        )
        seed_landing_page_version(s, ctx, chain, tag)

    request = DeliveryTargetRequest(
        meta_connection_id=upstream["meta_connection_id"],
        ad_account_id=upstream["ad_account_id"],
        # ck_sdt_scenario_biconditional: a controlled_mock target is scoped to
        # its ScenarioVersion, every other mode to the reserved marker.
        scenario_scope_key=(
            upstream["scenario_version_id"]
            if meta_mode == "controlled_mock"
            else "__no_scenario__"
        ),
        campaign_remote_id=f"camp_{tag}",
        adset_remote_id=f"adset_{tag}",
        ad_remote_id=f"ad_{tag}",
        ad_creative_id=f"creative_{tag}",
        creative_asset_version_id=chain["asset_version_id"],
        creative_binding_evidence_hash="e" * 64,
        market_code=chain["market"],
        placement="feed",
        attribution_setting=chain["attribution_semantics_hash"],
        contract_generation_id=upstream["contract_generation_id"],
        offer_version_id=chain["offer_version_id"],
        landing_page_version_id=chain["landing_page_evidence_hash"],
        source_type="curated_scenario" if is_curated else "evidence_observation",
        source_manifest={"schema_version": 1, "source_type": "curated_scenario" if is_curated else "evidence_observation"},
        observed_at=OBSERVED_AT,
        expires_at=FAR_FUTURE,
        scenario_version_id=upstream.get("scenario_version_id"),
        source_sync_run_id=upstream.get("source_sync_run_id"),
        source_publication_id=upstream.get("source_publication_id"),
        source_attestation_id=upstream.get("source_attestation_id"),
    )
    target_ctx = TenantContext(
        workspace_id=ctx.workspace_id, role=ctx.role, executor=ctx.executor,
        initiator_id=ctx.initiator_id, meta_mode=meta_mode,
    )
    with tenant_session(target_ctx) as s:
        target = create_delivery_target(s, target_ctx, request)
        return target.id


def build_ready_winner_and_target(
    ctx: TenantContext,
    product_id: str,
    tag: str,
    *,
    target_meta_mode: str = "customer_sandbox",
    admission: PaidOperationAdmission | None = None,
) -> dict:
    """Full chain, parameterized by an arbitrary caller-owned ``ctx``/
    ``product_id``: a confirmed, fully-eligible Winner Decision (light
    identity chain plus a ready MarketAssetBinding/TruthPackage, spec §4.2
    checks 1-11) plus a field-for-field compatible ``customer_sandbox``
    DeliveryTarget (spec §4.4). Call :func:`ensure_governance_prereqs`
    first.

    Returns a dict with (at least) ``decision_id`` and
    ``delivery_target_id`` -- the exact pair
    ``app.scale.candidates.create_enrollment_candidate`` needs.
    """
    chain = build_ready_winner(ctx, product_id, tag, admission=admission)
    chain["delivery_target_id"] = _make_matching_delivery_target(ctx, chain, tag, target_meta_mode)
    return chain


def build_ready_winner(
    ctx: TenantContext,
    product_id: str,
    tag: str,
    *,
    admission: PaidOperationAdmission | None = None,
) -> dict:
    """:func:`build_ready_winner_and_target` without the DeliveryTarget, for a
    caller that puts the target on a connection of its own (#110: the
    sandbox connection the Scale contract set reads). ``chain`` names what
    a matching target must carry: ``market``, ``asset_version_id``,
    ``offer_version_id``, ``landing_page_evidence_hash`` (the Landing Page
    version id, see :func:`seed_landing_page_version`) and
    ``attribution_semantics_hash``.
    """
    admission = admission or PaidOperationAdmission(
        policy=EntitlementPolicy(),
        quote_engine=CostQuoteEngine(price_registry=PRICE_REGISTRY, price_rule_version="test-v1"),
        reservation_book=ReservationBook(),
    )
    market = None
    chain = _build_confirmed_winner(ctx, product_id, tag)
    market = chain["market"]
    _seed_review(ctx, product_id, chain["asset_version_id"], "approved")

    with tenant_session(ctx) as s:
        mpv = _make_market_policy_version(s, market)
        mpv_id = mpv.id

    with tenant_session(ctx) as s:
        mpv = s.get(MarketPolicyVersionRecord, mpv_id)
        truth_cmd = _truth_command_for(ctx, tag, chain["product_version_id"], chain["offer_version_id"], mpv, market)

    snapshot = {
        "market": market, "placement": "feed", "locale": "en-US",
        "country": market, "currency": "USD",
    }
    with tenant_session(ctx) as s:
        draft = create_draft(
            s, ctx, product_version_id=chain["product_version_id"],
            offer_version_id=chain["offer_version_id"],
            source_market_snapshot=snapshot,
            truth_data_mode="real", provider_execution_mode="mock",
            sources=[BatchSourceInput(
                asset_version_id=chain["asset_version_id"],
                decision_id=chain["decision_id"],
                source_market_snapshot=snapshot,
            )],
            market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv_id, market=market)],
            actor_id="user_1",
        )
        batch_id = draft.batch_id

    recipe_hash = f"recipe-{tag}"
    with tenant_session(ctx) as s:
        create_inventory_from_analysis(
            s, ctx, asset_version_id=chain["asset_version_id"], analysis_recipe_hash=recipe_hash,
            claims=[{
                "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello world",
                "confidence": 0.99, "risk": "low", "conflicting": False,
            }],
            provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
            analysis_execution_mode="mock", adapter_version="mock-v1",
            mock_scenario="default", mock_version="v1",
        )

    with tenant_session(ctx) as s:
        start_preparation(
            s, ctx, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
            analysis_provider="mock-vision", analysis_model="mock-vision-1",
            analysis_adapter_version="mock-v1", actor_id="user_1", admission=admission,
        )

    from app.adapters.persistence.models import LocalizationBatchRecord

    with tenant_session(ctx) as s:
        scd = submit_claim_decisions(
            s, ctx, batch_id=batch_id,
            expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
            asset_version_id=chain["asset_version_id"],
            target_market=market, analysis_recipe_hash=recipe_hash,
            decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
            copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
            translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
            actor_id="user_1", role="owner", admission=admission,
        )
        copy_version_id = scd.copy_version_id

    with tenant_session(ctx) as s:
        approve_copy(
            s, ctx, batch_id=batch_id,
            expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
            copy_version_id=copy_version_id,
            actor_id="user_1", role="owner",
        )

    with tenant_session(ctx) as s:
        expected_revision = s.get(LocalizationBatchRecord, batch_id).revision

    with tenant_session(ctx) as s:
        confirmed = confirm(
            s, ctx, batch_id=batch_id, expected_revision=expected_revision,
            edit_provider="mock-edit", edit_model="mock-edit-1",
            edit_adapter_version="mock-edit-v1", actor_id="user_1", role="owner",
            admission=admission,
        )
    item_id = confirmed.item_ids[0]

    with tenant_session(ctx) as s:
        result = publish_binding(s, ctx, item_id=item_id, batch_id=batch_id, publication_id=None, actor_id="user_1")
        binding_id = result.binding_id

    with tenant_session(ctx) as s:
        binding = s.get(MarketAssetBindingRecord, binding_id)
        binding.review_state = "approved"
        binding.ready_for_experiment = True
        s.add(binding)
        # Truth resolution already created a fresh, full package. It is
        # append-only in PostgreSQL, so readiness setup must never rewrite it.
        s.flush()

    chain["binding_id"] = binding_id
    return chain


def seed_activation_evidence_prereqs(
    ctx: TenantContext, *, delivery_target_id: str, meta_mode: str, tag: str,
) -> None:
    """Seed what issue #86 Task 10/11's ``resolve_activation_evidence``/
    ``app.scale.enrollment._build_episode_from_evidence`` need for a
    DeliveryTarget's exact contract generation, beyond what
    :func:`build_ready_winner_and_target` itself already builds: a valid
    ``EvidenceAttestationRecord`` (mechanical regardless of the target's own
    curated-scenario vs. evidence-observation provenance -- an
    ``evidence_observation`` target already has one from
    ``_make_evidence_observation_context`` above; creating a second,
    equally-valid one here is harmless, ``current_attestation()`` just picks
    the highest-ranked) and a compatible
    ``ScaleMetricSemanticContractSetRecord`` (spec §6.11 step 6, which no
    earlier task populates automatically).
    """
    from app.meta.evidence import create_attestation, publish_evidence
    from app.meta.models import MetaObservationSyncRunRecord
    from app.scale.models import (
        ScaleDeliveryTargetRecord,
        ScaleMetricSemanticContractSetMemberRecord,
        ScaleMetricSemanticContractSetRecord,
    )

    with tenant_session(ctx) as session:
        target = session.get(ScaleDeliveryTargetRecord, delivery_target_id)
        contract_generation_id = target.contract_generation_id

        op = OperationRecord(
            id=new_id("op"), workspace_id=ctx.workspace_id,
            command_type="evidence_publish", state="completed",
        )
        session.add(op)
        session.flush()
        sync_run = MetaObservationSyncRunRecord(
            id=new_id("msync"), workspace_id=ctx.workspace_id, meta_mode=meta_mode,
            contract_generation_id=contract_generation_id,
            occurrence_id=new_id("occ"), run_generation=1,
            state="succeeded", operation_id=op.id, executor="test", completed_at=datetime.now(timezone.utc),
        )
        session.add(sync_run)
        session.flush()
        pub = publish_evidence(session, sync_run_id=sync_run.id, operation_id=op.id)
        create_attestation(
            session, contract_generation_id=contract_generation_id,
            publication_id=pub.id, evidence_watermark="[2026-09-01,2026-09-07]",
        )

        contract_set = ScaleMetricSemanticContractSetRecord(
            id=new_id("smcs"), workspace_id=ctx.workspace_id, meta_mode=meta_mode, created_by="user_1",
        )
        session.add(contract_set)
        session.flush()
        session.add(ScaleMetricSemanticContractSetMemberRecord(
            id=new_id("smcm"), contract_set_id=contract_set.id,
            contract_type="delivery_configuration",
            observation_contract_generation=contract_generation_id,
            contract_hash=f"delivery-hash-{tag}",
        ))
        session.flush()
