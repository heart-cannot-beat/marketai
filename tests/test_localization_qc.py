"""Deterministic QC module tests for Localization (#24, spec §2G/§2H, §3H,
§16.7).

Exercises `get_frozen_qc_plan()`, `run_deterministic_qc()` and
`derive_qc_eligibility()` through their public interfaces only, against a
real Item + Attempt 1 produced by driving the actual command sequence
(`create_draft` -> `create_inventory_from_analysis` -> `start_preparation`
-> `submit_claim_decisions` -> `approve_copy` -> `confirm`), the same
proven Winner-chain/entitlement fixture pattern
`tests/test_localization_batch.py` uses -- Item/Attempt rows carry deep
composite foreign keys through `localization_batch_sources` into the real
Experiments v2 tables, so hand-inserting them would just re-implement
that same chain unsafely. `LocalizationAttemptOutputRecord` has no
producing module yet (Worker dispatch is a later task), so it is
hand-inserted directly for the one Attempt each test needs QC'd -- it is
consumed, not exercised, by `qc.py`.

One Item is built per test (`_build_qc_item`) with one `preserve` headline
Claim and one `replace` price Claim (locked to a real `TruthFactRecord`),
under a target market whose `qc_rules_json` configures one blocking
forbidden term, one warning term and one commercial-claim keyword -- the
exact shape `qc.py`'s module docstring documents. Each test then supplies
a different `DeterministicQCInput` override to isolate one deterministic
check.
"""
from __future__ import annotations

