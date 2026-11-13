"""Shared Winner eligibility tests (Issue #86, spec §4.2).

Exercises ``evaluate_winner_identity()`` and ``validate_winner_eligibility()``
through their public interfaces only. Follows this repo's established
``unittest.TestCase`` fixture style (``tests/test_localization_eligibility.py``,
``tests/test_localization_binding.py``) rather than the pytest-fixture
skeleton sketched in the task-5 brief -- this codebase's test suite is
consistently ``unittest.TestCase``-based and duplicates fixture builders
per test file rather than sharing them across modules (see
``tests/test_localization_binding.py``'s note on that convention), so this
file follows suit instead of introducing a new pytest-fixture pattern the
rest of the suite does not use.

Winner Decisions are built through the real Experiments v2 flow
(``create_experiment_v2`` .. ``confirm_proposal_v2``), exactly as
``tests/test_localization_eligibility.py`` does, so the
Decision/confirmation/candidate/AssetVersion FK chain is exactly what
production code creates. Checks that don't depend on delivery context
(1-7, 11) reuse that lighter chain, purpose-building one
``ExperimentDecisionRecord`` per scenario the same way
``tests/test_localization_eligibility.py`` does. The two tests that need a
ready ``MarketAssetBindingRecord`` (checks 8-10) drive the real
Localization Batch pipeline (``create_draft`` .. ``confirm``) up to
``publish_binding()``, mirroring
``tests/test_localization_binding.py``'s ``_build_ready_batch()``.

Deviation from the task-5 brief's sample test file: the brief's skeleton
includes a ``test_meta_connection_not_active_raises`` case. Spec §4.2's
11-check list has no Meta-connection check, and the module docstring for
``validate_winner_eligibility()`` states it "deliberately knows nothing
about delivery targets" -- Meta connection state belongs to
``ScaleDeliveryTargetRecord``/``app.scale.enrollment_lineage.
lock_and_validate_enrollment_lineage()`` (spec §4.3/§4.4, a later task),
not Winner eligibility. That scenario is intentionally omitted here.
"""
from __future__ import annotations

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
    ExperimentVersionCandidateRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MetaAccountRecord,
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
from app.experiments.eligibility import append_eligibility_event
from app.experiments.service import (
    add_entry_v2,
    bind_candidate_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.experiments.winner_eligibility import (
    EligibleWinnerLineage,
    WinnerIdentity,
    WinnerIneligibleError,
    evaluate_winner_identity,
    validate_winner_eligibility,
)
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
from app.localization.truth import FactPolicy, MarketResolutionCommand, SourceStep, TruthResolutionCommand
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import govern_asset_versions, govern_product_offer_versions
from tests.support.market_codes import next_market_code as _next_market

CTX = TenantContext(
    workspace_id="ws_winelig",
    role="owner",
    initiator_id="user_winelig",
    request_id="req_winelig",
    trace_id="trace_winelig",
)

CTX_OTHER = TenantContext(
    workspace_id="ws_winelig_other",
    role="owner",
    initiator_id="user_winelig_other",
    request_id="req_winelig_other",
    trace_id="trace_winelig_other",
)

PROD_ID = "prod_winelig"

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}

# ---------------------------------------------------------------------------
# Fixture helpers -- the light chain (checks 1-7, 11: no binding needed)
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
        session.add(WorkspaceRecord(id=ws_id, name=f"Winner eligibility test {ws_id}"))
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
        session.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P WinElig"))
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
    from app.adapters.persistence.models import AdDailyMetricRecord, MetaSyncRunRecord

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
    """Drive a real experiment to a confirmed material Decision, exactly as
    ``tests/test_localization_eligibility.py``'s identical helper does."""
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
        CTX, product_id=PROD_ID, name=f"WELIG-{tag}",
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
            "execution_mode": candidate.execution_mode,
        }


