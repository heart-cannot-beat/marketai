"""Batch module tests for Localization (#24, spec §2E/§2F/§2G, §3F,
§16.2/§16.6/§16.8/§16.9).

Exercises `create_draft()`, `start_preparation()`, `submit_claim_decisions()`,
`approve_copy()`, `confirm()`, `retry_item()` and `derive_batch_outcome()`
through their public interfaces only, against the shared suite-wide database
(`app.adapters.persistence.database.engine`, migrated once in
`setUpClass`), the same way `tests/test_localization_eligibility.py` does.
Each command call runs inside its own `tenant_session()` block -- exactly
how production code would call it -- so a failed command's automatic
rollback (spec §3F: "entitlement, capacity or guardrail failure -> no
frozen Batch, ... commits") is `tenant_session`'s own commit-on-success/
rollback-on-exception contract, not anything batch.py has to implement
itself.

A confirmed Winner source is built through the real Experiments v2 flow
(`create_experiment_v2` .. `confirm_proposal_v2`), the same pattern
`tests/test_localization_eligibility.py` uses, so the
Decision/confirmation/candidate/AssetVersion FK chain
`app.localization.eligibility.validate_source_for_batch` reads is exactly
what production code creates. Truth/Claims/Copy provenance is produced by
driving the real `app.localization.truth`/`app.localization.claims`/
`app.localization.copy` modules, the same way
`tests/test_localization_claims.py` and `tests/test_localization_copy.py`
do, rather than hand-inserting rows that would bypass their own composite
foreign keys.
"""
from __future__ import annotations

import itertools
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ExperimentDecisionRecord,
    GenerationRunRecord,
    JobRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    LocalizationAttemptRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchRecord,
    LocalizationItemProviderCallRecord,
    LocalizationItemRecord,
    LocalizedCopyApprovalRecord,
    LocalizedCopyVersionRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    OperationRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    SourceClaimInventoryRecord,
    TruthPackageMarketRecord,
    TruthPackageRecord,
    WorkspaceRecord,
    new_id,
)
from app.entitlements.admission import PaidOperationAdmission
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401 -- FK target
from app.entitlements.models import EntitlementProjectionRecord
from app.entitlements.policy import (
    LeaseExpiredError,
    NoEntitlementProjectionError,
)
from app.entitlements.policy import EntitlementPolicy
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import ReservationBook
from app.entitlements.storage_quota import StorageLimitReachedError, StorageLimitUnavailableError
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
    AttemptExhaustedError,
    BatchMarketTargetInput,
    BatchSourceInput,
    BatchValidationError,
    FrozenBatchError,
    StaleRevisionError,
    approve_copy,
    confirm,
    create_draft,
    derive_batch_outcome,
    retry_item,
    start_preparation,
    submit_claim_decisions,
)
from app.localization.claims import ClaimDecisionInput, create_inventory_from_analysis
from app.localization.evidence_tier import EvidenceTier
from app.localization.queries import get_item
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
)
from app.platform.tenancy import TenantContext
from tests.support.storage_limit import grant_storage_limit, set_storage_headroom
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

WS_ID = "ws_lb_batch"
WS_ID_NO_ENTITLEMENT = "ws_lb_batch_noent"

CTX = TenantContext(workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner")
CTX_NOENT = TenantContext(
    workspace_id=WS_ID_NO_ENTITLEMENT, role="owner", initiator_id="user_1", initiator_role="owner",
)

# `market_policies.market_id` is unique across the shared suite-wide
# database (spec §1.2) -- every dynamically-built market policy in this
# file draws from one monotonic counter so distinct test tags can never
# collide, even when two tags happen to share a truncated suffix.
_MARKET_ID_COUNTER = itertools.count(1)


PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}

_SOURCE_MARKET_SNAPSHOT = {
    "market": "US",
    "placement": "feed",
    "locale": "en-US",
    "country": "US",
    "currency": "USD",
}


# ---------------------------------------------------------------------------
# Winner-chain fixtures (mirrors tests/test_localization_eligibility.py)
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
        session.add(WorkspaceRecord(id=ws_id, name=f"Batch test {ws_id}"))
        session.flush()


def _seed_library_versions(
    session,
    ws_id: str,
    tag: str,
    *,
    source_market_snapshot: dict[str, str] | None = _SOURCE_MARKET_SNAPSHOT,
) -> tuple[str, str]:
    pv_entry, ov_entry = f"le_pv_{tag}", f"le_ov_{tag}"
    pv_id, ov_id = f"pv_{tag}", f"ov_{tag}"
    for entry_id, ver_id, etype in [(pv_entry, pv_id, "product"), (ov_entry, ov_id, "offer")]:
        session.add(LibraryEntryRecord(
            id=entry_id, workspace_id=ws_id, entry_type=etype, display_name=f"Test {etype} {tag}",
        ))
        session.flush()
        content = (
            {"source_market_snapshot": dict(source_market_snapshot)}
            if etype == "offer" and source_market_snapshot is not None
            else {}
        )
        session.add(LibraryVersionRecord(
            id=ver_id, workspace_id=ws_id, entry_id=entry_id, entry_type=etype,
            version_no=1, content_json=content, content_hash="c" * 64,
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
    """`ProductRecord.id` is a bare primary key (no composite Workspace key),
    so every Workspace this file uses needs its own Product row."""
    return f"prod_{ws_id}"


def _seed_asset_versions(
    session,
    ws_id: str,
    tag: str,
    count: int = 2,
    *,
    language: str = "en",
    product_id: str | None = None,
) -> list[str]:
    _ensure_workspace(session, ws_id)
    product_id = product_id or _product_id(ws_id)
    if session.get(ProductRecord, product_id) is None:
        session.add(ProductRecord(id=product_id, workspace_id=ws_id, name="P"))
        session.flush()

    plan_id = f"plan_{tag}"
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
                aspect_ratio="1:1", language=language, object_key=f"{tag}_{i}.png",
                content_sha256=f"{tag}{i}".rjust(64, "0"), byte_size=1, mime_type="image/png",
            ))
            session.flush()
        av_ids.append(av_id)
    return av_ids


def _seed_metrics(session, ws_id: str, tag: str, ad_ids: list[str]) -> None:
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


def _build_confirmed_winner(
    ws_id: str,
    ctx: TenantContext,
    tag: str,
    *,
    asset_language: str = "en",
    source_market_snapshot: dict[str, str] | None = _SOURCE_MARKET_SNAPSHOT,
    product_id: str | None = None,
    offer_markets: tuple[str, ...] = ("US",),
    library_versions: tuple[str, str] | None = None,
) -> dict:
    """Drive a real experiment to a confirmed material 'win' Decision.

    Mirrors `tests/test_localization_eligibility.py::_build_confirmed_winner`.
    `offer_markets` is the Offer's `market_scope`; the first one is the
    Winner's source market. `library_versions` is a `(ProductVersion,
    OfferVersion)` pair already published through the Library to use
    instead of seeding new ones; `govern_product_offer_versions` still fits
    that Offer's market, landing page and validity fields to the
    Experiments chain, as it does for seeded versions.
    """
    with tenant_session(ctx) as s:
        if library_versions is not None:
            pv_id, ov_id = library_versions
            # Library publish bridged the Product entry to its own `products` row.
            product_id = product_id or s.scalar(
                select(ProductRecord.id).where(
                    ProductRecord.workspace_id == ws_id,
                    ProductRecord.product_profile_entry_id == s.get(LibraryVersionRecord, pv_id).entry_id,
                )
            )
        else:
            product_id = product_id or _product_id(ws_id)
            pv_id, ov_id = _seed_library_versions(
                s,
                ws_id,
                tag,
                source_market_snapshot=source_market_snapshot,
            )
        avs = _seed_asset_versions(
            s, ws_id, tag, language=asset_language, product_id=product_id
        )
        govern_product_offer_versions(
            s,
            ctx,
            product_id=product_id,
            product_version_id=pv_id,
            offer_version_id=ov_id,
            markets=offer_markets,
        )
        govern_asset_versions(
            s,
            ctx,
            product_id=product_id,
            asset_version_ids=avs,
            tag=tag,
        )

    exp = create_experiment_v2(
        ctx, product_id=product_id, name=f"LB-{tag}", draft_context=_draft_context(pv_id, ov_id),
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
            "product_version_id": pv_id, "offer_version_id": ov_id, "product_id": product_id,
        }