import itertools
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchRecord,
    LocalizationQcRecordRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    ProductRecord,
    QualityReviewRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
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
from app.entitlements.usage_models import UsageEventRecord  # noqa: F401
from app.experiments.service import (
    add_entry_v2,
    bind_candidate_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.generation.models import GenerationDraftRecord  # noqa: F401
from app.localization.batch import (
    BatchMarketTargetInput,
    BatchSourceInput,
    approve_copy,
    confirm,
    create_draft,
    start_preparation,
    submit_claim_decisions,
)
from app.localization.claims import ClaimDecisionInput, create_inventory_from_analysis
from app.localization.evidence_tier import EvidenceTier
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.qc import (
    DeterministicQCInput,
    ObservedSegment,
    QCError,
    derive_qc_eligibility,
    get_frozen_qc_plan,
    run_deterministic_qc,
)
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
    resolve_or_reuse_truth_package,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

WS_ID = "ws_lq_qc"

CTX = TenantContext(workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner")

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
}

_MARKET_ID_COUNTER = itertools.count(1)

QC_RULES = {
    "forbidden_terms": ["guaranteed"],
    "warning_terms": ["amazing"],
    "commercial_claim_keywords": ["lifetime warranty"],
}


# ---------------------------------------------------------------------------
# Winner-chain fixtures (mirrors tests/test_localization_batch.py)
# ---------------------------------------------------------------------------


def _draft_context(pv_id: str, ov_id: str, **overrides: object) -> dict:
    base = {
        "market": "US", "currency": "USD", "placement": "feed",
        "product_version_id": pv_id, "offer_version_id": ov_id,
        "budget": {"daily_amount": "100", "total_amount": "3000"},
        "schedule": {
            "start_at": "2026-09-01T00:00:00+00:00", "end_at": "2026-12-01T00:00:00+00:00",
            "timezone": "UTC",
        },
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {
            "primary_metric": "ctr", "ctr_threshold": "0.01", "alpha": "0.05",
            "bootstrap_samples": 2000,
        },
    }
    base.update(overrides)
    return base


def _ensure_workspace(session, ws_id: str) -> None:
    if session.get(WorkspaceRecord, ws_id) is None:
        session.add(WorkspaceRecord(id=ws_id, name=f"QC test {ws_id}"))
        session.flush()


def _seed_library_versions(session, ws_id: str, tag: str) -> tuple[str, str]:
    pv_entry, ov_entry = f"le_pv_{tag}", f"le_ov_{tag}"
    pv_id, ov_id = f"pv_{tag}", f"ov_{tag}"
    for entry_id, ver_id, etype in [(pv_entry, pv_id, "product"), (ov_entry, ov_id, "offer")]:
        session.add(LibraryEntryRecord(
            id=entry_id, workspace_id=ws_id, entry_type=etype, display_name=f"Test {etype} {tag}",
        ))
        session.flush()
        session.add(LibraryVersionRecord(
            id=ver_id, workspace_id=ws_id, entry_id=entry_id, entry_type=etype,
            version_no=1,
            content_json=(
                {
                    "source_market_snapshot": {
                        "market": "US", "placement": "feed",
                        "locale": "en-US", "country": "US", "currency": "USD",
                    }
                }
                if etype == "offer" else {}
            ),
            content_hash="c" * 64,
            state="effective", published_by="system",
        ))
        session.flush()
    return pv_id, ov_id


def _make_version(session, workspace_id: str, entry_type: str, tag: str, *, content=None) -> str:
    entry_id = new_id("le")
    session.add(LibraryEntryRecord(
        id=entry_id, workspace_id=workspace_id, entry_type=entry_type,
        display_name=f"{entry_type}-{tag}", lifecycle="active",
    ))
    session.flush()
    version_id = new_id("lv")
    session.add(LibraryVersionRecord(
        id=version_id, workspace_id=workspace_id, entry_id=entry_id, entry_type=entry_type,
        version_no=1, content_json=content if content is not None else {},
        content_hash=f"hash-{version_id}", published_by="tester",
    ))
    session.flush()
    return version_id


def _product_id(ws_id: str) -> str:
    return f"prod_{ws_id}"


def _seed_asset_versions(session, ws_id: str, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session, ws_id)
    product_id = _product_id(ws_id)
    if session.get(ProductRecord, product_id) is None:
        session.add(ProductRecord(id=product_id, workspace_id=ws_id, name="P"))
        session.flush()

    plan_id = f"plan_{tag}"
    from app.adapters.persistence.models import PlanRecord

    if session.get(PlanRecord, plan_id) is None:
        session.add(PlanRecord(
            id=plan_id, workspace_id=ws_id, product_id=product_id,
            mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
        ))
        session.flush()

    run_id = f"run_{tag}"
    if session.get(GenerationRunRecord, run_id) is None:
        session.add(GenerationRunRecord(
            id=run_id, workspace_id=ws_id, product_id=product_id, plan_id=plan_id,
            provider="mock", model="mock", aspect_ratio="1:1", image_size="1024",
            variants=1, use_ref=False, status="done", total=1, done=1, failed=0,
            est_cost=Decimal("0"), currency="USD",
        ))
        session.flush()

    av_ids = []
    for i in range(count):
        asset_id, av_id = f"asset_{tag}_{i}", f"av_{tag}_{i}"
        if session.get(AssetRecord, asset_id) is None:
            session.add(AssetRecord(
                id=asset_id, workspace_id=ws_id, product_id=product_id, run_id=run_id,
                combo_id="C01", combo_code="A1", tags_json={}, filename=f"{tag}_{i}.png",
                provider="mock", model="mock", prompt="test", fingerprint=f"fp_{tag}_{i}", status="ok",
            ))
            session.flush()
        if session.get(AssetVersionRecord, av_id) is None:
            session.add(AssetVersionRecord(
                id=av_id, workspace_id=ws_id, asset_id=asset_id, kind="generated",
                aspect_ratio="1:1", language="en", object_key=f"{tag}_{i}.png",
                content_sha256=f"{tag}{i}".rjust(64, "0"), byte_size=1, mime_type="image/png",
            ))
            session.flush()
        av_ids.append(av_id)
    return av_ids


def _seed_metrics(session, ws_id: str, tag: str, ad_ids: list[str]) -> None:
    from app.adapters.persistence.models import AdDailyMetricRecord, MetaAccountRecord, MetaSyncRunRecord

    acct_id = f"macct_{tag}"
    if session.get(MetaAccountRecord, acct_id) is None:
        session.add(MetaAccountRecord(
            id=acct_id, workspace_id=ws_id, ad_account_id=f"act_{tag}", currency="USD",
            timezone_name="UTC", api_version="v23.0",
        ))
        session.flush()

    sync_id = f"sync_{tag}"
    if session.get(MetaSyncRunRecord, sync_id) is None:
        session.add(MetaSyncRunRecord(
            id=sync_id, workspace_id=ws_id, account_id=acct_id, state="completed",
            date_start=date(2026, 7, 1), date_stop=date(2026, 8, 1),
            contract_hash="b" * 64, contract_json={}, api_version="v23.0",
            account_currency="USD", account_timezone="UTC", idempotency_key=f"sync_{tag}",
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
                    id=mid, account_id=acct_id, sync_run_id=sync_id, ad_id=ad_id, metric_date=d,
                    spend=Decimal("10.00"), impressions=imp, clicks=clk,
                    purchases=Decimal("2"), purchase_value=Decimal("40"),
                    currency="USD", contract_hash="b" * 64, raw_json={},
                ))
        session.flush()


def _build_confirmed_winner(ws_id: str, ctx: TenantContext, tag: str) -> dict:
    """Drive a real experiment to a confirmed material 'win' Decision --
    mirrors `tests/test_localization_eligibility.py::_build_confirmed_winner`
    and `tests/test_localization_batch.py::_build_confirmed_winner`."""
    with tenant_session(ctx) as s:
        pv_id, ov_id = _seed_library_versions(s, ws_id, tag)
        avs = _seed_asset_versions(s, ws_id, tag)
        govern_product_offer_versions(
            s,
            ctx,
            product_id=_product_id(ws_id),
            product_version_id=pv_id,
            offer_version_id=ov_id,
        )
        govern_asset_versions(
            s,
            ctx,
            product_id=_product_id(ws_id),
            asset_version_ids=avs,
            tag=tag,
        )

    exp = create_experiment_v2(
        ctx, product_id=_product_id(ws_id), name=f"LQ-{tag}", draft_context=_draft_context(pv_id, ov_id),
    )
    add_entry_v2(ctx, exp["id"], asset_version_id=avs[0], expected_revision=1)
    add_entry_v2(ctx, exp["id"], asset_version_id=avs[1], expected_revision=2)
    frozen = freeze_experiment_v2(ctx, exp["id"], expected_revision=3)
    rev = frozen["revision"]
    ad_ids = []
    for cand in frozen["candidates"]:
        ad_id = f"ad_{tag}_{cand['position']}"
        ad_ids.append(ad_id)
        bind_candidate_v2(
            ctx, exp["id"], cand["id"], ad_account_id=f"act_{tag}_{cand['position']}",
            ad_id=ad_id, expected_revision=rev,
        )
    running = start_experiment_v2(ctx, exp["id"], expected_revision=rev)
    with tenant_session(ctx) as s:
        _seed_metrics(s, ws_id, tag, ad_ids)
    j_result = run_judgement_v2(
        ctx, running["id"], expected_revision=running["revision"], as_of=date(2026, 8, 1),
    )
    conf = confirm_proposal_v2(
        ctx, running["id"], j_result["proposal_id"], expected_proposal_sequence=1,
    )
    real_decision_id = conf["decisions"][0]["id"]

    with tenant_session(ctx) as s:
        decision = s.get(ExperimentDecisionRecord, real_decision_id)
        return {
            "tag": tag, "decision_id": decision.id, "asset_version_id": avs[0],
            "product_version_id": pv_id, "offer_version_id": ov_id,
        }


def _seed_review(ctx: TenantContext, asset_version_id: str) -> None:
    with tenant_session(ctx) as s:
        existing = s.scalar(
            select(QualityReviewRecord).where(
                QualityReviewRecord.workspace_id == ctx.workspace_id,
                QualityReviewRecord.asset_version_id == asset_version_id,
                QualityReviewRecord.review_value == "approved",
                QualityReviewRecord.legacy_unscoped.is_(False),
            )
        )
        if existing is not None:
            return
        av = s.get(AssetVersionRecord, asset_version_id)
        s.add(QualityReviewRecord(
            id=new_id("qr"), workspace_id=ctx.workspace_id, product_id=_product_id(ctx.workspace_id),
            asset_id=av.asset_id, asset_version_id=asset_version_id,
            review_value="approved", source="human", actor_id="reviewer_1",
        ))
        s.flush()


def _make_market_policy_version(
    session, market_id: str, *, qc_rules_json: dict | None = None,
) -> MarketPolicyVersionRecord:
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
        max_fact_staleness_seconds=86400, qc_rules_json=qc_rules_json or {},
    )
    session.add(version)
    session.flush()
    return version