def _make_decision(
    chain: dict,
    *,
    verdict: str,
    confirmation_id: str | None,
    suffix: str = "",
) -> str:
    """Append one purpose-built Decision reusing ``chain``'s real identity,
    exactly as ``tests/test_localization_eligibility.py``'s identical
    helper does (PC-03 technique, ``tests/test_pattern_candidate.py``)."""
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


def _seed_review(asset_version_id: str, review_value: str | None) -> None:
    """Attach a QualityReview to an AssetVersion, or clear all when ``None``."""
    if review_value is None:
        with tenant_session(CTX) as s:
            reviews = s.scalars(select(QualityReviewRecord).where(
                QualityReviewRecord.asset_version_id == asset_version_id,
            )).all()
            for review in reviews:
                s.delete(review)
        return
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
# Fixture helpers -- the heavy chain (checks 8-10: needs a ready binding)
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


_SQLITE_CHECKS_WIDENED = False


def _widen_sqlite_check_constraint(conn, table_name: str, *, old_fragment: str, new_fragment: str) -> None:
    """See ``tests/test_localization_binding.py``'s identical helper: widens
    exactly one pre-existing CHECK constraint's value list on SQLite, which
    cannot ``ALTER TABLE ... DROP/ADD CONSTRAINT``. Copied per this repo's
    convention of duplicating fixture builders per test file."""
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,),
    ).fetchone()
    if row is None or old_fragment not in row[0]:
        return
    old_sql = row[0]
    new_sql = old_sql.replace(old_fragment, new_fragment)

    cols = conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    col_names = ", ".join(c[1] for c in cols)
    legacy_name = f"{table_name}_pre_winelig"
    indices = conn.exec_driver_sql(f"PRAGMA index_list({table_name})").fetchall()
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    for idx in indices:
        index_name = idx[1]
        if not index_name.startswith("sqlite_autoindex_"):
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
    conn.exec_driver_sql(f"ALTER TABLE {table_name} RENAME TO {legacy_name}")
    conn.exec_driver_sql(new_sql)
    conn.exec_driver_sql(
        f"INSERT INTO {table_name} ({col_names}) SELECT {col_names} FROM {legacy_name}"
    )
    conn.exec_driver_sql(f"DROP TABLE {legacy_name}")
    conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _widen_sqlite_localization_check_constraints() -> None:
    global _SQLITE_CHECKS_WIDENED
    if engine.dialect.name != "sqlite" or _SQLITE_CHECKS_WIDENED:
        return
    with engine.begin() as conn:
        _widen_sqlite_check_constraint(
            conn, "provider_call_attempts",
            old_fragment="CHECK (step_kind IN ('generation', 'visual_qc'))",
            new_fragment=(
                "CHECK (step_kind IN ('generation', 'visual_qc', 'source_analysis', "
                "'translation', 'localization_edit', 'localization_ocr', "
                "'localization_visual_qc'))"
            ),
        )
    _SQLITE_CHECKS_WIDENED = True


class WinnerEligibilityTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        _widen_sqlite_localization_check_constraints()

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, CTX.workspace_id)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P WinElig"))
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
        with tenant_session(CTX_OTHER) as s:
            _ensure_workspace(s, CTX_OTHER.workspace_id)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def setUp(self) -> None:
        self.admission = PaidOperationAdmission(
            policy=EntitlementPolicy(),
            quote_engine=CostQuoteEngine(price_registry=PRICE_REGISTRY, price_rule_version="test-v1"),
            reservation_book=ReservationBook(),
        )

    def _build_ready_lineage_chain(self, tag: str) -> dict:
        """Full chain through a ready, active, approved MarketAssetBinding:
        experiment -> decision -> confirmation -> asset -> product -> offer
        -> binding, driving the real Localization Batch pipeline
        (``create_draft`` .. ``confirm`` .. ``publish_binding``) exactly as
        ``tests/test_localization_binding.py``'s ``_build_ready_batch()``
        does, stopping short of the async Attempt/finalize pipeline since a
        ``source_reuse`` binding needs no publication."""
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
                analysis_adapter_version="mock-v1", actor_id="user_1", admission=self.admission,
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
                actor_id="user_1", role="owner", admission=self.admission,
            )
            copy_version_id = scd.copy_version_id

        from app.adapters.persistence.models import LocalizationBatchRecord

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
                admission=self.admission,
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
        chain["batch_id"] = batch_id
        chain["item_id"] = item_id
        return chain


