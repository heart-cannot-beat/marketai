"""Tests for Enrollment Candidates: the two-tier server-derived lineage gate
(spec §4.4, issue #86 Task 9).

Follows this repo's established ``unittest.TestCase`` fixture style
(``tests/test_winner_eligibility.py``, ``tests/test_scale_delivery_targets.py``)
rather than the pytest-fixture skeleton sketched in the task-9 brief -- this
codebase's test suite is consistently ``unittest.TestCase``-based and
duplicates fixture builders per test file rather than sharing them across
modules (``tests/test_winner_eligibility.py``'s own identical note), so this
file follows suit. A "confirmed winner" here must be a *fully* eligible
Winner (light identity chain plus a ready MarketAssetBinding/TruthPackage)
because ``create_enrollment_candidate``/``create_draft_enrollment`` always
run the complete authoritative lineage gate
(``app.scale.enrollment_lineage.lock_and_validate_enrollment_lineage``) --
there is no way to reach the DeliveryTarget-specific checks (C10 below)
without first passing full Winner eligibility.

Deviation from the task-9 brief's sample test file: C2's finding code is
``inconclusive_decision`` (the real code
``app.experiments.winner_eligibility.evaluate_winner_identity`` raises for
an inconclusive verdict, per Task 5's shipped implementation), not the
brief sketch's placeholder ``winner_not_confirmed``.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    TruthPackageRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.admission import PaidOperationAdmission
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401 -- FK target
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
from app.experiments.winner_eligibility import WinnerIneligibleError, _versioned_hash
from app.generation.models import GenerationDraftRecord  # noqa: F401 -- FK target
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
from app.meta.models import MetaConnectionRecord, ScenarioVersionRecord
from app.meta.observation import create_contract_generation
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext
from app.scale.candidates import (
    CandidateAlreadyConsumedError,
    CandidateNotFoundError,
    consume_candidate,
    create_enrollment_candidate,
)
from app.scale.delivery_targets import DeliveryTargetRequest, create_delivery_target
from app.scale.enrollment import CandidateFingerprintDriftError, create_draft_enrollment
from app.scale.enrollment_lineage import DeliveryTargetNotFoundError
from tests.experiment_governance_fixtures import govern_asset_versions, govern_product_offer_versions
from tests.support.market_codes import next_market_code as _next_market

CTX = TenantContext(
    workspace_id="ws_secand",
    role="owner",
    initiator_id="user_secand",
    request_id="req_secand",
    trace_id="trace_secand",
)

CTX_SANDBOX = TenantContext(
    workspace_id="ws_secand",
    role="owner",
    initiator_id="user_secand",
    meta_mode="customer_sandbox",
)

PROD_ID = "prod_secand"
OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
FAR_FUTURE = OBSERVED_AT + timedelta(days=365)

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}

# ---------------------------------------------------------------------------
# Light Winner chain (mirrors tests/test_winner_eligibility.py's identical
# helpers, duplicated per this repo's fixture convention)
# ---------------------------------------------------------------------------


def _draft_context(pv_id: str, ov_id: str, market: str, **overrides: object) -> dict:
    base = {
        "market": market,
        "currency": "USD",
        "placement": "feed",
        "product_version_id": pv_id,
        "offer_version_id": ov_id,
        "budget": {"daily_amount": "100", "total_amount": "3000"},
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
    }
    base.update(overrides)
    return base


def _ensure_workspace(session, ws_id: str) -> None:
    if session.get(WorkspaceRecord, ws_id) is None:
        session.add(WorkspaceRecord(id=ws_id, name=f"Candidate test {ws_id}"))
        session.flush()


def _seed_library_versions(session, tag: str, market: str) -> tuple[str, str]:
    pv_entry, ov_entry = f"le_pv_{tag}", f"le_ov_{tag}"
    pv_id, ov_id = f"pv_{tag}", f"ov_{tag}"
    for entry_id, ver_id, etype in [(pv_entry, pv_id, "product"), (ov_entry, ov_id, "offer")]:
        session.add(LibraryEntryRecord(
            id=entry_id, workspace_id=CTX.workspace_id,
            entry_type=etype, display_name=f"Test {etype} {tag}",
        ))
        session.flush()
        session.add(LibraryVersionRecord(
            id=ver_id, workspace_id=CTX.workspace_id,
            entry_id=entry_id, entry_type=etype,
            version_no=1, content_json={}, content_hash="c" * 64,
            state="effective", published_by="system",
        ))
        session.flush()
    return pv_id, ov_id


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session, CTX.workspace_id)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P Candidate"))
        session.flush()

    plan_id = f"plan_{tag}"
    if session.get(PlanRecord, plan_id) is None:
        session.add(PlanRecord(
            id=plan_id, workspace_id=CTX.workspace_id,
            product_id=PROD_ID, mode="full", cap=0, total_full=1,
            snapshot_hash="a" * 64,
        ))
        session.flush()

    run_id = f"run_{tag}"
    if session.get(GenerationRunRecord, run_id) is None:
        session.add(GenerationRunRecord(
            id=run_id, workspace_id=CTX.workspace_id,
            product_id=PROD_ID, plan_id=plan_id,
            provider="mock", model="mock", aspect_ratio="1:1",
            image_size="1024", variants=1, use_ref=False,
            status="done", total=1, done=1, failed=0,
            est_cost=Decimal("0"), currency="USD",
        ))
        session.flush()

    av_ids = []
    for i in range(count):
        asset_id = f"asset_{tag}_{i}"
        av_id = f"av_{tag}_{i}"
        if session.get(AssetRecord, asset_id) is None:
            session.add(AssetRecord(
                id=asset_id, workspace_id=CTX.workspace_id,
                product_id=PROD_ID, run_id=run_id,
                combo_id="C01", combo_code="A1", tags_json={},
                filename=f"{tag}_{i}.png", provider="mock", model="mock",
                prompt="test", fingerprint=f"fp_{tag}_{i}", status="ok",
            ))
            session.flush()
        if session.get(AssetVersionRecord, av_id) is None:
            session.add(AssetVersionRecord(
                id=av_id, workspace_id=CTX.workspace_id,
                asset_id=asset_id, kind="generated", aspect_ratio="1:1",
                language="en", object_key=f"{tag}_{i}.png",
                content_sha256=f"{i:0>64x}", byte_size=1, mime_type="image/png",
            ))
            session.flush()
        av_ids.append(av_id)
    govern_asset_versions(
        session, CTX, product_id=PROD_ID, asset_version_ids=av_ids, tag=tag,
    )
    return av_ids


def _seed_metrics(session, tag: str, ad_ids: list[str]) -> None:
    from app.adapters.persistence.models import AdDailyMetricRecord, MetaAccountRecord, MetaSyncRunRecord

    acct_id = f"macct_{tag}"
    if session.get(MetaAccountRecord, acct_id) is None:
        session.add(MetaAccountRecord(
            id=acct_id, workspace_id=CTX.workspace_id,
            ad_account_id=f"act_{tag}", currency="USD",
            timezone_name="UTC", api_version="v23.0",
        ))
        session.flush()

    sync_id = f"sync_{tag}"
    if session.get(MetaSyncRunRecord, sync_id) is None:
        session.add(MetaSyncRunRecord(
            id=sync_id, workspace_id=CTX.workspace_id,
            account_id=acct_id, state="completed",
            date_start=date(2026, 7, 1), date_stop=date(2026, 8, 1),
            contract_hash="b" * 64, contract_json={},
            api_version="v23.0", account_currency="USD",
            account_timezone="UTC", idempotency_key=f"sync_{tag}",
        ))
        session.flush()

    for idx, ad_id in enumerate(ad_ids):
        for day_offset in range(7):
            d = date(2026, 7, 25) + timedelta(days=day_offset)
            mid = f"met_{tag}_{idx}_{day_offset}"
            if session.get(AdDailyMetricRecord, mid) is None:
                imp = 1000 * (idx + 1)
                clk = 50 * (idx + 1) + (10 if idx == 0 else 0)
                session.add(AdDailyMetricRecord(
                    id=mid, account_id=acct_id, sync_run_id=sync_id,
                    ad_id=ad_id, metric_date=d,
                    spend=Decimal("10.00"), impressions=imp, clicks=clk,
                    purchases=Decimal("2"), purchase_value=Decimal("40"),
                    currency="USD", contract_hash="b" * 64, raw_json={},
                ))
        session.flush()


def _build_confirmed_winner(tag: str, *, market: str | None = None) -> dict:
    market = market or _next_market()
    with tenant_session(CTX) as s:
        pv_id, ov_id = _seed_library_versions(s, tag, market)
        avs = _seed_asset_versions(s, tag)
        govern_product_offer_versions(
            s, CTX, product_id=PROD_ID,
            product_version_id=pv_id, offer_version_id=ov_id,
            markets=(market,),
        )

    exp = create_experiment_v2(
        CTX, product_id=PROD_ID, name=f"CAND-{tag}",
        draft_context=_draft_context(pv_id, ov_id, market),
    )
    add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
    add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
    frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
    rev = frozen["revision"]
    ad_ids = []
    for cand in frozen["candidates"]:
        ad_id = f"ad_{tag}_{cand['position']}"
        ad_ids.append(ad_id)
        bind_candidate_v2(
            CTX, exp["id"], cand["id"],
            ad_account_id=f"act_{tag}_{cand['position']}",
            ad_id=ad_id, expected_revision=rev,
        )
    running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
    with tenant_session(CTX) as s:
        _seed_metrics(s, tag, ad_ids)
    j_result = run_judgement_v2(
        CTX, running["id"], expected_revision=running["revision"],
        as_of=date(2026, 8, 1),
    )
    conf = confirm_proposal_v2(
        CTX, running["id"], j_result["proposal_id"],
        expected_proposal_sequence=1,
    )
    real_decision_id = conf["decisions"][0]["id"]

    with tenant_session(CTX) as s:
        real_decision = s.get(ExperimentDecisionRecord, real_decision_id)
        candidate = s.get(
            ExperimentVersionCandidateRecord,
            real_decision.experiment_version_candidate_id,
        )
        experiment_version = s.get(ExperimentVersionRecord, real_decision.experiment_version_id)
        return {
            "tag": tag,
            "market": market,
            "decision_id": real_decision.id,
            "experiment_id": real_decision.experiment_id,
            "experiment_version_id": real_decision.experiment_version_id,
            "experiment_entry_id": real_decision.experiment_entry_id,
            "candidate_id": real_decision.experiment_version_candidate_id,
            "asset_version_id": candidate.asset_version_id,
            "proposal_id": real_decision.proposal_id,
            "confirmation_id": real_decision.confirmation_id,
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


def _make_decision(chain: dict, *, verdict: str, confirmation_id: str | None, suffix: str = "") -> str:
    """Append one purpose-built Decision reusing ``chain``'s real identity
    (PC-03 technique, mirrors ``tests/test_winner_eligibility.py``)."""
    with tenant_session(CTX) as s:
        judgement_id = new_id("judge")
        s.add(JudgementRecord(
            id=judgement_id, workspace_id=CTX.workspace_id,
            experiment_id=chain["experiment_id"],
            experiment_version_id=chain["experiment_version_id"],
            algorithm_version=f"test_v0{suffix}", as_of=date(2026, 8, 1),
            snapshot_hash=new_id("snap"), input_snapshot_json={}, result_json={},
            record_schema_version=2,
        ))
        s.flush()
        decision_id = new_id("decision")
        s.add(ExperimentDecisionRecord(
            id=decision_id, workspace_id=CTX.workspace_id,
            experiment_id=chain["experiment_id"], judgement_id=judgement_id,
            experiment_entry_id=chain["experiment_entry_id"],
            experiment_version_id=chain["experiment_version_id"],
            experiment_version_candidate_id=chain["candidate_id"],
            proposal_id=chain["proposal_id"], confirmation_id=confirmation_id,
            verdict=verdict, metric="ctr",
            actor_id=f"user_{chain['tag']}", decision_schema_version=2,
        ))
        s.flush()
    return decision_id


def _seed_review(asset_version_id: str, review_value: str) -> None:
    with tenant_session(CTX) as s:
        av = s.get(AssetVersionRecord, asset_version_id)
        s.add(QualityReviewRecord(
            id=new_id("qr"), workspace_id=CTX.workspace_id,
            product_id=PROD_ID, asset_id=av.asset_id,
            asset_version_id=asset_version_id, review_value=review_value,
            source="human", actor_id="reviewer_1",
        ))
        s.flush()


# ---------------------------------------------------------------------------
# Ready MarketAssetBinding (checks 8-10), mirrors
# tests/test_winner_eligibility.py's _build_ready_lineage_chain
# ---------------------------------------------------------------------------


def _make_market_policy_version(session, market_id: str) -> MarketPolicyVersionRecord:
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


def _make_version(session, entry_type: str, tag: str) -> str:
    entry_id = new_id("le")
    session.add(LibraryEntryRecord(
        id=entry_id, workspace_id=CTX.workspace_id, entry_type=entry_type,
        display_name=f"{entry_type}-{tag}",
    ))
    session.flush()
    version_id = new_id("lv")
    session.add(LibraryVersionRecord(
        id=version_id, workspace_id=CTX.workspace_id, entry_id=entry_id, entry_type=entry_type,
        version_no=1, content_json={}, content_hash=f"hash-{version_id}",
        published_by="tester",
    ))
    session.flush()
    return version_id


def _truth_command_for(tag: str, pv_id: str, ov_id: str, mpv: MarketPolicyVersionRecord, market: str) -> TruthResolutionCommand:
    with tenant_session(CTX) as s:
        policy_version_id = _make_version(s, "truth_resolution_policy", tag)
        data_source_version_id = _make_version(s, "data_source", tag)
        manual_fact_set_version_id = _make_version(s, "manual_fact_set", tag)

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


# ---------------------------------------------------------------------------
# customer_sandbox DeliveryTarget fixture builder
# ---------------------------------------------------------------------------


def _make_curated_scenario_context(session, tag: str, meta_mode: str = "customer_sandbox") -> dict:
    conn = MetaConnectionRecord(
        id=new_id("mcon"), workspace_id=CTX.workspace_id,
        meta_mode=meta_mode, ad_account_id=f"act_sdt_{tag}",
    )
    session.add(conn)
    session.flush()
    gen = create_contract_generation(
        session, workspace_id=CTX.workspace_id, meta_mode=meta_mode,
        connection_id=conn.id, contract_type="delivery_configuration",
        ad_account_id=f"act_sdt_{tag}", fields=["status"], attribution_window={},
        breakdowns=[], time_window={}, local_timezone="UTC",
    )
    scenario = ScenarioVersionRecord(
        id=new_id("scv"), workspace_id=CTX.workspace_id, meta_mode=meta_mode,
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


def _make_matching_delivery_target(chain: dict, tag: str) -> str:
    """Build a ``ScaleDeliveryTargetRecord`` under ``customer_sandbox`` that
    is *field-for-field compatible* with ``chain``'s Winner lineage (see
    ``app.scale.enrollment_lineage._assert_winner_target_compatible``).

    ``landing_page_version_id`` needs special handling: the target column
    is FK-enforced against ``library_versions``, while the Winner's own
    ``landing_page_version_id`` is populated from
    ``ExperimentVersionRecord.landing_page_evidence_hash`` -- a hash, not a
    real Library entity id (no Landing Page Library entity exists yet, per
    ``app.experiments.winner_eligibility``'s own documented flag). A real
    ``LibraryVersionRecord`` is created here with its id forced to exactly
    that hash value, satisfying both the DeliveryTarget's FK and the
    lineage-gate's equality check simultaneously.
    """
    with tenant_session(CTX) as s:
        upstream = _make_curated_scenario_context(s, tag)

        landing_entry_id = new_id("le")
        s.add(LibraryEntryRecord(
            id=landing_entry_id, workspace_id=CTX.workspace_id,
            entry_type="landing_page", display_name=f"landing-{tag}",
        ))
        s.flush()
        s.add(LibraryVersionRecord(
            id=chain["landing_page_evidence_hash"], workspace_id=CTX.workspace_id,
            entry_id=landing_entry_id, entry_type="landing_page",
            version_no=1, content_json={}, content_hash="d" * 64,
            published_by="test-fixture",
        ))
        s.flush()

    request = DeliveryTargetRequest(
        meta_connection_id=upstream["meta_connection_id"],
        ad_account_id=upstream["ad_account_id"],
        scenario_scope_key="__no_scenario__",
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
        source_type="curated_scenario",
        source_manifest={"schema_version": 1, "source_type": "curated_scenario"},
        observed_at=OBSERVED_AT,
        expires_at=FAR_FUTURE,
        scenario_version_id=upstream["scenario_version_id"],
    )
    with tenant_session(CTX_SANDBOX) as s:
        target = create_delivery_target(s, CTX_SANDBOX, request)
        return target.id


# ---------------------------------------------------------------------------
# Test base
# ---------------------------------------------------------------------------


class CandidateTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, CTX.workspace_id)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P Candidate"))
                s.flush()
            if s.get(EntitlementProjectionRecord, CTX.workspace_id) is None:
                s.add(EntitlementProjectionRecord(
                    workspace_id=CTX.workspace_id, entitlement_version=1, lease_sequence=1,
                    projection_version=1, schema_version=2, semantic_hash="h" * 8,
                    valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                    period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                    resolved_entitlements_json={},
                ))
            if s.get(SpendGuardrailRecord, CTX.workspace_id) is None:
                s.add(SpendGuardrailRecord(
                    workspace_id=CTX.workspace_id, revision=1, budget_currency="USD",
                    per_operation_limit=Decimal("100.00"), rolling_24h_limit=Decimal("500.00"),
                    period_limit=Decimal("5000.00"), outstanding_exposure_limit=Decimal("1000.00"),
                    created_at=now, updated_at=now, updated_by="user_1",
                ))

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def setUp(self) -> None:
        self.admission = PaidOperationAdmission(
            policy=EntitlementPolicy(),
            quote_engine=CostQuoteEngine(price_registry=PRICE_REGISTRY, price_rule_version="test-v1"),
            reservation_book=ReservationBook(),
        )

    def _ready_chain(self) -> dict:
        """One shared, fully-ready Winner+DeliveryTarget pair, built once
        for the whole module and reused read-only across every test that
        just needs *a* valid lineage (repeat ``create_enrollment_candidate``
        calls against the same pair are designed to be safe -- they reuse
        the same ``ScaleLineageGuardRecord``, see that record's docstring).
        Tests that specifically exercise winner/target *invalidity* (C2,
        C10) build their own narrower fixture instead."""
        return _get_shared_ready_chain(self.admission)


def _build_ready_winner_and_target(tag: str, admission: PaidOperationAdmission) -> dict:
    """Full chain: confirmed Winner (light chain) + ready
    MarketAssetBinding/TruthPackage (checks 8-10) + a field-compatible
    customer_sandbox DeliveryTarget."""
    market = _next_market()
    chain = _build_confirmed_winner(tag, market=market)
    _seed_review(chain["asset_version_id"], "approved")

    with tenant_session(CTX) as s:
        mpv = _make_market_policy_version(s, market)
        mpv_id = mpv.id

    with tenant_session(CTX) as s:
        mpv = s.get(MarketPolicyVersionRecord, mpv_id)
        truth_cmd = _truth_command_for(
            tag, chain["product_version_id"], chain["offer_version_id"], mpv, market,
        )

    snapshot = {
        "market": market, "placement": "feed", "locale": "en-US",
        "country": market, "currency": "USD",
    }
    with tenant_session(CTX) as s:
        draft = create_draft(
            s, CTX, product_version_id=chain["product_version_id"],
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
    with tenant_session(CTX) as s:
        create_inventory_from_analysis(
            s, CTX, asset_version_id=chain["asset_version_id"], analysis_recipe_hash=recipe_hash,
            claims=[{
                "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello world",
                "confidence": 0.99, "risk": "low", "conflicting": False,
            }],
            provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
            analysis_execution_mode="mock", adapter_version="mock-v1",
            mock_scenario="default", mock_version="v1",
        )

    with tenant_session(CTX) as s:
        start_preparation(
            s, CTX, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
            analysis_provider="mock-vision", analysis_model="mock-vision-1",
            analysis_adapter_version="mock-v1", actor_id="user_1", admission=admission,
        )

    from app.adapters.persistence.models import LocalizationBatchRecord

    with tenant_session(CTX) as s:
        scd = submit_claim_decisions(
            s, CTX, batch_id=batch_id,
            expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
            asset_version_id=chain["asset_version_id"],
            target_market=market, analysis_recipe_hash=recipe_hash,
            decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
            copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
            translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
            actor_id="user_1", role="owner", admission=admission,
        )
        copy_version_id = scd.copy_version_id

    with tenant_session(CTX) as s:
        approve_copy(
            s, CTX, batch_id=batch_id,
            expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
            copy_version_id=copy_version_id,
            actor_id="user_1", role="owner",
        )

    with tenant_session(CTX) as s:
        expected_revision = s.get(LocalizationBatchRecord, batch_id).revision

    with tenant_session(CTX) as s:
        confirmed = confirm(
            s, CTX, batch_id=batch_id, expected_revision=expected_revision,
            edit_provider="mock-edit", edit_model="mock-edit-1",
            edit_adapter_version="mock-edit-v1", actor_id="user_1", role="owner",
            admission=admission,
        )
    item_id = confirmed.item_ids[0]

    with tenant_session(CTX) as s:
        result = publish_binding(
            s, CTX, item_id=item_id, batch_id=batch_id,
            publication_id=None, actor_id="user_1",
        )
        binding_id = result.binding_id

    now = datetime.now(timezone.utc)
    with tenant_session(CTX) as s:
        binding = s.get(MarketAssetBindingRecord, binding_id)
        binding.review_state = "approved"
        binding.ready_for_experiment = True
        s.add(binding)
        truth_package = s.get(TruthPackageRecord, binding.truth_package_id)
        truth_package.status = "full"
        truth_package.valid_until = now + timedelta(days=30)
        s.add(truth_package)
        s.flush()

    chain["binding_id"] = binding_id
    chain["delivery_target_id"] = _make_matching_delivery_target(chain, tag)
    return chain


_SHARED_READY_CHAIN: dict | None = None


def _get_shared_ready_chain(admission: PaidOperationAdmission) -> dict:
    """Build the one shared ready Winner+DeliveryTarget pair exactly once
    for the whole test module (module-level cache, not per-class/per-test)
    -- the heavy Localization Batch pipeline this needs is expensive, and
    every consumer here only reads it (see ``CandidateTestBase._ready_chain``'s
    docstring)."""
    global _SHARED_READY_CHAIN
    if _SHARED_READY_CHAIN is None:
        _SHARED_READY_CHAIN = _build_ready_winner_and_target("shared1", admission)
    return _SHARED_READY_CHAIN


# ---------------------------------------------------------------------------
# C1/C2/C10: create_enrollment_candidate()
# ---------------------------------------------------------------------------


class TestCreateEnrollmentCandidate(CandidateTestBase):
    def test_marketing_delivery_discovery_uses_only_current_compatible_targets(self) -> None:
        from app.scale.candidates import marketing_delivery_choices
        from app.scale.models import ScaleDeliveryTargetRecord
        chain = self._ready_chain()
        with tenant_session(CTX_SANDBOX) as session:
            choices, reason = marketing_delivery_choices(session, CTX_SANDBOX, chain["decision_id"])
            self.assertIsNone(reason)
            self.assertIn(chain["delivery_target_id"], [item["id"] for item in choices])
            target = session.get(ScaleDeliveryTargetRecord, chain["delivery_target_id"])
            placement = target.placement
            target.placement = "incompatible_fixture"
            session.flush()
            choices, reason = marketing_delivery_choices(session, CTX_SANDBOX, chain["decision_id"])
            self.assertEqual(choices, ())
            self.assertEqual(reason, "compatible_delivery_target_required")
            target.placement = placement
            session.flush()

    def test_create_candidate_happy_path(self) -> None:
        """C1: valid winner + valid target -> candidate created."""
        chain = self._ready_chain()
        with tenant_session(CTX_SANDBOX) as session:
            candidate = create_enrollment_candidate(
                session, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key="idem_c1",
            )
            self.assertEqual(candidate.workspace_id, CTX_SANDBOX.workspace_id)
            self.assertIsNone(candidate.consumed_at)
            self.assertTrue(candidate.id.startswith("secand_"))

    def test_inconclusive_decision_rejected(self) -> None:
        """C2: inconclusive decision -> rejected with inconclusive_decision
        finding (real Task 5 code; brief sketch used a placeholder)."""
        chain = _build_confirmed_winner("c2")
        decision_id = _make_decision(chain, verdict="inconclusive", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(WinnerIneligibleError) as exc_info:
                create_enrollment_candidate(
                    session, CTX_SANDBOX,
                    decision_id=decision_id,
                    delivery_target_id="does-not-matter",
                    idempotency_key="idem_c2",
                )
        self.assertIn("inconclusive_decision", [f.code for f in exc_info.exception.findings])

    def test_delivery_target_not_found(self) -> None:
        """C10: delivery target not found."""
        chain = self._ready_chain()
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(DeliveryTargetNotFoundError):
                create_enrollment_candidate(
                    session, CTX_SANDBOX,
                    decision_id=chain["decision_id"],
                    delivery_target_id="nonexistent",
                    idempotency_key="idem_c10",
                )


# ---------------------------------------------------------------------------
# consume_candidate() -- atomic CAS single-use claim
# ---------------------------------------------------------------------------


class TestConsumeCandidate(CandidateTestBase):
    def _new_candidate_id(self, idempotency_key: str) -> str:
        chain = self._ready_chain()
        with tenant_session(CTX_SANDBOX) as session:
            candidate = create_enrollment_candidate(
                session, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=idempotency_key,
            )
            return candidate.id

    def test_consume_candidate_success(self) -> None:
        candidate_id = self._new_candidate_id("idem_cc1")
        with tenant_session(CTX_SANDBOX) as session:
            consumed = consume_candidate(session, CTX_SANDBOX, candidate_id)
            self.assertIsNotNone(consumed.consumed_at)

    def test_consume_candidate_already_consumed(self) -> None:
        candidate_id = self._new_candidate_id("idem_cc2")
        with tenant_session(CTX_SANDBOX) as session:
            consume_candidate(session, CTX_SANDBOX, candidate_id)
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(CandidateAlreadyConsumedError):
                consume_candidate(session, CTX_SANDBOX, candidate_id)

    def test_consume_candidate_not_found(self) -> None:
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(CandidateNotFoundError):
                consume_candidate(session, CTX_SANDBOX, "nonexistent")


# ---------------------------------------------------------------------------
# create_draft_enrollment() -- candidate-gated, issue #86 Task 9
# ---------------------------------------------------------------------------


class TestCreateDraftEnrollmentFromCandidate(CandidateTestBase):
    def _new_candidate_id(self, idempotency_key: str) -> str:
        chain = self._ready_chain()
        with tenant_session(CTX_SANDBOX) as session:
            candidate = create_enrollment_candidate(
                session, CTX_SANDBOX,
                decision_id=chain["decision_id"],
                delivery_target_id=chain["delivery_target_id"],
                idempotency_key=idempotency_key,
            )
            return candidate.id

    def test_create_enrollment_from_candidate(self) -> None:
        """E1: valid candidate -> draft enrollment."""
        candidate_id = self._new_candidate_id("idem_e1_candidate")
        with tenant_session(CTX_SANDBOX) as session:
            enrollment = create_draft_enrollment(
                session, CTX_SANDBOX,
                enrollment_candidate_id=candidate_id,
                idempotency_key="idem_e1",
            )
            self.assertEqual(enrollment.lifecycle, "draft")
            self.assertEqual(enrollment.creator_id, CTX_SANDBOX.initiator_id)
            self.assertEqual(enrollment.enrollment_candidate_id, candidate_id)

    def test_enrollment_from_consumed_candidate(self) -> None:
        """E7: candidate already consumed."""
        candidate_id = self._new_candidate_id("idem_e7_candidate")
        with tenant_session(CTX_SANDBOX) as session:
            consume_candidate(session, CTX_SANDBOX, candidate_id)
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(CandidateAlreadyConsumedError):
                create_draft_enrollment(
                    session, CTX_SANDBOX,
                    enrollment_candidate_id=candidate_id,
                    idempotency_key="idem_e7",
                )

    def test_enrollment_creator_from_context(self) -> None:
        """E8: creator_id == context.initiator_id (never caller-supplied)."""
        candidate_id = self._new_candidate_id("idem_e8_candidate")
        with tenant_session(CTX_SANDBOX) as session:
            enrollment = create_draft_enrollment(
                session, CTX_SANDBOX,
                enrollment_candidate_id=candidate_id,
                idempotency_key="idem_e8",
            )
            self.assertEqual(enrollment.creator_id, CTX_SANDBOX.initiator_id)
            self.assertEqual(enrollment.original_initiator_id, CTX_SANDBOX.initiator_id)

    def test_expired_candidate_rejected(self) -> None:
        """Candidate past its TTL cannot produce an enrollment."""
        from app.scale.candidates import CandidateExpiredError
        from app.scale.models import ScaleEnrollmentCandidateRecord

        candidate_id = self._new_candidate_id("idem_exp_candidate")
        with tenant_session(CTX_SANDBOX) as session:
            row = session.get(ScaleEnrollmentCandidateRecord, candidate_id)
            row.expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
            session.flush()
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(CandidateExpiredError):
                create_draft_enrollment(
                    session, CTX_SANDBOX,
                    enrollment_candidate_id=candidate_id,
                    idempotency_key="idem_expired",
                )

    def test_fingerprint_drift_rejected(self) -> None:
        """Guard revision bump between candidate creation and enrollment
        creation triggers drift detection."""
        from app.scale.models import ScaleEnrollmentCandidateRecord

        candidate_id = self._new_candidate_id("idem_drift_candidate")
        with tenant_session(CTX_SANDBOX) as session:
            row = session.get(ScaleEnrollmentCandidateRecord, candidate_id)
            row.candidate_fingerprint = "drifted_fingerprint_value_xxxxxx"
            session.flush()
        with tenant_session(CTX_SANDBOX) as session:
            with self.assertRaises(CandidateFingerprintDriftError):
                create_draft_enrollment(
                    session, CTX_SANDBOX,
                    enrollment_candidate_id=candidate_id,
                    idempotency_key="idem_drift",
                )


# ---------------------------------------------------------------------------
# HTTP-level: candidate-only request body (schema `extra="forbid"`)
# ---------------------------------------------------------------------------


class TestEnrollmentCreateRouteValidation(unittest.TestCase):
    """E2/E3: any raw Winner/DeliveryTarget id or caller-supplied
    ``creator_id`` in the create-enrollment request body is a 422 --
    Pydantic's ``extra="forbid"`` rejects it before any domain call, so no
    valid Candidate is needed for either case."""

    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app.adapters.persistence.database import platform_session
        from app.main import app
        from app.platform.http_identity import request_tenant_context

        self.workspace_id = f"ws_secand_http_{new_id('t')}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=self.workspace_id, name=self.workspace_id))

        self.ctx = TenantContext(
            workspace_id=self.workspace_id, role="owner", initiator_id="user_1",
        )
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_creator_id_in_body_rejected(self) -> None:
        resp = self.client.post(
            "/api/v1/scale/customer_sandbox/enrollments",
            json={"enrollment_candidate_id": "secand_x", "creator_id": "someone"},
            headers={"Idempotency-Key": "idem_e2"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_raw_ids_in_body_rejected(self) -> None:
        resp = self.client.post(
            "/api/v1/scale/customer_sandbox/enrollments",
            json={"winner_decision_id": "dec_1", "meta_connection_id": "conn_1"},
            headers={"Idempotency-Key": "idem_e3"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)


if __name__ == "__main__":
    unittest.main()