# ---------------------------------------------------------------------------
# TestCase
# ---------------------------------------------------------------------------


class TestDeterministicQC(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, WS_ID)
            s.add(EntitlementProjectionRecord(
                workspace_id=WS_ID, entitlement_version=1, lease_sequence=1,
                projection_version=1, schema_version=2, semantic_hash="h" * 8,
                valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                resolved_entitlements_json={},
            ))
            s.add(SpendGuardrailRecord(
                workspace_id=WS_ID, revision=1, budget_currency="USD",
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

    # -- fixture builder -------------------------------------------------

    def _build_qc_item(self, tag: str) -> dict:
        """Build one real Item + Attempt 1 (state ``authorized``) through
        the actual command pipeline, plus one hand-inserted
        `LocalizationAttemptOutputRecord` (no producing module exists yet
        -- Worker dispatch is a later task). One `preserve` headline
        Claim (claim_index 0) and one `replace` price Claim (claim_index
        1, locked to a real ``price`` Fact) under a target market
        configured with `QC_RULES`."""
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])

        # "QC" prefix (rather than "US") keeps this file's market_id
        # sequence from colliding with tests/test_localization_batch.py's
        # own counter -- `market_policies.market_id` is globally unique,
        # not Workspace-scoped, and both files share the persistent
        # suite-wide database (tests/conftest.py).
        market_id = f"QC{next(_MARKET_ID_COUNTER):04d}"[:10]
        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, market_id, qc_rules_json=QC_RULES)
            mpv_id = mpv.id

        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv_id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id
            locale, country, currency = row.locale, row.country, row.currency

        policy_version_id = None
        data_source_version_id = None
        manual_fact_set_version_id = None
        with tenant_session(CTX) as s:
            policy_version_id = _make_version(s, WS_ID, "truth_resolution_policy", tag)
            data_source_version_id = _make_version(s, WS_ID, "data_source", tag)
            manual_fact_set_version_id = _make_version(s, WS_ID, "manual_fact_set", tag)

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
            market=market, locale=locale, country=country, currency=currency,
            market_policy_version_id=mpv_id, sources=(step,),
            fact_policies=(FactPolicy("price"), FactPolicy("availability")),
        )
        truth_cmd = TruthResolutionCommand(
            product_version_id=winner["product_version_id"], offer_version_id=winner["offer_version_id"],
            truth_resolution_policy_version_id=policy_version_id, truth_data_mode="real",
            max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
            markets=(market_cmd,), now=now,
        )

        with tenant_session(CTX) as s:
            draft = create_draft(
                s, CTX, product_version_id=winner["product_version_id"],
                offer_version_id=winner["offer_version_id"], source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                truth_data_mode="real", provider_execution_mode="mock",
                sources=[BatchSourceInput(
                    asset_version_id=winner["asset_version_id"], decision_id=winner["decision_id"],
                    source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                )],
                market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv_id, market=market)],
                actor_id="user_1",
            )
            batch_id = draft.batch_id

        # Resolve Truth directly first (before the Inventory exists) so the
        # `replace` Claim below can be built with the *exact* scope_key the
        # resolved price Fact carries -- `claims.py` requires
        # `claim_scope_key == replacement_fact.scope_key` (spec §16.7).
        # `start_preparation()` below re-resolves the identical command and
        # deterministically reuses this same TruthPackage (spec §16.9).
        with tenant_session(CTX) as s:
            truth_result = resolve_or_reuse_truth_package(s, CTX, truth_cmd)
            truth_package_id = truth_result.truth_package_id
            tpm = s.scalar(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.workspace_id == WS_ID,
                    TruthPackageMarketRecord.truth_package_id == truth_package_id,
                    TruthPackageMarketRecord.market == market,
                )
            )
            price_fact = s.scalar(
                select(TruthFactRecord).where(
                    TruthFactRecord.workspace_id == WS_ID,
                    TruthFactRecord.truth_package_id == truth_package_id,
                    TruthFactRecord.truth_package_market_id == tpm.id,
                    TruthFactRecord.fact_type == "price",
                )
            )
            price_fact_id = price_fact.id
            price_fact_scope_key = price_fact.scope_key
            price_fact_value = dict(price_fact.value_json)

        recipe_hash = f"recipe-{tag}"
        with tenant_session(CTX) as s:
            create_inventory_from_analysis(
                s, CTX, asset_version_id=winner["asset_version_id"], analysis_recipe_hash=recipe_hash,
                claims=[
                    {
                        "claim_type": "headline", "scope_key": f"headline_{tag}",
                        "ocr_text": "Great product for everyone",
                        "confidence": 0.99, "risk": "low", "conflicting": False,
                    },
                    {
                        "claim_type": "price_claim", "scope_key": price_fact_scope_key, "ocr_text": "$19.99",
                        "confidence": 0.99, "risk": "low", "conflicting": False,
                    },
                ],
                provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                analysis_execution_mode="mock", adapter_version="mock-v1",
                mock_scenario="default", mock_version="v1",
            )

        with tenant_session(CTX) as s:
            start_preparation(
                s, CTX, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
                analysis_provider="mock-vision", analysis_model="mock-vision-1",
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
            )

        with tenant_session(CTX) as s:
            scd = submit_claim_decisions(
                s, CTX, batch_id=batch_id,
                expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                asset_version_id=winner["asset_version_id"],
                target_market=market, analysis_recipe_hash=recipe_hash,
                decisions=[
                    ClaimDecisionInput(claim_index=0, action="preserve"),
                    ClaimDecisionInput(claim_index=1, action="replace", replacement_fact_id=price_fact_id),
                ],
                copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
                actor_id="user_1", role="owner", admission=self.admission,
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
            revision = s.get(LocalizationBatchRecord, batch_id).revision

        with tenant_session(CTX) as s:
            confirm_result = confirm(
                s, CTX, batch_id=batch_id, expected_revision=revision,
                edit_provider="mock-edit", edit_model="mock-edit-1",
                edit_adapter_version="mock-edit-v1", actor_id="user_1", role="owner",
                admission=self.admission,
            )
        item_id = confirm_result.item_ids[0]

        with tenant_session(CTX) as s:
            attempt = s.scalar(
                select(LocalizationAttemptRecord).where(
                    LocalizationAttemptRecord.workspace_id == WS_ID,
                    LocalizationAttemptRecord.item_id == item_id,
                    LocalizationAttemptRecord.attempt_number == 1,
                )
            )
            attempt_id = attempt.id
            # No producing module exists yet for LocalizationAttemptOutputRecord
            # (Worker dispatch is a later task) -- hand-inserted directly, the
            # same way this file hand-inserts every other upstream row not
            # owned by the module under test.
            s.add(LocalizationAttemptOutputRecord(
                id=new_id("lao"), workspace_id=WS_ID, attempt_id=attempt_id,
                item_id=item_id, batch_id=batch_id,
                object_key=f"localized/{attempt_id}.png", content_sha256="a" * 64,
                byte_size=100, mime_type="image/png", output_evidence_hash="b" * 64,
            ))
            s.flush()

        return {
            "batch_id": batch_id, "item_id": item_id, "attempt_id": attempt_id,
            "price_fact_id": price_fact_id, "price_fact_value": price_fact_value,
            "market_policy_version_id": mpv_id,
        }

    def _run_qc(self, item: dict, observed: DeterministicQCInput | None = None):
        with tenant_session(CTX) as s:
            return run_deterministic_qc(
                s, CTX, attempt_id=item["attempt_id"], item_id=item["item_id"],
                batch_id=item["batch_id"], observed=observed,
            )

    def _records_by_slot(self, report) -> dict:
        return {r.check_slot_key: r for r in report.records}

    # -- tests ------------------------------------------------------------

    def test_locked_fact_mismatch_is_blocking(self) -> None:
        item = self._build_qc_item("lockedfact01")
        observed = DeterministicQCInput(
            observed_segments={
                1: ObservedSegment(
                    kind="locked_replace", value_json={"amount": "9.99", "currency": "USD"}
                ),
            }
        )
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        record = records["locked_fact:1"]
        self.assertEqual(record.check_kind, "locked_fact")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "fail")
        self.assertEqual(record.expected_json["value_json"], item["price_fact_value"])
        self.assertEqual(record.observed_json["value_json"], {"amount": "9.99", "currency": "USD"})

        with tenant_session(CTX) as s:
            eligibility = derive_qc_eligibility(
                s, CTX, attempt_id=item["attempt_id"], item_id=item["item_id"], batch_id=item["batch_id"],
            )
        self.assertIn("locked_fact:1", eligibility.blocking_failure_slot_keys)
        self.assertFalse(eligibility.is_eligible)

    def test_number_match_wrong_format(self) -> None:
        item = self._build_qc_item("numbermatch01")
        observed = DeterministicQCInput(
            observed_segments={
                1: ObservedSegment(
                    kind="locked_replace", value_json={"amount": "19,99", "currency": "USD"}
                ),
            }
        )
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        record = records["number_match:1"]
        self.assertEqual(record.check_kind, "number_match")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "fail")
        self.assertEqual(record.expected_json["amount"], "19.99")
        self.assertEqual(record.observed_json["amount"], "19,99")

    def test_currency_match_wrong_symbol(self) -> None:
        item = self._build_qc_item("currencymatch01")
        observed = DeterministicQCInput(
            observed_segments={
                1: ObservedSegment(
                    kind="locked_replace", value_json={"amount": "19.99", "currency": "EUR"}
                ),
            }
        )
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        record = records["currency_match:1"]
        self.assertEqual(record.check_kind, "currency_match")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "fail")
        self.assertEqual(record.expected_json["currency"], "USD")
        self.assertEqual(record.observed_json["currency"], "EUR")

    def test_claim_decision_replace_verified(self) -> None:
        item = self._build_qc_item("claimdecision01")
        report = self._run_qc(item)  # happy path: no override
        records = self._records_by_slot(report)

        record = records["claim_decision:1"]
        self.assertEqual(record.check_kind, "claim_decision")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "pass")
        self.assertEqual(record.expected_json["action"], "replace")
        self.assertEqual(record.expected_json["expected_kind"], "locked_replace")
        self.assertEqual(record.observed_json["observed_kind"], "locked_replace")

        # And the `preserve` headline Claim's own claim_decision slot
        # also passes in the happy path.
        preserve_record = records["claim_decision:0"]
        self.assertEqual(preserve_record.result, "pass")

    def test_placeholder_count_mismatch(self) -> None:
        item = self._build_qc_item("placeholdercount01")
        # Simulate the locked placeholder silently rendering as plain text
        # -- the placeholder is lost.
        observed = DeterministicQCInput(
            observed_segments={1: ObservedSegment(kind="text")}
        )
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        record = records["placeholder_count"]
        self.assertEqual(record.check_kind, "placeholder_count")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "fail")
        self.assertEqual(record.expected_json["count"], 1)
        self.assertEqual(record.observed_json["count"], 0)

    def test_new_commercial_claim_is_blocking(self) -> None:
        item = self._build_qc_item("newclaim01")
        observed = DeterministicQCInput(
            full_text_override="Buy now with a lifetime warranty included"
        )
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        record = records["new_commercial_claim:lifetime warranty"]
        self.assertEqual(record.check_kind, "new_commercial_claim")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "fail")
        self.assertTrue(record.observed_json["found"])

        with tenant_session(CTX) as s:
            eligibility = derive_qc_eligibility(
                s, CTX, attempt_id=item["attempt_id"], item_id=item["item_id"], batch_id=item["batch_id"],
            )
        self.assertIn("new_commercial_claim:lifetime warranty", eligibility.blocking_failure_slot_keys)
        self.assertFalse(eligibility.is_eligible)

    def test_forbidden_term_detected(self) -> None:
        item = self._build_qc_item("forbidden01")
        observed = DeterministicQCInput(full_text_override="Results are guaranteed for everyone")
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        record = records["forbidden_term:blocking:guaranteed"]
        self.assertEqual(record.check_kind, "forbidden_term")
        self.assertEqual(record.severity, "blocking")
        self.assertEqual(record.result, "fail")
        self.assertTrue(record.observed_json["found"])

    def test_warning_severity_does_not_block(self) -> None:
        item = self._build_qc_item("warning01")
        # Only the configured warning term appears -- no forbidden term,
        # no new commercial claim, and every other slot uses the
        # unmodified happy-path evidence.
        observed = DeterministicQCInput(full_text_override="This deal is amazing")
        report = self._run_qc(item, observed)
        records = self._records_by_slot(report)

        warning_record = records["forbidden_term:warning:amazing"]
        self.assertEqual(warning_record.severity, "warning")
        self.assertEqual(warning_record.result, "fail")

        with tenant_session(CTX) as s:
            eligibility = derive_qc_eligibility(
                s, CTX, attempt_id=item["attempt_id"], item_id=item["item_id"], batch_id=item["batch_id"],
            )
        self.assertIn("forbidden_term:warning:amazing", eligibility.warning_failure_slot_keys)
        # A failed warning-severity finding never blocks eligibility.
        self.assertEqual(eligibility.blocking_failure_slot_keys, ())
        self.assertTrue(eligibility.is_eligible)

    def test_idempotent_fold_per_check_slot(self) -> None:
        item = self._build_qc_item("idempotent01")

        first = self._run_qc(item)
        first_ids = {slot: r.id for slot, r in self._records_by_slot(first).items()}

        with tenant_session(CTX) as s:
            count_after_first = len(
                s.scalars(
                    select(LocalizationQcRecordRecord).where(
                        LocalizationQcRecordRecord.attempt_id == item["attempt_id"]
                    )
                ).all()
            )

        second = self._run_qc(item)
        second_ids = {slot: r.id for slot, r in self._records_by_slot(second).items()}

        # Re-running for the same Attempt with the same evidence appends
        # nothing new: every slot's record is the exact same row.
        self.assertEqual(first_ids, second_ids)

        with tenant_session(CTX) as s:
            count_after_second = len(
                s.scalars(
                    select(LocalizationQcRecordRecord).where(
                        LocalizationQcRecordRecord.attempt_id == item["attempt_id"]
                    )
                ).all()
            )
        self.assertEqual(count_after_first, count_after_second)

    def test_no_attempt_output_raises(self) -> None:
        """`run_deterministic_qc()` refuses to run before an isolated
        output exists for the Attempt (spec §2G: deterministic QC only
        follows a persisted successful edit response)."""
        with self.assertRaises(QCError):
            with tenant_session(CTX) as s:
                run_deterministic_qc(
                    s, CTX, attempt_id="missing_attempt_id_xyz",
                    item_id="missing_item_id_xyz", batch_id="missing_batch_id_xyz",
                )

    def test_frozen_plan_matches_recorded_slots(self) -> None:
        item = self._build_qc_item("frozenplan01")
        with tenant_session(CTX) as s:
            plan = get_frozen_qc_plan(s, CTX, item_id=item["item_id"], batch_id=item["batch_id"])
        expected_kinds = {
            "claim_decision", "locked_fact", "number_match", "currency_match",
            "placeholder_count", "forbidden_term", "new_commercial_claim",
        }
        self.assertEqual({slot.check_kind for slot in plan.slots}, expected_kinds)

        report = self._run_qc(item)
        self.assertEqual(set(plan.slot_keys), set(self._records_by_slot(report).keys()))