# ---------------------------------------------------------------------------
# evaluate_winner_identity() / validate_winner_eligibility() -- checks 1-7, 11
# ---------------------------------------------------------------------------


class TestWinnerIdentity(WinnerEligibilityTestBase):
    def test_decision_not_found_raises(self) -> None:
        with tenant_session(CTX) as s:
            with self.assertRaises(WinnerIneligibleError) as exc_info:
                evaluate_winner_identity(s, CTX, decision_id="decision_does_not_exist")
        self.assertEqual([f.code for f in exc_info.exception.findings], ["no_decision"])

    def test_cross_workspace_decision_is_invisible(self) -> None:
        chain = _build_confirmed_winner("id01")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX_OTHER) as s:
            with self.assertRaises(WinnerIneligibleError) as exc_info:
                evaluate_winner_identity(s, CTX_OTHER, decision_id=decision_id)
        self.assertEqual([f.code for f in exc_info.exception.findings], ["no_decision"])

    def test_inconclusive_decision_raises(self) -> None:
        chain = _build_confirmed_winner("id02")
        decision_id = _make_decision(chain, verdict="inconclusive", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        self.assertIn("inconclusive_decision", [f.code for f in findings])

    def test_non_winner_decision_raises(self) -> None:
        chain = _build_confirmed_winner("id03")
        decision_id = _make_decision(chain, verdict="lose", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        codes = [f.code for f in findings]
        self.assertIn("non_winner_decision", codes)
        self.assertNotIn("inconclusive_decision", codes)

    def test_missing_confirmation_raises(self) -> None:
        chain = _build_confirmed_winner("id04")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=None)
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        self.assertIn("missing_confirmation", [f.code for f in findings])

    def test_asset_not_approved_raises(self) -> None:
        for review_value, expected in (
            (None, "no_approved_review"),
            ("rejected", "review_rejected"),
            ("needs_changes", "review_needs_changes"),
        ):
            with self.subTest(expected):
                chain = _build_confirmed_winner(f"id05_{expected}")
                _seed_review(chain["asset_version_id"], review_value)
                decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
                with tenant_session(CTX) as s:
                    _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
                self.assertIn(expected, [f.code for f in findings])

    def test_withdrawn_product_raises(self) -> None:
        chain = _build_confirmed_winner("id06")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            product_version = s.get(LibraryVersionRecord, chain["product_version_id"])
            product_version.state = "withdrawn"
            s.add(product_version)
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        self.assertIn("product_withdrawn", [f.code for f in findings])

    def test_withdrawn_offer_raises(self) -> None:
        chain = _build_confirmed_winner("id07")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            offer_version = s.get(LibraryVersionRecord, chain["offer_version_id"])
            offer_version.state = "withdrawn"
            s.add(offer_version)
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        self.assertIn("offer_withdrawn", [f.code for f in findings])

    def test_eligibility_projection_ineligible_raises(self) -> None:
        chain = _build_confirmed_winner("id08")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            append_eligibility_event(
                s, workspace_id=CTX.workspace_id,
                experiment_id=chain["experiment_id"],
                experiment_version_id=chain["experiment_version_id"],
                candidate_id=chain["candidate_id"],
                upstream_event_type="stale_truth_detected",
                upstream_resource_type="truth_package",
                upstream_resource_id=new_id("tp"),
                upstream_event_id=new_id("stale"),
                evidence={"reason": "dependency_changed"},
            )
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        self.assertIn("stale_truth_detected", [f.code for f in findings])

    def test_findings_collected_not_short_circuited(self) -> None:
        chain = _build_confirmed_winner("id09")
        _seed_review(chain["asset_version_id"], "rejected")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            offer_version = s.get(LibraryVersionRecord, chain["offer_version_id"])
            offer_version.state = "withdrawn"
            s.add(offer_version)
        with tenant_session(CTX) as s:
            _, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        codes = [f.code for f in findings]
        self.assertIn("review_rejected", codes)
        self.assertIn("offer_withdrawn", codes)
        self.assertGreaterEqual(len(findings), 2)

    def test_valid_winner_identity_has_no_findings(self) -> None:
        chain = _build_confirmed_winner("id10")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            identity, findings = evaluate_winner_identity(s, CTX, decision_id=decision_id)
        self.assertEqual(findings, ())
        self.assertIsInstance(identity, WinnerIdentity)
        self.assertEqual(identity.decision.id, decision_id)


# ---------------------------------------------------------------------------
# validate_winner_eligibility() -- full 11-check lineage (checks 8-10 too)
# ---------------------------------------------------------------------------


class TestWinnerEligibilityLineage(WinnerEligibilityTestBase):
    def test_valid_winner_returns_lineage(self) -> None:
        chain = self._build_ready_lineage_chain("lin01")
        with tenant_session(CTX) as s:
            lineage = validate_winner_eligibility(s, CTX, decision_id=chain["decision_id"])
        self.assertIsInstance(lineage, EligibleWinnerLineage)
        self.assertEqual(lineage.decision_id, chain["decision_id"])
        self.assertEqual(lineage.market_code, chain["market"])
        self.assertEqual(lineage.placement, "feed")
        self.assertEqual(lineage.market_asset_binding_id, chain["binding_id"])
        self.assertTrue(lineage.winner_fingerprint)

    def test_winner_fingerprint_deterministic(self) -> None:
        chain = self._build_ready_lineage_chain("lin02")
        with tenant_session(CTX) as s:
            lineage1 = validate_winner_eligibility(s, CTX, decision_id=chain["decision_id"])
        with tenant_session(CTX) as s:
            lineage2 = validate_winner_eligibility(s, CTX, decision_id=chain["decision_id"])
        self.assertEqual(lineage1.winner_fingerprint, lineage2.winner_fingerprint)

    def test_binding_not_ready_raises(self) -> None:
        chain = self._build_ready_lineage_chain("lin03")
        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, chain["binding_id"])
            binding.truth_freshness = "stale"
            s.add(binding)
        with tenant_session(CTX) as s:
            with self.assertRaises(WinnerIneligibleError) as exc_info:
                validate_winner_eligibility(s, CTX, decision_id=chain["decision_id"])
        self.assertIn("binding_not_ready", [f.code for f in exc_info.exception.findings])

    def test_missing_binding_raises(self) -> None:
        chain = _build_confirmed_winner("lin04")
        decision_id = _make_decision(chain, verdict="win", confirmation_id=chain["confirmation_id"])
        with tenant_session(CTX) as s:
            with self.assertRaises(WinnerIneligibleError) as exc_info:
                validate_winner_eligibility(s, CTX, decision_id=decision_id)
        self.assertIn("binding_not_ready", [f.code for f in exc_info.exception.findings])

    def test_reference_rights_ineligible_when_truth_package_expired(self) -> None:
        chain = self._build_ready_lineage_chain("lin05")
        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, chain["binding_id"])
            truth_package = s.get(TruthPackageRecord, binding.truth_package_id)
            truth_package.valid_until = datetime.now(timezone.utc) - timedelta(hours=1)
            s.add(truth_package)
        with tenant_session(CTX) as s:
            with self.assertRaises(WinnerIneligibleError) as exc_info:
                validate_winner_eligibility(s, CTX, decision_id=chain["decision_id"])
        self.assertIn("reference_rights_ineligible", [f.code for f in exc_info.exception.findings])


if __name__ == "__main__":
    unittest.main()