def _seed_review(
    ctx: TenantContext, asset_version_id: str, *, product_id: str | None = None
) -> None:
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
            id=new_id("qr"), workspace_id=ctx.workspace_id,
            product_id=product_id or _product_id(ctx.workspace_id),
            asset_id=av.asset_id, asset_version_id=asset_version_id,
            review_value="approved", source="human", actor_id="reviewer_1",
        ))
        s.flush()


def _make_market_policy_version(session, market_id: str, *, restricted_claim_types=()) -> MarketPolicyVersionRecord:
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
        reserved_words_json=[], restricted_claim_types_json=list(restricted_claim_types),
        requires_local_copy=False, shipping_address_granularity="country",
        max_fact_staleness_seconds=86400, qc_rules_json={},
    )
    session.add(version)
    session.flush()
    return version


# ---------------------------------------------------------------------------
# TestCase
# ---------------------------------------------------------------------------


class TestBatchCommandSequence(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, WS_ID)
            s.merge(EntitlementProjectionRecord(
                workspace_id=WS_ID, entitlement_version=1, lease_sequence=1,
                projection_version=1, schema_version=2, semantic_hash="h" * 8,
                valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                resolved_entitlements_json={"storage.bytes.max": 10 * 1024 ** 3},
            ))
            s.add(SpendGuardrailRecord(
                workspace_id=WS_ID, revision=1, budget_currency="USD",
                per_operation_limit=Decimal("100.00"), rolling_24h_limit=Decimal("500.00"),
                period_limit=Decimal("5000.00"), outstanding_exposure_limit=Decimal("1000.00"),
                created_at=now, updated_at=now, updated_by="user_1",
            ))
        with tenant_session(CTX_NOENT) as s:
            _ensure_workspace(s, WS_ID_NO_ENTITLEMENT)
            # Deliberately no EntitlementProjectionRecord/SpendGuardrailRecord here.

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def setUp(self) -> None:
        self.admission = PaidOperationAdmission(
            policy=EntitlementPolicy(),
            quote_engine=CostQuoteEngine(price_registry=PRICE_REGISTRY, price_rule_version="test-v1"),
            reservation_book=ReservationBook(),
        )

    def test_confirmed_winner_preserves_explicit_product_identity(self) -> None:
        tag = f"customprod{next(_MARKET_ID_COUNTER)}"
        product_id = f"prod_{tag}"

        winner = _build_confirmed_winner(
            WS_ID,
            CTX,
            tag,
            product_id=product_id,
        )
        _seed_review(
            CTX,
            winner["asset_version_id"],
            product_id=product_id,
        )

        with tenant_session(CTX) as session:
            asset_version = session.get(
                AssetVersionRecord,
                winner["asset_version_id"],
            )
            asset = session.get(AssetRecord, asset_version.asset_id)
            review = session.scalar(
                select(QualityReviewRecord).where(
                    QualityReviewRecord.workspace_id == WS_ID,
                    QualityReviewRecord.asset_version_id
                    == winner["asset_version_id"],
                )
            )

            self.assertEqual(asset.product_id, product_id)
            self.assertEqual(review.product_id, product_id)

    # -- fixture builders -------------------------------------------

    def _build_ready_batch(
        self,
        tag: str,
        markets: tuple[str, ...] = ("US",),
        *,
        truth_overrides: dict[int, tuple[dict, tuple[FactPolicy, ...]]] | None = None,
    ) -> dict:
        """Build a Batch through the real command sequence up to (but not
        including) `confirm()`: one source, N target markets, Truth
        resolved, analysis reused, Claim decisions submitted (deterministic
        source-locale projection, no translation), and copy approved for
        every market. `truth_overrides` maps a market's index to the facts
        its Manual source holds and the `on_failure` rules it resolves
        under."""
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])

        with tenant_session(CTX) as s:
            mpvs = [
                _make_market_policy_version(s, f"{m}{next(_MARKET_ID_COUNTER):04d}"[:10])
                for m in markets
            ]

        # MarketPolicyVersionRecord has no ORM relationship back to
        # MarketPolicyRecord, so the market_id is fetched explicitly.
        with tenant_session(CTX) as s:
            market_targets = []
            mpv_rows = []
            for mpv in mpvs:
                row = s.get(MarketPolicyVersionRecord, mpv.id)
                mp = s.get(MarketPolicyRecord, row.market_policy_id)
                market_targets.append(BatchMarketTargetInput(market_policy_version_id=row.id, market=mp.market_id))
                mpv_rows.append((row.id, mp.market_id))

        truth_cmd = self._truth_command_for(
            tag, winner["product_version_id"], winner["offer_version_id"], mpv_rows,
            overrides=truth_overrides,
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
                market_targets=market_targets, actor_id="user_1",
            )
            batch_id = draft.batch_id

        recipe_hash = f"recipe-{tag}"
        with tenant_session(CTX) as s:
            create_inventory_from_analysis(
                s, CTX, asset_version_id=winner["asset_version_id"], analysis_recipe_hash=recipe_hash,
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
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
            )

        copy_version_ids = {}
        for index, (market_policy_version_id, market) in enumerate(mpv_rows):
            with tenant_session(CTX) as s:
                scd = submit_claim_decisions(
                    s, CTX, batch_id=batch_id,
                    expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                    asset_version_id=winner["asset_version_id"],
                    target_market=market, analysis_recipe_hash=recipe_hash,
                    decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                    copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                    translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
                    actor_id="user_1", role="owner", admission=self.admission,
                )
                copy_version_ids[market] = scd.copy_version_id
                batch = s.get(LocalizationBatchRecord, batch_id)
                if index < len(mpv_rows) - 1:
                    self.assertEqual(batch.phase, "preparing")
                    self.assertEqual(batch.preparation_stage, "claim_review")

            with tenant_session(CTX) as s:
                approve_copy(
                    s, CTX, batch_id=batch_id,
                    expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                    copy_version_id=copy_version_ids[market],
                    actor_id="user_1", role="owner",
                )
                batch = s.get(LocalizationBatchRecord, batch_id)
                if index < len(mpv_rows) - 1:
                    self.assertEqual(batch.phase, "preparing")
                    self.assertEqual(batch.preparation_stage, "claim_review")

        return {
            "batch_id": batch_id, "asset_version_id": winner["asset_version_id"],
            "decision_id": winner["decision_id"], "markets": [m for _, m in mpv_rows],
            "copy_version_ids": copy_version_ids, "recipe_hash": recipe_hash,
        }

    def _truth_command_for(
        self, tag: str, pv_id: str, ov_id: str, mpv_rows: list[tuple[str, str]],
        *, overrides: dict[int, tuple[dict, tuple[FactPolicy, ...]]] | None = None,
    ) -> TruthResolutionCommand:
        with tenant_session(CTX) as s:
            policy_version_id = _make_version(s, WS_ID, "truth_resolution_policy", tag)
            data_source_version_id = _make_version(s, WS_ID, "data_source", tag)
            manual_fact_set_version_id = _make_version(s, WS_ID, "manual_fact_set", tag)
            rows = [
                (mpv_id, s.get(MarketPolicyVersionRecord, mpv_id))
                for mpv_id, _ in mpv_rows
            ]

        now = datetime.now(timezone.utc)
        window = (now - timedelta(days=1), now + timedelta(days=365))
        facts = {"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}}

        fact_policies = (
            FactPolicy("price"),
            FactPolicy("availability"),
            FactPolicy("discount"),
            FactPolicy("shipping"),
            FactPolicy("delivery"),
            FactPolicy("product_claim"),
        )

        market_cmds = []
        for index, (mpv_id, market) in enumerate(mpv_rows):
            with tenant_session(CTX) as s:
                row = s.get(MarketPolicyVersionRecord, mpv_id)
                locale, country, currency = row.locale, row.country, row.currency
            market_facts, market_fact_policies = (overrides or {}).get(index, (facts, fact_policies))
            step = SourceStep(
                data_source_version_id=data_source_version_id, adapter=ManualFactSourceAdapter(),
                adapter_kind="manual", adapter_version="manual-v1", truth_data_mode="real",
                manual_fact_set=ManualFactSet(
                    version_id=manual_fact_set_version_id, facts=market_facts,
                    valid_from=window[0], valid_to=window[1],
                ),
                manual_fact_set_version_id=manual_fact_set_version_id,
            )
            market_cmds.append(MarketResolutionCommand(
                market=market, locale=locale, country=country, currency=currency,
                market_policy_version_id=mpv_id, sources=(step,),
                fact_policies=market_fact_policies,
            ))
        return TruthResolutionCommand(
            product_version_id=pv_id, offer_version_id=ov_id,
            truth_resolution_policy_version_id=policy_version_id, truth_data_mode="real",
            max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
            markets=tuple(market_cmds), now=now,
        )

    def _batch_revision(self, batch_id: str) -> int:
        with tenant_session(CTX) as s:
            return s.get(LocalizationBatchRecord, batch_id).revision

    def _item_revision(self, item_id: str) -> int:
        with tenant_session(CTX) as s:
            return s.get(LocalizationItemRecord, item_id).revision

    def _confirm(
        self, batch_id: str, *, edit_model: str = "mock-edit-1", expected_revision: int | None = None,
    ) -> object:
        if expected_revision is None:
            expected_revision = self._batch_revision(batch_id)
        with tenant_session(CTX) as s:
            return confirm(
                s, CTX, batch_id=batch_id, expected_revision=expected_revision,
                edit_provider="mock-edit", edit_model=edit_model,
                edit_adapter_version="mock-edit-v1", actor_id="user_1", role="owner",
                admission=self.admission,
            )

    # -- tests --------------------------------------------------------

    def test_preparation_binds_every_pair_before_becoming_ready(self) -> None:
        ready = self._build_ready_batch("pairs01", markets=("US", "GB"))

        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            recipe = batch.frozen_recipe_json
            self.assertEqual(batch.phase, "ready")
            self.assertEqual(batch.preparation_stage, "complete")
            self.assertEqual(len(recipe["claim_decision_set_ids"]), 2)
            self.assertEqual(
                set(recipe["copy_version_ids"].values()),
                set(ready["copy_version_ids"].values()),
            )

    def test_preparation_mutations_reject_stale_batch_revision(self) -> None:
        ready = self._build_ready_batch("prep_cas01")
        copy_version_id = next(iter(ready["copy_version_ids"].values()))
        with tenant_session(CTX) as session:
            current_revision = session.get(
                LocalizationBatchRecord, ready["batch_id"]
            ).revision

        with self.assertRaises(StaleRevisionError):
            with tenant_session(CTX) as session:
                approve_copy(
                    session,
                    CTX,
                    batch_id=ready["batch_id"],
                    expected_revision=current_revision - 1,
                    copy_version_id=copy_version_id,
                    actor_id="concurrent-approver",
                    role="member",
                )

        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            approval = session.scalar(
                select(LocalizedCopyApprovalRecord).where(
                    LocalizedCopyApprovalRecord.workspace_id == CTX.workspace_id,
                    LocalizedCopyApprovalRecord.copy_version_id == copy_version_id,
                    LocalizedCopyApprovalRecord.actor_id == "concurrent-approver",
                )
            )
            self.assertEqual(batch.revision, current_revision)
            self.assertIsNone(approval)

    def test_confirm_projects_unavailable_pair_without_job_and_preserves_sibling(
        self,
    ) -> None:
        ready = self._build_ready_batch(
            "partial_unavailable01", markets=("GB", "AU")
        )
        blocked_market = ready["markets"][1]
        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            package = session.get(TruthPackageRecord, batch.truth_package_id)
            package.status = "degraded"
            markets = session.scalars(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.workspace_id == CTX.workspace_id,
                    TruthPackageMarketRecord.truth_package_id == batch.truth_package_id,
                )
            ).all()
            for market in markets:
                if market.market == blocked_market:
                    market.readiness = "unavailable"
                    market.readiness_detail_json = {
                        "reason": "skipped_unavailable"
                    }
                else:
                    market.readiness = "ready"
                    market.readiness_detail_json = {
                        "resolved_fact_types": ["price", "availability"]
                    }

        confirmed = self._confirm(ready["batch_id"])
        with tenant_session(CTX) as session:
            items = session.scalars(
                select(LocalizationItemRecord).where(
                    LocalizationItemRecord.batch_id == ready["batch_id"]
                )
            ).all()
            by_market = {item.target_market: item for item in items}
            jobs = session.scalars(
                select(JobRecord).where(
                    JobRecord.operation_id == confirmed.operation_id,
                    JobRecord.type == "localization.edit_attempt",
                )
            ).all()
            attempts = session.scalars(
                select(LocalizationAttemptRecord).where(
                    LocalizationAttemptRecord.batch_id == ready["batch_id"]
                )
            ).all()

            self.assertEqual(len(items), 2)
            self.assertEqual(by_market[blocked_market].state, "skipped")
            self.assertEqual(
                by_market[blocked_market].reason_code,
                "skipped_market_unavailable",
            )
            self.assertEqual(by_market[blocked_market].attempt_count, 0)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].resource_id, by_market[ready["markets"][0]].id)
            self.assertEqual(len(attempts), 1)

            executable = by_market[ready["markets"][0]]
            executable.state = "succeeded"
            executable.revision += 1
            batch_revision = session.get(
                LocalizationBatchRecord, ready["batch_id"]
            ).revision

        with tenant_session(CTX) as session:
            outcome = derive_batch_outcome(
                session,
                CTX,
                ready["batch_id"],
                expected_revision=batch_revision,
            )
        self.assertEqual(outcome, "partially_succeeded")

    def test_failed_price_pair_requires_policy_permission_but_not_sibling_loss(
        self,
    ) -> None:
        ready = self._build_ready_batch(
            "partial_price01", markets=("GB", "CA")
        )
        failed_market = ready["markets"][1]
        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            package = session.get(TruthPackageRecord, batch.truth_package_id)
            package.status = "degraded"
            target = session.scalar(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.truth_package_id
                    == batch.truth_package_id,
                    TruthPackageMarketRecord.market == failed_market,
                )
            )
            target.readiness = "failed"
            target.readiness_detail_json = {
                "reason": "price_unresolved",
                "missing": ["price"],
            }
            policy = session.get(
                MarketPolicyVersionRecord, target.market_policy_version_id
            )
            policy.pricing_context_json = {"requires_local_price": True}

        confirmed = self._confirm(ready["batch_id"])
        with tenant_session(CTX) as session:
            items = session.scalars(
                select(LocalizationItemRecord).where(
                    LocalizationItemRecord.batch_id == ready["batch_id"]
                )
            ).all()
            by_market = {item.target_market: item for item in items}
            failed = by_market[failed_market]
            sibling = by_market[ready["markets"][0]]
            self.assertEqual(failed.state, "failed")
            self.assertEqual(failed.reason_code, "truth_price_unresolved")
            self.assertIn('"missing":["price"]', failed.reason_detail)
            self.assertEqual(failed.attempt_count, 0)
            job_resource_ids = set(
                session.scalars(
                    select(JobRecord.resource_id).where(
                        JobRecord.operation_id == confirmed.operation_id
                    )
                ).all()
            )
            self.assertEqual(job_resource_ids, {sibling.id})

    def test_failed_price_pair_can_run_when_no_source_price_claim_is_preserved(
        self,
    ) -> None:
        ready = self._build_ready_batch(
            "price_removed01", markets=("GB", "CA")
        )
        recovered_market = ready["markets"][1]
        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            package = session.get(TruthPackageRecord, batch.truth_package_id)
            package.status = "degraded"
            target = session.scalar(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.truth_package_id
                    == batch.truth_package_id,
                    TruthPackageMarketRecord.market == recovered_market,
                )
            )
            target.readiness = "failed"
            target.readiness_detail_json = {
                "reason": "price_unresolved",
                "missing": ["price"],
            }

        confirmed = self._confirm(ready["batch_id"])
        with tenant_session(CTX) as session:
            items = session.scalars(
                select(LocalizationItemRecord).where(
                    LocalizationItemRecord.batch_id == ready["batch_id"]
                )
            ).all()
            self.assertEqual({item.state for item in items}, {"queued"})
            jobs = session.scalars(
                select(JobRecord).where(
                    JobRecord.operation_id == confirmed.operation_id,
                    JobRecord.type == "localization.edit_attempt",
                )
            ).all()
            self.assertEqual(len(jobs), 2)

    def _confirm_with_blocked_market(
        self, tag: str, market_facts: dict, market_fact_policies: tuple[FactPolicy, ...],
    ) -> tuple[dict, object]:
        """Two markets resolved through Truth; the second one's `block` fact
        has no source. Neither MarketPolicy requires a local price and the
        source's only Claim is a headline, so the price exception would let
        a price-unresolved market run."""
        ready = self._build_ready_batch(
            tag, markets=("GB", "CA"),
            truth_overrides={1: (market_facts, market_fact_policies)},
        )
        return ready, self._confirm(ready["batch_id"])

    def _assert_blocked_item_and_running_sibling(
        self, ready: dict, confirmed: object, blocked: list[str],
    ) -> LocalizationItemRecord:
        blocked_market, sibling_market = ready["markets"][1], ready["markets"][0]
        with tenant_session(CTX) as session:
            items = session.scalars(
                select(LocalizationItemRecord).where(
                    LocalizationItemRecord.batch_id == ready["batch_id"]
                )
            ).all()
            by_market = {item.target_market: item for item in items}
            failed, sibling = by_market[blocked_market], by_market[sibling_market]
            self.assertEqual(failed.state, "failed")
            self.assertEqual(failed.reason_code, "truth_fact_missing_blocked")
            self.assertEqual(failed.attempt_count, 0)
            self.assertEqual(failed.next_action, "create_new_batch")
            self.assertEqual(sibling.state, "queued")
            job_resource_ids = set(
                session.scalars(
                    select(JobRecord.resource_id).where(
                        JobRecord.operation_id == confirmed.operation_id,
                        JobRecord.type == "localization.edit_attempt",
                    )
                ).all()
            )
            self.assertEqual(job_resource_ids, {sibling.id})

            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            tpm = session.scalar(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.truth_package_id == batch.truth_package_id,
                    TruthPackageMarketRecord.market == blocked_market,
                )
            )
            self.assertEqual(tpm.readiness, "failed")
            self.assertEqual(tpm.readiness_detail_json["reason"], "fact_missing_blocked")
            self.assertEqual(tpm.readiness_detail_json["blocked"], blocked)
            return sibling

    def test_blocked_price_market_skips_the_price_exception(self) -> None:
        ready, confirmed = self._confirm_with_blocked_market(
            "blocked_price01",
            {"availability": {"available": True}},
            (FactPolicy("price", on_failure="block"), FactPolicy("availability", on_failure="block")),
        )
        self._assert_blocked_item_and_running_sibling(ready, confirmed, ["price"])

    def test_blocked_fact_market_fails_while_sibling_succeeds(self) -> None:
        ready, confirmed = self._confirm_with_blocked_market(
            "blocked_discount01",
            {"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}},
            (
                FactPolicy("price", on_failure="block"),
                FactPolicy("availability", on_failure="block"),
                FactPolicy("discount", on_failure="block"),
            ),
        )
        sibling = self._assert_blocked_item_and_running_sibling(ready, confirmed, ["discount"])

        with tenant_session(CTX) as session:
            executable = session.get(LocalizationItemRecord, sibling.id)
            executable.state = "succeeded"
            executable.revision += 1
            batch_revision = session.get(LocalizationBatchRecord, ready["batch_id"]).revision
        with tenant_session(CTX) as session:
            outcome = derive_batch_outcome(
                session, CTX, ready["batch_id"], expected_revision=batch_revision,
            )
        self.assertEqual(outcome, "partially_succeeded")

    def test_v1_degraded_market_still_runs_at_confirm(self) -> None:
        # A batch bound to a package frozen under `truth-rule-v1`, which left
        # a market missing a `block` fact `degraded`: confirm keeps that
        # frozen readiness instead of recomputing it (#401 decision 4a).
        ready = self._build_ready_batch(
            "v1_degraded01", markets=("GB", "CA"),
            truth_overrides={1: (
                {"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}},
                (
                    FactPolicy("price", on_failure="block"),
                    FactPolicy("availability", on_failure="block"),
                    FactPolicy("discount", on_failure="block"),
                ),
            )},
        )
        legacy_market = ready["markets"][1]
        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            target = session.scalar(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.truth_package_id == batch.truth_package_id,
                    TruthPackageMarketRecord.market == legacy_market,
                )
            )
            target.readiness = "degraded"
            target.readiness_detail_json = {
                "reason": "fact_missing_permitted",
                "missing": ["discount"],
            }

        confirmed = self._confirm(ready["batch_id"])
        with tenant_session(CTX) as session:
            items = session.scalars(
                select(LocalizationItemRecord).where(
                    LocalizationItemRecord.batch_id == ready["batch_id"]
                )
            ).all()
            self.assertEqual({item.state for item in items}, {"queued"})
            jobs = session.scalars(
                select(JobRecord).where(
                    JobRecord.operation_id == confirmed.operation_id,
                    JobRecord.type == "localization.edit_attempt",
                )
            ).all()
            self.assertEqual(len(jobs), 2)

    def test_failed_truth_package_blocks_confirmation_without_items(self) -> None:
        ready = self._build_ready_batch("failed_package01")
        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            package = session.get(TruthPackageRecord, batch.truth_package_id)
            package.status = "failed"

        with self.assertRaises(BatchValidationError) as captured:
            self._confirm(ready["batch_id"])
        self.assertEqual(captured.exception.findings, ("truth_package_failed",))
        with tenant_session(CTX) as session:
            self.assertEqual(
                session.scalar(
                    select(LocalizationItemRecord).where(
                        LocalizationItemRecord.batch_id == ready["batch_id"]
                    )
                ),
                None,
            )

    def test_client_locales_cannot_bypass_translation_planning(self) -> None:
        ready = self._build_ready_batch("locale_spoof01")
        with tenant_session(CTX) as session:
            revision = session.get(
                LocalizationBatchRecord, ready["batch_id"]
            ).revision

        with self.assertRaises(BatchValidationError) as error:
            with tenant_session(CTX) as session:
                submit_claim_decisions(
                    session,
                    CTX,
                    batch_id=ready["batch_id"],
                    expected_revision=revision,
                    asset_version_id=ready["asset_version_id"],
                    target_market=ready["markets"][0],
                    analysis_recipe_hash=ready["recipe_hash"],
                    decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                    copy_policy_snapshot={},
                    source_locale="fake-locale",
                    target_locale="fake-locale",
                    translation_recipe=None,
                    requested_evidence_tier=EvidenceTier.MOCK,
                    actor_id="user_1",
                    role="owner",
                    admission=self.admission,
                )
        self.assertEqual(
            error.exception.findings,
            ("source_locale_mismatch", "target_locale_mismatch"),
        )

    def test_item_cost_uses_only_that_items_jobs(self) -> None:
        ready = self._build_ready_batch("item_cost01", markets=("US", "GB"))
        with tenant_session(CTX) as session:
            batch = session.get(LocalizationBatchRecord, ready["batch_id"])
            confirmed = confirm(
                session,
                CTX,
                batch_id=ready["batch_id"],
                expected_revision=batch.revision,
                edit_provider="mock-edit",
                edit_model="mock-edit-1",
                edit_adapter_version="mock-v1",
                actor_id="user_1",
                role="owner",
                admission=self.admission,
            )

        with tenant_session(CTX) as session:
            # The historical SQLite migration retains the pre-Localization
            # provider step CHECK; PostgreSQL and current ORM metadata already
            # admit localization_edit. This fixture needs the current shape.
            if session.bind.dialect.name == "sqlite":
                session.execute(text("PRAGMA ignore_check_constraints = ON"))
            command = session.scalar(
                select(LocalizationBatchCommandRecord).where(
                    LocalizationBatchCommandRecord.workspace_id == CTX.workspace_id,
                    LocalizationBatchCommandRecord.operation_id
                    == confirmed.operation_id,
                )
            )
            for index, item_id in enumerate(confirmed.item_ids, start=1):
                item = session.get(LocalizationItemRecord, item_id)
                attempt = session.scalar(
                    select(LocalizationAttemptRecord).where(
                        LocalizationAttemptRecord.workspace_id == CTX.workspace_id,
                        LocalizationAttemptRecord.item_id == item_id,
                    )
                )
                job = session.scalar(
                    select(JobRecord).where(
                        JobRecord.workspace_id == CTX.workspace_id,
                        JobRecord.operation_id == confirmed.operation_id,
                        JobRecord.resource_id == item_id,
                    )
                )
                reservation = session.scalar(
                    select(SpendReservationRecord).where(
                        SpendReservationRecord.workspace_id == CTX.workspace_id,
                        SpendReservationRecord.job_id == job.id,
                    )
                )
                now = datetime.now(timezone.utc)
                if reservation is None:
                    reservation = SpendReservationRecord(
                        id=new_id("sr"),
                        workspace_id=CTX.workspace_id,
                        operation_id=confirmed.operation_id,
                        job_id=job.id,
                        semantic_key_hash=f"item-cost-reservation-{item_id}",
                        state="settled",
                        provider="mock-edit",
                        execution_mode="mock",
                        provider_currency="USD",
                        maximum_provider_amount=Decimal(index),
                        budget_currency="USD",
                        maximum_budget_amount=Decimal(index),
                        outstanding_budget_amount=Decimal("0"),
                        entitlement_version=1,
                        lease_sequence=1,
                        guardrail_revision=1,
                        platform_policy_version=1,
                        created_at=now,
                        updated_at=now,
                        settled_at=now,
                    )
                    session.add(reservation)
                    session.flush()
                call_id = new_id("pca")
                session.add(
                    ProviderCallAttemptRecord(
                        id=call_id,
                        workspace_id=CTX.workspace_id,
                        operation_id=confirmed.operation_id,
                        job_id=job.id,
                        spend_reservation_id=None,
                        call_semantic_key_hash=f"item-cost-call-{item_id}",
                        attempt_no=1,
                        execution_mode="mock",
                        provider="mock-edit",
                        model="mock-edit-1",
                        dispatch_state="succeeded",
                        prepared_at=now,
                        completed_at=now,
                        updated_at=now,
                        step_kind="localization_edit",
                        attempt_number=1,
                        chargeability="free_mock",
                        truth_package_id=item.truth_package_id,
                    )
                )
                session.flush()
                session.add(
                    LocalizationItemProviderCallRecord(
                        id=new_id("lipc"),
                        workspace_id=CTX.workspace_id,
                        item_id=item_id,
                        attempt_id=attempt.id,
                        batch_id=ready["batch_id"],
                        batch_command_id=command.id,
                        operation_id=confirmed.operation_id,
                        truth_package_id=item.truth_package_id,
                        provider_execution_mode="mock",
                        provider_step_kind="localization_edit",
                        provider_call_attempt_id=call_id,
                    )
                )
                session.add(
                    ProviderCostLedgerEntryRecord(
                        id=new_id("pcl"),
                        workspace_id=CTX.workspace_id,
                        operation_id=confirmed.operation_id,
                        job_id=job.id,
                        spend_reservation_id=reservation.id,
                        credential_version_id=None,
                        entry_type="adjustment",
                        direction="increase",
                        execution_mode="mock",
                        semantic_key_hash=f"item-cost-ledger-{item_id}",
                        provider="mock-edit",
                        model="mock-edit-1",
                        provider_amount=Decimal(index),
                        provider_currency="USD",
                        budget_amount=Decimal(index),
                        budget_currency="USD",
                        exchange_rate=Decimal("1"),
                        created_at=now,
                    )
                )
            session.flush()
            if session.bind.dialect.name == "sqlite":
                session.execute(text("PRAGMA ignore_check_constraints = OFF"))

        with tenant_session(CTX) as session:
            first = get_item(
                session, CTX, ready["batch_id"], confirmed.item_ids[0]
            )
            second = get_item(
                session, CTX, ready["batch_id"], confirmed.item_ids[1]
            )
        self.assertEqual(first["cost"]["totals"], [{"amount": "1.000000000000", "currency": "USD"}])
        self.assertEqual(second["cost"]["totals"], [{"amount": "2.000000000000", "currency": "USD"}])

    def test_create_draft_rejects_product_offer_mismatch(self) -> None:
        """Spec §16.2: "a Batch cannot mix ProductVersion, OfferVersion,
        Workspace or source market". A source whose real Winner context
        (Decision -> ExperimentVersion) belongs to a *different*
        Product/Offer than the Batch declares must be rejected, even
        though `validate_source_for_batch` alone has no opinion on it."""
        batch_winner = _build_confirmed_winner(WS_ID, CTX, "podecl01")
        other_winner = _build_confirmed_winner(WS_ID, CTX, "pomism01")
        _seed_review(CTX, batch_winner["asset_version_id"])
        _seed_review(CTX, other_winner["asset_version_id"])
        self.assertNotEqual(batch_winner["product_version_id"], other_winner["product_version_id"])

        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, "USPOM")
        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv.id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        with self.assertRaises(BatchValidationError) as ctx_mgr:
            with tenant_session(CTX) as s:
                create_draft(
                    s, CTX, product_version_id=batch_winner["product_version_id"],
                    offer_version_id=batch_winner["offer_version_id"],
                    source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                    truth_data_mode="real", provider_execution_mode="mock",
                    sources=[BatchSourceInput(
                        # A source drawn from a *different* Product/Offer
                        # experiment than the Batch declares.
                        asset_version_id=other_winner["asset_version_id"],
                        decision_id=other_winner["decision_id"],
                        source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                    )],
                    market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv.id, market=market)],
                    actor_id="user_1",
                )
        self.assertTrue(
            any("product_offer_mismatch" in f for f in ctx_mgr.exception.findings),
            ctx_mgr.exception.findings,
        )

        with tenant_session(CTX) as s:
            count = len(s.scalars(
                select(LocalizationBatchRecord).where(
                    LocalizationBatchRecord.workspace_id == WS_ID,
                    LocalizationBatchRecord.product_version_id == batch_winner["product_version_id"],
                )
            ).all())
        self.assertEqual(count, 0)

    def test_analysis_incomplete_blocks_translation(self) -> None:
        tag = "ai01"
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])
        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, "USANAL")
        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv.id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        truth_cmd = self._truth_command_for(
            tag, winner["product_version_id"], winner["offer_version_id"], [(mpv.id, market)],
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
                market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv.id, market=market)],
                actor_id="user_1",
            )
            batch_id = draft.batch_id

        # No Inventory pre-seeded: start_preparation admits a real analysis
        # Job (and moves preparation_stage to "analysis"), but the analysis
        # never actually completes in this test.
        with tenant_session(CTX) as s:
            sp = start_preparation(
                s, CTX, batch_id=batch_id, truth_command=truth_cmd,
                analysis_recipe_hash=f"recipe-{tag}", analysis_provider="mock-vision",
                analysis_model="mock-vision-1", analysis_adapter_version="mock-v1",
                actor_id="user_1", admission=self.admission,
            )
        self.assertEqual(sp.preparation_stage, "analysis")
        self.assertEqual(sp.missing_analysis, (winner["asset_version_id"],))

        with self.assertRaises(BatchValidationError) as ctx_mgr:
            with tenant_session(CTX) as s:
                submit_claim_decisions(
                    s, CTX, batch_id=batch_id,
                    expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                    asset_version_id=winner["asset_version_id"],
                    target_market=market, analysis_recipe_hash=f"recipe-{tag}",
                    decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                    copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                    translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
                    actor_id="user_1", role="owner", admission=self.admission,
                )
        self.assertIn("analysis_incomplete", ctx_mgr.exception.findings)

        with tenant_session(CTX) as s:
            count = len(s.scalars(
                select(LocalizedCopyVersionRecord).where(
                    LocalizedCopyVersionRecord.workspace_id == WS_ID,
                    LocalizedCopyVersionRecord.source_asset_version_id == winner["asset_version_id"],
                )
            ).all())
        self.assertEqual(count, 0)

    def test_unresolved_high_risk_blocks_translation(self) -> None:
        tag = "hr01"
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])
        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, "USHR")
        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv.id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        truth_cmd = self._truth_command_for(
            tag, winner["product_version_id"], winner["offer_version_id"], [(mpv.id, market)],
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
                market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv.id, market=market)],
                actor_id="user_1",
            )
            batch_id = draft.batch_id

        recipe_hash = f"recipe-{tag}"
        with tenant_session(CTX) as s:
            create_inventory_from_analysis(
                s, CTX, asset_version_id=winner["asset_version_id"], analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "health_claim", "scope_key": f"k1_{tag}", "ocr_text": "Cures everything",
                    "confidence": 0.5, "risk": "high", "conflicting": False,
                }],
                provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                analysis_execution_mode="mock", adapter_version="mock-v1",
                mock_scenario="default", mock_version="v1",
            )
        with tenant_session(CTX) as s:
            sp = start_preparation(
                s, CTX, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
                analysis_provider="mock-vision", analysis_model="mock-vision-1",
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
            )
        self.assertEqual(sp.preparation_stage, "claim_review")

        with self.assertRaises(BatchValidationError) as ctx_mgr:
            with tenant_session(CTX) as s:
                submit_claim_decisions(
                    s, CTX, batch_id=batch_id,
                    expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                    asset_version_id=winner["asset_version_id"],
                    target_market=market, analysis_recipe_hash=recipe_hash,
                    decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                    copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                    translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
                    actor_id="user_1", role="owner", admission=self.admission,
                )
        self.assertIn("claim_0_requires_confirmation", ctx_mgr.exception.findings)

        with tenant_session(CTX) as s:
            count = len(s.scalars(
                select(LocalizedCopyVersionRecord).where(
                    LocalizedCopyVersionRecord.workspace_id == WS_ID,
                    LocalizedCopyVersionRecord.source_asset_version_id == winner["asset_version_id"],
                )
            ).all())
        self.assertEqual(count, 0)

    def test_reused_work_creates_no_operation(self) -> None:
        tag = "reuse01"
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])
        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, "USRE")
        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv.id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        truth_cmd = self._truth_command_for(
            tag, winner["product_version_id"], winner["offer_version_id"], [(mpv.id, market)],
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
                market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv.id, market=market)],
                actor_id="user_1",
            )
            batch_id = draft.batch_id

        recipe_hash = f"recipe-{tag}"
        with tenant_session(CTX) as s:
            create_inventory_from_analysis(
                s, CTX, asset_version_id=winner["asset_version_id"], analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello",
                    "confidence": 0.99, "risk": "low", "conflicting": False,
                }],
                provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                analysis_execution_mode="mock", adapter_version="mock-v1",
                mock_scenario="default", mock_version="v1",
            )

        with tenant_session(CTX) as s:
            sp = start_preparation(
                s, CTX, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
                analysis_provider="mock-vision", analysis_model="mock-vision-1",
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
            )

        self.assertIsNone(sp.operation_id)
        self.assertEqual(sp.missing_analysis, ())
        self.assertEqual(sp.preparation_stage, "claim_review")

        with tenant_session(CTX) as s:
            cmd_row = s.scalar(
                select(LocalizationBatchCommandRecord).where(
                    LocalizationBatchCommandRecord.workspace_id == WS_ID,
                    LocalizationBatchCommandRecord.batch_id == batch_id,
                    LocalizationBatchCommandRecord.command_kind == "start_preparation",
                )
            )
            self.assertIsNotNone(cmd_row)
            self.assertIsNone(cmd_row.operation_id)

            jobs = s.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == WS_ID,
                    JobRecord.resource_type == "asset_version",
                    JobRecord.resource_id == winner["asset_version_id"],
                )
            ).all()
            self.assertEqual(len(jobs), 0)

    def test_entitlement_failure_rolls_back_fully(self) -> None:
        tag = "ent01"
        winner = _build_confirmed_winner(WS_ID_NO_ENTITLEMENT, CTX_NOENT, tag)
        _seed_review(CTX_NOENT, winner["asset_version_id"])
        with tenant_session(CTX_NOENT) as s:
            mpv = _make_market_policy_version(s, "USENT")
        with tenant_session(CTX_NOENT) as s:
            row = s.get(MarketPolicyVersionRecord, mpv.id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        with tenant_session(CTX_NOENT) as s:
            policy_version_id = _make_version(s, WS_ID_NO_ENTITLEMENT, "truth_resolution_policy", tag)
            data_source_version_id = _make_version(s, WS_ID_NO_ENTITLEMENT, "data_source", tag)
            manual_fact_set_version_id = _make_version(s, WS_ID_NO_ENTITLEMENT, "manual_fact_set", tag)

        now = datetime.now(timezone.utc)
        window = (now - timedelta(days=1), now + timedelta(days=365))
        facts = {"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}}
        step = SourceStep(
            data_source_version_id=data_source_version_id, adapter=ManualFactSourceAdapter(),
            adapter_kind="manual", adapter_version="manual-v1", truth_data_mode="real",
            manual_fact_set=ManualFactSet(
                version_id=manual_fact_set_version_id, facts=facts, valid_from=window[0], valid_to=window[1],
            ),
            manual_fact_set_version_id=manual_fact_set_version_id,
        )
        truth_cmd = TruthResolutionCommand(
            product_version_id=winner["product_version_id"], offer_version_id=winner["offer_version_id"],
            truth_resolution_policy_version_id=policy_version_id, truth_data_mode="real",
            max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
            markets=(MarketResolutionCommand(
                market=market, locale=mpv.locale, country=mpv.country, currency=mpv.currency,
                market_policy_version_id=mpv.id, sources=(step,),
                fact_policies=(FactPolicy("price"), FactPolicy("availability")),
            ),), now=now,
        )

        with tenant_session(CTX_NOENT) as s:
            draft = create_draft(
                s, CTX_NOENT, product_version_id=winner["product_version_id"],
                offer_version_id=winner["offer_version_id"], source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                truth_data_mode="real", provider_execution_mode="mock",
                sources=[BatchSourceInput(
                    asset_version_id=winner["asset_version_id"], decision_id=winner["decision_id"],
                    source_market_snapshot={"market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD"},
                )],
                market_targets=[BatchMarketTargetInput(market_policy_version_id=mpv.id, market=market)],
                actor_id="user_1",
            )
            batch_id = draft.batch_id

        recipe_hash = f"recipe-{tag}"
        with tenant_session(CTX_NOENT) as s:
            create_inventory_from_analysis(
                s, CTX_NOENT, asset_version_id=winner["asset_version_id"], analysis_recipe_hash=recipe_hash,
                claims=[{
                    "claim_type": "headline", "scope_key": f"k1_{tag}", "ocr_text": "Hello",
                    "confidence": 0.99, "risk": "low", "conflicting": False,
                }],
                provider="mock-vision", model="mock-vision-1", rule_version="claims-rule-v1",
                analysis_execution_mode="mock", adapter_version="mock-v1",
                mock_scenario="default", mock_version="v1",
            )
        # start_preparation admits nothing (Inventory already reused), so it
        # succeeds even without an EntitlementProjectionRecord.
        with tenant_session(CTX_NOENT) as s:
            start_preparation(
                s, CTX_NOENT, batch_id=batch_id, truth_command=truth_cmd, analysis_recipe_hash=recipe_hash,
                analysis_provider="mock-vision", analysis_model="mock-vision-1",
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
            )
        with tenant_session(CTX_NOENT) as s:
            scd = submit_claim_decisions(
                s, CTX_NOENT, batch_id=batch_id,
                expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                asset_version_id=winner["asset_version_id"],
                target_market=market, analysis_recipe_hash=recipe_hash,
                decisions=[ClaimDecisionInput(claim_index=0, action="preserve")],
                copy_policy_snapshot={}, source_locale="en-US", target_locale="en-US",
                translation_recipe=None, requested_evidence_tier=EvidenceTier.MOCK,
                actor_id="user_1", role="owner", admission=self.admission,
            )
            copy_version_id = scd.copy_version_id
        with tenant_session(CTX_NOENT) as s:
            approve_copy(
                s, CTX_NOENT, batch_id=batch_id,
                expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                copy_version_id=copy_version_id,
                actor_id="user_1", role="owner",
            )

        with tenant_session(CTX_NOENT) as s:
            expected_revision = s.get(LocalizationBatchRecord, batch_id).revision

        # confirm() always admits (edit Job) -- with no EntitlementProjection
        # for this Workspace, admission must fail before anything commits.
        # The storage check (#158) is the first to read the projection, and
        # fails closed on its absence as entitlement_unavailable.
        with self.assertRaises(StorageLimitUnavailableError):
            with tenant_session(CTX_NOENT) as s:
                confirm(
                    s, CTX_NOENT, batch_id=batch_id, expected_revision=expected_revision,
                    edit_provider="mock-edit",
                    edit_model="mock-edit-1", edit_adapter_version="mock-edit-v1",
                    actor_id="user_1", role="owner", admission=self.admission,
                )

        with tenant_session(CTX_NOENT) as s:
            batch = s.get(LocalizationBatchRecord, batch_id)
            self.assertIsNone(batch.frozen_at)
            self.assertNotEqual(batch.phase, "running")

            items = s.scalars(
                select(LocalizationItemRecord).where(LocalizationItemRecord.batch_id == batch_id)
            ).all()
            self.assertEqual(items, [])

            confirm_cmd = s.scalar(
                select(LocalizationBatchCommandRecord).where(
                    LocalizationBatchCommandRecord.workspace_id == WS_ID_NO_ENTITLEMENT,
                    LocalizationBatchCommandRecord.batch_id == batch_id,
                    LocalizationBatchCommandRecord.command_kind == "confirm",
                )
            )
            self.assertIsNone(confirm_cmd)

    def test_confirm_freezes_batch_creates_items_attempt1(self) -> None:
        ready = self._build_ready_batch("cf01")
        result = self._confirm(ready["batch_id"])

        self.assertFalse(result.replayed)
        self.assertEqual(len(result.item_ids), 1)
        self.assertIsNotNone(result.operation_id)

        with tenant_session(CTX) as s:
            batch = s.get(LocalizationBatchRecord, ready["batch_id"])
            self.assertIsNotNone(batch.frozen_at)
            self.assertEqual(batch.phase, "running")
            self.assertEqual(batch.evidence_tier, "mock")

            items = s.scalars(
                select(LocalizationItemRecord).where(LocalizationItemRecord.batch_id == ready["batch_id"])
            ).all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].attempt_count, 1)
            self.assertEqual(items[0].state, "queued")

            attempts = s.scalars(
                select(LocalizationAttemptRecord).where(LocalizationAttemptRecord.item_id == items[0].id)
            ).all()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].attempt_number, 1)
            self.assertEqual(attempts[0].state, "authorized")

            op = s.get(OperationRecord, result.operation_id)
            self.assertIsNotNone(op)

    def test_same_semantic_command_replays(self) -> None:
        ready = self._build_ready_batch("replay01")
        first = self._confirm(ready["batch_id"])
        second = self._confirm(ready["batch_id"])

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.item_ids, second.item_ids)
        self.assertEqual(first.operation_id, second.operation_id)

        with tenant_session(CTX) as s:
            items = s.scalars(
                select(LocalizationItemRecord).where(LocalizationItemRecord.batch_id == ready["batch_id"])
            ).all()
            self.assertEqual(len(items), 1)

            ops = s.scalars(
                select(OperationRecord).where(OperationRecord.id == first.operation_id)
            ).all()
            self.assertEqual(len(ops), 1)

            confirm_cmds = s.scalars(
                select(LocalizationBatchCommandRecord).where(
                    LocalizationBatchCommandRecord.workspace_id == WS_ID,
                    LocalizationBatchCommandRecord.batch_id == ready["batch_id"],
                    LocalizationBatchCommandRecord.command_kind == "confirm",
                )
            ).all()
            self.assertEqual(len(confirm_cmds), 1)

    def test_approve_copy_second_approver_appends_distinct_approval(self) -> None:
        """`approve_copy()`'s semantic key includes `actor_id` (unlike
        every other command's key): the same actor calling twice must
        replay idempotently, but a genuinely different Owner/Member
        approving the same CopyVersion must append its own
        `LocalizedCopyApprovalRecord`, never be silently dropped as a
        false-positive replay (spec §2F: "a later approval ... still
        appends its own record (e.g. a second required approver)")."""
        ready = self._build_ready_batch("secondapprover01")
        copy_version_id = ready["copy_version_ids"][ready["markets"][0]]

        # The same actor calling approve_copy again for the same
        # CopyVersion is an idempotent replay -- no second row.
        with tenant_session(CTX) as s:
            replay_result = approve_copy(
                s, CTX, batch_id=ready["batch_id"],
                expected_revision=s.get(
                    LocalizationBatchRecord, ready["batch_id"]
                ).revision,
                copy_version_id=copy_version_id,
                actor_id="user_1", role="owner",
            )
        with tenant_session(CTX) as s:
            approvals = s.scalars(
                select(LocalizedCopyApprovalRecord).where(
                    LocalizedCopyApprovalRecord.workspace_id == WS_ID,
                    LocalizedCopyApprovalRecord.copy_version_id == copy_version_id,
                )
            ).all()
        self.assertEqual(len(approvals), 1)
        self.assertEqual(replay_result.approval_id, approvals[0].id)

        # A genuinely different actor approving the same CopyVersion must
        # append its own approval, not be treated as a replay.
        with tenant_session(CTX) as s:
            second_result = approve_copy(
                s, CTX, batch_id=ready["batch_id"],
                expected_revision=s.get(
                    LocalizationBatchRecord, ready["batch_id"]
                ).revision,
                copy_version_id=copy_version_id,
                actor_id="user_2_second_approver", role="member",
            )
        with tenant_session(CTX) as s:
            approvals = s.scalars(
                select(LocalizedCopyApprovalRecord).where(
                    LocalizedCopyApprovalRecord.workspace_id == WS_ID,
                    LocalizedCopyApprovalRecord.copy_version_id == copy_version_id,
                )
            ).all()
        self.assertEqual(len(approvals), 2)
        self.assertNotEqual(second_result.approval_id, replay_result.approval_id)
        actor_ids = {a.actor_id for a in approvals}
        self.assertEqual(actor_ids, {"user_1", "user_2_second_approver"})

    def test_frozen_field_cannot_be_rewritten(self) -> None:
        ready = self._build_ready_batch("frozen01")
        self._confirm(ready["batch_id"], edit_model="mock-edit-1")

        with tenant_session(CTX) as s:
            batch = s.get(LocalizationBatchRecord, ready["batch_id"])
            original_recipe = dict(batch.frozen_recipe_json)

        with self.assertRaises(FrozenBatchError):
            self._confirm(ready["batch_id"], edit_model="a-different-edit-model")

        with tenant_session(CTX) as s:
            batch = s.get(LocalizationBatchRecord, ready["batch_id"])
            self.assertEqual(batch.frozen_recipe_json, original_recipe)
            self.assertEqual(batch.frozen_recipe_json["edit_model"], "mock-edit-1")

    def test_retry_item_creates_attempt2(self) -> None:
        ready = self._build_ready_batch("retry01")
        confirm_result = self._confirm(ready["batch_id"])
        item_id = confirm_result.item_ids[0]

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            item.state = "retry_wait"
            item.reason_code = "qc_failed"

        item_revision = self._item_revision(item_id)
        result = None
        with tenant_session(CTX) as s:
            result = retry_item(
                s, CTX, batch_id=ready["batch_id"], item_id=item_id,
                expected_item_revision=item_revision,
                actor_id="user_1", role="owner", admission=self.admission,
            )

        self.assertFalse(result.replayed)
        self.assertEqual(result.attempt_number, 2)
        self.assertIsNotNone(result.operation_id)

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            self.assertEqual(item.attempt_count, 2)
            self.assertEqual(item.state, "queued")
            self.assertEqual(item.revision, item_revision + 1)

            attempts = s.scalars(
                select(LocalizationAttemptRecord)
                .where(LocalizationAttemptRecord.item_id == item_id)
                .order_by(LocalizationAttemptRecord.attempt_number)
            ).all()
            self.assertEqual([a.attempt_number for a in attempts], [1, 2])
            self.assertEqual(attempts[1].state, "authorized")

    def test_storage_at_the_limit_refuses_confirm_and_retry_but_not_replays(self) -> None:
        """#158: localized outputs are new stored bytes, so confirm and a
        retry are refused once the Workspace stores its limit; one byte
        below they run, and a replayed confirm is never re-checked."""
        self.addCleanup(self._restore_storage_limit)
        ready = self._build_ready_batch("storage01")

        set_storage_headroom(CTX, 0)
        with self.assertRaises(StorageLimitReachedError):
            self._confirm(ready["batch_id"])
        with tenant_session(CTX) as s:
            self.assertIsNone(s.get(LocalizationBatchRecord, ready["batch_id"]).frozen_at)

        set_storage_headroom(CTX, 1)
        first = self._confirm(ready["batch_id"])
        set_storage_headroom(CTX, 0)
        self.assertTrue(self._confirm(ready["batch_id"]).replayed)

        item_id = first.item_ids[0]
        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            item.state = "retry_wait"
            item.reason_code = "qc_failed"
        item_revision = self._item_revision(item_id)

        def retry():
            with tenant_session(CTX) as s:
                return retry_item(
                    s, CTX, batch_id=ready["batch_id"], item_id=item_id,
                    expected_item_revision=item_revision,
                    actor_id="user_1", role="owner", admission=self.admission,
                )

        with self.assertRaises(StorageLimitReachedError):
            retry()
        self.assertEqual(self._item_revision(item_id), item_revision)
        set_storage_headroom(CTX, 1)
        self.assertEqual(retry().attempt_number, 2)

    @staticmethod
    def _restore_storage_limit() -> None:
        with platform_session() as session:
            grant_storage_limit(session, WS_ID)

    def test_retry_item_stale_revision_rejected(self) -> None:
        """Spec §3F: "all Item transitions use CAS and a losing writer
        reloads rather than overwriting" -- a caller that supplies a stale
        `expected_item_revision` must be rejected, not silently overwrite
        a concurrent transition."""
        ready = self._build_ready_batch("retrystale01")
        confirm_result = self._confirm(ready["batch_id"])
        item_id = confirm_result.item_ids[0]

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            item.state = "retry_wait"
        current_revision = self._item_revision(item_id)
        stale_revision = current_revision - 1
        self.assertGreaterEqual(stale_revision, 0)

        with self.assertRaises(StaleRevisionError):
            with tenant_session(CTX) as s:
                retry_item(
                    s, CTX, batch_id=ready["batch_id"], item_id=item_id,
                    expected_item_revision=stale_revision,
                    actor_id="user_1", role="owner", admission=self.admission,
                )

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            self.assertEqual(item.attempt_count, 1)
            self.assertEqual(item.revision, current_revision)
            attempts = s.scalars(
                select(LocalizationAttemptRecord).where(LocalizationAttemptRecord.item_id == item_id)
            ).all()
            self.assertEqual(len(attempts), 1)

    def test_attempt4_rejected(self) -> None:
        ready = self._build_ready_batch("attempt4_01")
        confirm_result = self._confirm(ready["batch_id"])
        item_id = confirm_result.item_ids[0]

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            item.attempt_count = 3
            item.state = "failed"
            item.reason_code = "qc_exhausted"
            item.recoverable = False

        item_revision = self._item_revision(item_id)
        with self.assertRaises(AttemptExhaustedError):
            with tenant_session(CTX) as s:
                retry_item(
                    s, CTX, batch_id=ready["batch_id"], item_id=item_id,
                    expected_item_revision=item_revision,
                    actor_id="user_1", role="owner", admission=self.admission,
                )

        with tenant_session(CTX) as s:
            attempts = s.scalars(
                select(LocalizationAttemptRecord).where(LocalizationAttemptRecord.item_id == item_id)
            ).all()
            self.assertEqual(len(attempts), 1)
            item = s.get(LocalizationItemRecord, item_id)
            self.assertEqual(item.attempt_count, 3)

    def test_derive_outcome_succeeded(self) -> None:
        ready = self._build_ready_batch("outcome_ok01")
        confirm_result = self._confirm(ready["batch_id"])
        item_id = confirm_result.item_ids[0]

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            item.state = "succeeded"

        batch_revision = self._batch_revision(ready["batch_id"])
        with tenant_session(CTX) as s:
            outcome = derive_batch_outcome(s, CTX, ready["batch_id"], expected_revision=batch_revision)

        self.assertEqual(outcome, "succeeded")
        with tenant_session(CTX) as s:
            batch = s.get(LocalizationBatchRecord, ready["batch_id"])
            self.assertEqual(batch.outcome, "succeeded")
            self.assertEqual(batch.phase, "closed")
            self.assertEqual(batch.revision, batch_revision + 1)

    def test_derive_outcome_failed(self) -> None:
        ready = self._build_ready_batch("outcome_fail01")
        confirm_result = self._confirm(ready["batch_id"])
        item_id = confirm_result.item_ids[0]

        with tenant_session(CTX) as s:
            item = s.get(LocalizationItemRecord, item_id)
            item.state = "failed"
            item.reason_code = "qc_exhausted"
            item.recoverable = False

        batch_revision = self._batch_revision(ready["batch_id"])
        with tenant_session(CTX) as s:
            outcome = derive_batch_outcome(s, CTX, ready["batch_id"], expected_revision=batch_revision)

        self.assertEqual(outcome, "failed")

    def test_derive_outcome_partially_succeeded(self) -> None:
        ready = self._build_ready_batch("outcome_partial01", markets=("US", "GB"))
        confirm_result = self._confirm(ready["batch_id"])
        self.assertEqual(len(confirm_result.item_ids), 2)

        with tenant_session(CTX) as s:
            first = s.get(LocalizationItemRecord, confirm_result.item_ids[0])
            first.state = "succeeded"
            second = s.get(LocalizationItemRecord, confirm_result.item_ids[1])
            second.state = "failed"
            second.reason_code = "qc_exhausted"
            second.recoverable = False

        batch_revision = self._batch_revision(ready["batch_id"])
        with tenant_session(CTX) as s:
            outcome = derive_batch_outcome(s, CTX, ready["batch_id"], expected_revision=batch_revision)

        self.assertEqual(outcome, "partially_succeeded")


if __name__ == "__main__":
    unittest.main()
