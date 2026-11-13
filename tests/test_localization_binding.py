"""Binding module tests for Localization (#24, spec §2I/§2J, §16.11/§16.12).

Exercises `publish_binding()`, `assess_binding_eligibility()`, and
`apply_dependency_change()` through their public interfaces only, against
the shared suite-wide database (`app.adapters.persistence.database.engine`,
migrated once in `setUpClass`), the same way `tests/test_localization_
worker.py` does.

A real localized AssetVersion + `LocalizationOutputPublicationRecord` +
`LocalizedAssetLineageRecord` is produced by driving the actual Winner ->
Batch -> Attempt -> finalize pipeline through `app.localization.worker`
(mirroring `tests/test_localization_worker.py`'s `TestFinalizeItemHandler`
fixtures) rather than hand-inserting rows that would bypass the composite
foreign keys `market_asset_bindings`/`localized_asset_lineages` depend on.
`publish_binding()` itself is not yet wired into `handle_finalize_item`
(deferred to a later task per the Task 17 brief), so every test here calls
it directly against the real publication/item/batch state that finalize
already produced.
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
    LocalizationAttemptRecord,
    LocalizationBatchRecord,
    LocalizationOutputPublicationRecord,
    LocalizedAssetLineageRecord,
    MarketAssetBindingEventRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    TruthFactRecord,
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
from app.generation.models import GenerationDraftRecord  # noqa: F401
from app.localization import worker
from app.localization.batch import BatchMarketTargetInput, BatchSourceInput
from app.localization.batch import approve_copy, confirm, create_draft, start_preparation, submit_claim_decisions
from app.localization.binding import (
    BindingValidationError,
    apply_dependency_change,
    assess_binding_eligibility,
    publish_binding,
)
from app.localization.claims import ClaimDecisionInput, create_inventory_from_analysis
from app.localization.evidence_tier import EvidenceTier
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)
from app.providers.dispatch import ExternalCallCoordinator

WS_ID = "ws_lb_binding"
CTX = TenantContext(workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner")

_MARKET_ID_COUNTER = itertools.count(1)

PRICE_REGISTRY = {
    "mock-vision": {"mock-vision-1": Decimal("0.01")},
    "mock-edit": {"mock-edit-1": Decimal("0.05")},
    "mock-translate": {"mock-translate-1": Decimal("0.02")},
}


# ---------------------------------------------------------------------------
# Fixture helpers (mirrors tests/test_localization_worker.py)
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
        session.add(WorkspaceRecord(id=ws_id, name=f"Binding test {ws_id}"))
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

    from app.adapters.persistence.models import AdDailyMetricRecord

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
    with tenant_session(ctx) as s:
        pv_id, ov_id = _seed_library_versions(s, ws_id, tag)
        avs = _seed_asset_versions(s, ws_id, tag)
        govern_product_offer_versions(
            s, ctx, product_id=_product_id(ws_id),
            product_version_id=pv_id, offer_version_id=ov_id,
        )
        govern_asset_versions(
            s, ctx, product_id=_product_id(ws_id), asset_version_ids=avs, tag=tag,
        )

    exp = create_experiment_v2(
        ctx, product_id=_product_id(ws_id), name=f"LBind-{tag}", draft_context=_draft_context(pv_id, ov_id),
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
        av = s.get(AssetVersionRecord, asset_version_id)
        existing = s.scalar(select(QualityReviewRecord).where(
            QualityReviewRecord.workspace_id == ctx.workspace_id,
            QualityReviewRecord.asset_version_id == asset_version_id,
            QualityReviewRecord.review_value == "approved",
            QualityReviewRecord.legacy_unscoped.is_(False),
        ))
        if existing is not None:
            return
        s.add(QualityReviewRecord(
            id=new_id("qr"), workspace_id=ctx.workspace_id, product_id=_product_id(ctx.workspace_id),
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


_SQLITE_CHECKS_WIDENED = False


def _widen_sqlite_check_constraint(conn, table_name: str, *, old_fragment: str, new_fragment: str) -> None:
    """See `tests/test_localization_worker.py`'s identical helper: widens
    exactly one pre-existing CHECK constraint's value list on SQLite,
    which cannot `ALTER TABLE ... DROP/ADD CONSTRAINT`. Copied rather than
    imported -- every Localization test module that needs it keeps its
    own copy, matching the repo's existing convention of duplicating
    fixture builders per test file rather than sharing test-only helpers
    across modules."""
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,),
    ).fetchone()
    if row is None or old_fragment not in row[0]:
        return
    old_sql = row[0]
    new_sql = old_sql.replace(old_fragment, new_fragment)

    cols = conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    col_names = ", ".join(c[1] for c in cols)
    legacy_name = f"{table_name}_pre_lbind"
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
        _widen_sqlite_check_constraint(
            conn, "asset_versions",
            old_fragment="CHECK (origin_kind IN ('generation', 'upload', 'reuse', 'legacy'))",
            new_fragment=(
                "CHECK (origin_kind IN ('generation', 'upload', 'reuse', 'legacy', 'localized'))"
            ),
        )
    _SQLITE_CHECKS_WIDENED = True


# ---------------------------------------------------------------------------
# TestCase base
# ---------------------------------------------------------------------------


class BindingTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        _widen_sqlite_localization_check_constraints()

        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as s:
            _ensure_workspace(s, WS_ID)
            if s.get(EntitlementProjectionRecord, WS_ID) is None:
                s.merge(EntitlementProjectionRecord(
                    workspace_id=WS_ID, entitlement_version=1, lease_sequence=1,
                    projection_version=1, schema_version=2, semantic_hash="h" * 8,
                    valid_until=now + timedelta(days=30), issued_at=now - timedelta(days=1),
                    period_start=now - timedelta(days=1), period_end=now + timedelta(days=29),
                    resolved_entitlements_json={"storage.bytes.max": 10 * 1024 ** 3},
                ))
            if s.get(SpendGuardrailRecord, WS_ID) is None:
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
        self.coordinator = ExternalCallCoordinator(session_factory=tenant_session)

    # -- fixture builders -------------------------------------------

    def _build_ready_batch(self, tag: str) -> dict:
        """Build a Batch through the real command sequence up to (but not
        including) `confirm()`, returning enough to drive `confirm()` and
        the Attempt/QC/finalize handlers, plus the exact
        DataSourceVersion/ManualFactSetVersion/TruthResolutionPolicyVersion
        ids the frozen TruthPackage depends on (needed by
        `apply_dependency_change()` tests)."""
        winner = _build_confirmed_winner(WS_ID, CTX, tag)
        _seed_review(CTX, winner["asset_version_id"])

        with tenant_session(CTX) as s:
            mpv = _make_market_policy_version(s, f"LBI{next(_MARKET_ID_COUNTER):04d}"[:10])
            mpv_id = mpv.id

        with tenant_session(CTX) as s:
            row = s.get(MarketPolicyVersionRecord, mpv_id)
            mp = s.get(MarketPolicyRecord, row.market_policy_id)
            market = mp.market_id

        truth_cmd, dep_ids = self._truth_command_for(
            tag, winner["product_version_id"], winner["offer_version_id"], [(mpv_id, market)],
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
            copy_version_id = scd.copy_version_id

        with tenant_session(CTX) as s:
            approve_copy(
                s, CTX, batch_id=batch_id,
                expected_revision=s.get(LocalizationBatchRecord, batch_id).revision,
                copy_version_id=copy_version_id,
                actor_id="user_1", role="owner",
            )

        with tenant_session(CTX) as s:
            batch = s.get(LocalizationBatchRecord, batch_id)
            expected_revision = batch.revision

        with tenant_session(CTX) as s:
            confirmed = confirm(
                s, CTX, batch_id=batch_id, expected_revision=expected_revision,
                edit_provider="mock-edit", edit_model="mock-edit-1",
                edit_adapter_version="mock-edit-v1", actor_id="user_1", role="owner",
                admission=self.admission,
            )

        item_id = confirmed.item_ids[0]
        return {
            "batch_id": batch_id, "item_id": item_id,
            "asset_version_id": winner["asset_version_id"], "decision_id": winner["decision_id"],
            "market": market, "offer_version_id": winner["offer_version_id"],
            **dep_ids,
        }

    def _truth_command_for(
        self, tag: str, pv_id: str, ov_id: str, mpv_rows: list[tuple[str, str]],
    ) -> tuple[TruthResolutionCommand, dict]:
        with tenant_session(CTX) as s:
            policy_version_id = _make_version(s, WS_ID, "truth_resolution_policy", tag)
            data_source_version_id = _make_version(s, WS_ID, "data_source", tag)
            manual_fact_set_version_id = _make_version(s, WS_ID, "manual_fact_set", tag)

        now = datetime.now(timezone.utc)
        window = (now - timedelta(days=1), now + timedelta(days=365))
        facts = {"price": {"amount": "19.99", "currency": "USD"}, "availability": {"available": True}}

        market_cmds = []
        for mpv_id, market in mpv_rows:
            with tenant_session(CTX) as s:
                row = s.get(MarketPolicyVersionRecord, mpv_id)
                locale, country, currency = row.locale, row.country, row.currency
            step = SourceStep(
                data_source_version_id=data_source_version_id, adapter=ManualFactSourceAdapter(),
                adapter_kind="manual", adapter_version="manual-v1", truth_data_mode="real",
                manual_fact_set=ManualFactSet(
                    version_id=manual_fact_set_version_id, facts=facts,
                    valid_from=window[0], valid_to=window[1],
                ),
                manual_fact_set_version_id=manual_fact_set_version_id,
            )
            market_cmds.append(MarketResolutionCommand(
                market=market, locale=locale, country=country, currency=currency,
                market_policy_version_id=mpv_id, sources=(step,),
                fact_policies=(FactPolicy("price"), FactPolicy("availability")),
            ))
        cmd = TruthResolutionCommand(
            product_version_id=pv_id, offer_version_id=ov_id,
            truth_resolution_policy_version_id=policy_version_id, truth_data_mode="real",
            max_staleness_seconds=86400 * 30, resolution_rule_version="truth-rule-v1",
            markets=tuple(market_cmds), now=now,
        )
        dep_ids = {
            "truth_resolution_policy_version_id": policy_version_id,
            "data_source_version_id": data_source_version_id,
            "manual_fact_set_version_id": manual_fact_set_version_id,
            "market_policy_version_id": mpv_rows[0][0],
        }
        return cmd, dep_ids

    async def _run_edit(self, batch_id: str, item_id: str, attempt_number: int = 1) -> None:
        await worker.handle_edit_attempt(
            CTX, batch_id=batch_id, item_id=item_id, attempt_number=attempt_number,
            session_factory=tenant_session, coordinator=self.coordinator,
        )

    def _attempt(self, item_id: str, batch_id: str, attempt_number: int) -> LocalizationAttemptRecord:
        with tenant_session(CTX) as s:
            return s.scalar(
                select(LocalizationAttemptRecord).where(
                    LocalizationAttemptRecord.workspace_id == WS_ID,
                    LocalizationAttemptRecord.item_id == item_id,
                    LocalizationAttemptRecord.batch_id == batch_id,
                    LocalizationAttemptRecord.attempt_number == attempt_number,
                )
            )

    def _run_deterministic_qc(self, batch_id: str, item_id: str, attempt_id: str) -> None:
        with tenant_session(CTX) as s:
            worker.handle_run_deterministic_qc(s, CTX, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id)

    def _finalize(self, batch_id: str, item_id: str, attempt_id: str) -> None:
        with tenant_session(CTX) as s:
            worker.handle_finalize_item(s, CTX, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id)

    async def _run_to_finalized(self, tag: str) -> dict:
        """Drives a Batch all the way through a real successful finalize:
        the same pipeline `tests/test_localization_worker.py`'s
        `TestFinalizeItemHandler` uses, so the `LocalizationOutputPublicationRecord`
        and active binding returned here are the exact records production
        finalization creates atomically."""
        built = self._build_ready_batch(tag)
        await self._run_edit(built["batch_id"], built["item_id"], attempt_number=1)
        attempt = self._attempt(built["item_id"], built["batch_id"], 1)
        self._run_deterministic_qc(built["batch_id"], built["item_id"], attempt.id)
        self._finalize(built["batch_id"], built["item_id"], attempt.id)

        with tenant_session(CTX) as s:
            publication = s.scalar(
                select(LocalizationOutputPublicationRecord).where(
                    LocalizationOutputPublicationRecord.workspace_id == WS_ID,
                    LocalizationOutputPublicationRecord.item_id == built["item_id"],
                )
            )
            lineage = s.scalar(
                select(LocalizedAssetLineageRecord).where(
                    LocalizedAssetLineageRecord.workspace_id == WS_ID,
                    LocalizedAssetLineageRecord.item_id == built["item_id"],
                )
            )
        built["publication_id"] = publication.id
        built["child_asset_version_id"] = publication.asset_version_id
        built["lineage"] = {
            "id": lineage.id, "batch_id": lineage.batch_id, "item_id": lineage.item_id,
            "attempt_id": lineage.attempt_id, "output_id": lineage.output_id,
            "publication_id": lineage.publication_id, "decision_id": lineage.decision_id,
            "claim_decision_set_id": lineage.claim_decision_set_id,
            "product_version_id": lineage.product_version_id,
            "offer_version_id": lineage.offer_version_id,
            "source_market": lineage.source_market, "target_market": lineage.target_market,
            "truth_package_id": lineage.truth_package_id,
            "truth_package_market_id": lineage.truth_package_market_id,
            "inventory_id": lineage.inventory_id, "copy_version_id": lineage.copy_version_id,
            "market_policy_version_id": lineage.market_policy_version_id,
            "source_generation_mode": lineage.source_generation_mode,
            "experiment_evidence_mode": lineage.experiment_evidence_mode,
            "truth_data_mode": lineage.truth_data_mode,
            "provider_execution_mode": lineage.provider_execution_mode,
            "evidence_tier": lineage.evidence_tier,
        }
        return built


# ---------------------------------------------------------------------------
# TestBindingPublication
# ---------------------------------------------------------------------------


class TestBindingPublication(BindingTestBase):
    async def test_publish_supersedes_active_binding(self) -> None:
        built = await self._run_to_finalized("pub_supersede01")

        with tenant_session(CTX) as s:
            first = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=built["publication_id"], actor_id="user_1",
            )
        self.assertTrue(first.replayed)
        self.assertIsNone(first.superseded_binding_id)
        self.assertEqual(first.binding_kind, "localized")

        # A second `publish_binding()` call for the exact same (source,
        # target market, offer) natural key -- here simulated as a later
        # `source_reuse` republication of the same Item -- must supersede
        # the first binding rather than leaving two active rows.
        with tenant_session(CTX) as s:
            second = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=None, actor_id="user_1",
            )
        self.assertEqual(second.superseded_binding_id, first.binding_id)
        self.assertNotEqual(second.binding_id, first.binding_id)

        with tenant_session(CTX) as s:
            old = s.get(MarketAssetBindingRecord, first.binding_id)
            new = s.get(MarketAssetBindingRecord, second.binding_id)
            active_rows = s.scalars(
                select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == WS_ID,
                    MarketAssetBindingRecord.source_asset_version_id == built["asset_version_id"],
                    MarketAssetBindingRecord.target_market == built["market"],
                    MarketAssetBindingRecord.offer_version_id == built["offer_version_id"],
                    MarketAssetBindingRecord.active.is_(True),
                )
            ).all()

        self.assertFalse(old.active)
        self.assertTrue(new.active)
        self.assertEqual(len(active_rows), 1)
        self.assertEqual(active_rows[0].id, second.binding_id)

        with tenant_session(CTX) as s:
            old_events = s.scalars(
                select(MarketAssetBindingEventRecord).where(
                    MarketAssetBindingEventRecord.workspace_id == WS_ID,
                    MarketAssetBindingEventRecord.binding_id == first.binding_id,
                ).order_by(MarketAssetBindingEventRecord.sequence)
            ).all()
        self.assertEqual([e.event_type for e in old_events], ["published", "superseded"])

    async def test_lineage_is_immutable(self) -> None:
        built = await self._run_to_finalized("lineage01")
        lineage = built["lineage"]

        self.assertEqual(lineage["batch_id"], built["batch_id"])
        self.assertEqual(lineage["item_id"], built["item_id"])
        self.assertEqual(lineage["decision_id"], built["decision_id"])
        self.assertEqual(lineage["offer_version_id"], built["offer_version_id"])
        self.assertEqual(lineage["target_market"], built["market"])
        self.assertEqual(lineage["publication_id"], built["publication_id"])

        # Re-reading the same row twice returns byte-identical provenance
        # -- there is no update path in this module (or any other) that
        # touches `localized_asset_lineages`.
        with tenant_session(CTX) as s:
            reread = s.get(LocalizedAssetLineageRecord, lineage["id"])
            reread_snapshot = {
                "batch_id": reread.batch_id, "item_id": reread.item_id,
                "decision_id": reread.decision_id, "offer_version_id": reread.offer_version_id,
                "target_market": reread.target_market, "publication_id": reread.publication_id,
                "truth_package_id": reread.truth_package_id,
                "evidence_tier": reread.evidence_tier,
            }
        self.assertEqual(reread_snapshot["batch_id"], lineage["batch_id"])
        self.assertEqual(reread_snapshot["truth_package_id"], lineage["truth_package_id"])
        self.assertEqual(reread_snapshot["evidence_tier"], lineage["evidence_tier"])

        with tenant_session(CTX) as s:
            publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=built["publication_id"], actor_id="user_1",
            )
        # Publishing a binding never mutates the lineage row it references.
        with tenant_session(CTX) as s:
            after = s.get(LocalizedAssetLineageRecord, lineage["id"])
            after_snapshot = {
                "batch_id": after.batch_id, "item_id": after.item_id,
                "decision_id": after.decision_id, "offer_version_id": after.offer_version_id,
                "target_market": after.target_market, "publication_id": after.publication_id,
                "truth_package_id": after.truth_package_id, "evidence_tier": after.evidence_tier,
            }
        self.assertEqual(after_snapshot, reread_snapshot)

    async def test_child_starts_pending_review(self) -> None:
        built = await self._run_to_finalized("pending_review01")

        with tenant_session(CTX) as s:
            result = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=built["publication_id"], actor_id="user_1",
            )
        self.assertEqual(result.review_state, "pending_review")
        self.assertEqual(result.truth_freshness, "current")
        self.assertFalse(result.ready_for_experiment)

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, result.binding_id)
            self.assertEqual(binding.review_state, "pending_review")
            self.assertTrue(binding.active)
            reviews = s.scalars(
                select(QualityReviewRecord).where(
                    QualityReviewRecord.asset_version_id == built["child_asset_version_id"],
                )
            ).all()
        self.assertEqual(reviews, [], "no QualityReview is copied or created for the child")

        with tenant_session(CTX) as s:
            report = assess_binding_eligibility(s, CTX, binding_id=result.binding_id)
        self.assertFalse(report.eligible_for_experiment)
        self.assertIn("review_state:pending_review", report.findings)

    async def test_source_reuse_references_existing_version(self) -> None:
        built = await self._run_to_finalized("source_reuse01")

        with tenant_session(CTX) as s:
            result = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=None, actor_id="user_1",
            )
        self.assertEqual(result.binding_kind, "source_reuse")
        self.assertEqual(result.bound_asset_version_id, built["asset_version_id"])

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, result.binding_id)
            self.assertEqual(binding.bound_asset_version_id, binding.source_asset_version_id)
            self.assertIsNone(binding.publication_id)
            # No new AssetVersion was created for a source_reuse binding --
            # it references the exact already-reviewed source version.
            self.assertNotEqual(binding.bound_asset_version_id, built["child_asset_version_id"])
            reviews = s.scalars(
                select(QualityReviewRecord).where(
                    QualityReviewRecord.asset_version_id == built["asset_version_id"],
                )
            ).all()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].review_value, "approved")


# ---------------------------------------------------------------------------
# TestStalePropagation
# ---------------------------------------------------------------------------


class TestStalePropagation(BindingTestBase):
    async def _published_binding(self, tag: str) -> dict:
        built = await self._run_to_finalized(tag)
        with tenant_session(CTX) as s:
            result = publish_binding(
                s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                publication_id=built["publication_id"], actor_id="user_1",
            )
        built["binding_id"] = result.binding_id
        return built

    async def test_superseded_data_source_stales_binding(self) -> None:
        built = await self._published_binding("stale_ds01")

        with tenant_session(CTX) as s:
            result = apply_dependency_change(
                s, CTX, change_kind="data_source_version",
                changed_version_id=built["data_source_version_id"], actor_id="user_1",
            )
        self.assertTrue(result.tracked)
        self.assertIn(built["binding_id"], result.staled_binding_ids)

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, built["binding_id"])
            self.assertEqual(binding.truth_freshness, "stale")
            self.assertFalse(binding.ready_for_experiment)

            events = s.scalars(
                select(MarketAssetBindingEventRecord).where(
                    MarketAssetBindingEventRecord.workspace_id == WS_ID,
                    MarketAssetBindingEventRecord.binding_id == built["binding_id"],
                    MarketAssetBindingEventRecord.event_type == "stale_truth",
                )
            ).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].dependency_change_id, result.dependency_change_id)

    async def test_superseded_market_policy_stales_binding(self) -> None:
        built = await self._published_binding("stale_mp01")

        with tenant_session(CTX) as s:
            result = apply_dependency_change(
                s, CTX, change_kind="market_policy_version",
                changed_version_id=built["market_policy_version_id"], actor_id="user_1",
            )
        self.assertTrue(result.tracked)
        self.assertIn(built["binding_id"], result.staled_binding_ids)

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, built["binding_id"])
        self.assertEqual(binding.truth_freshness, "stale")
        self.assertFalse(binding.ready_for_experiment)

    async def test_historical_truth_unchanged(self) -> None:
        built = await self._published_binding("historical01")

        with tenant_session(CTX) as s:
            truth_package_id = built["lineage"]["truth_package_id"]
            package_before = s.get(TruthPackageRecord, truth_package_id)
            before_snapshot = {
                "canonical_hash": package_before.canonical_hash,
                "status": package_before.status, "valid_until": package_before.valid_until,
            }
            facts_before = {
                f.id: (f.value_json, f.valid_until)
                for f in s.scalars(
                    select(TruthFactRecord).where(TruthFactRecord.truth_package_id == truth_package_id)
                ).all()
            }
            av_before = s.get(AssetVersionRecord, built["child_asset_version_id"])
            av_before_snapshot = (av_before.content_sha256, av_before.object_key, av_before.parent_version_id)

        with tenant_session(CTX) as s:
            apply_dependency_change(
                s, CTX, change_kind="data_source_version",
                changed_version_id=built["data_source_version_id"], actor_id="user_1",
            )

        with tenant_session(CTX) as s:
            package_after = s.get(TruthPackageRecord, truth_package_id)
            after_snapshot = {
                "canonical_hash": package_after.canonical_hash,
                "status": package_after.status, "valid_until": package_after.valid_until,
            }
            facts_after = {
                f.id: (f.value_json, f.valid_until)
                for f in s.scalars(
                    select(TruthFactRecord).where(TruthFactRecord.truth_package_id == truth_package_id)
                ).all()
            }
            av_after = s.get(AssetVersionRecord, built["child_asset_version_id"])
            av_after_snapshot = (av_after.content_sha256, av_after.object_key, av_after.parent_version_id)

        self.assertEqual(before_snapshot, after_snapshot)
        self.assertEqual(facts_before, facts_after)
        self.assertEqual(av_before_snapshot, av_after_snapshot)

    async def test_event_delivery_idempotent(self) -> None:
        built = await self._published_binding("bind_idempotent01")

        with tenant_session(CTX) as s:
            first = apply_dependency_change(
                s, CTX, change_kind="data_source_version",
                changed_version_id=built["data_source_version_id"], actor_id="user_1",
            )
        with tenant_session(CTX) as s:
            revision_after_first = s.get(MarketAssetBindingRecord, built["binding_id"]).revision

        with tenant_session(CTX) as s:
            second = apply_dependency_change(
                s, CTX, change_kind="data_source_version",
                changed_version_id=built["data_source_version_id"], actor_id="user_1",
            )

        self.assertEqual(first.dependency_change_id, second.dependency_change_id)
        self.assertIn(built["binding_id"], first.staled_binding_ids)
        self.assertIn(built["binding_id"], second.skipped_binding_ids)
        self.assertNotIn(built["binding_id"], second.staled_binding_ids)

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, built["binding_id"])
            events = s.scalars(
                select(MarketAssetBindingEventRecord).where(
                    MarketAssetBindingEventRecord.workspace_id == WS_ID,
                    MarketAssetBindingEventRecord.binding_id == built["binding_id"],
                    MarketAssetBindingEventRecord.event_type == "stale_truth",
                )
            ).all()
        self.assertEqual(binding.revision, revision_after_first, "a redelivered change must not bump revision again")
        self.assertEqual(len(events), 1, "a redelivered change must not append a duplicate event")

    async def test_downstream_experiment_invalidation_precise(self) -> None:
        """Staling one binding's dependency must never touch an unrelated
        binding whose TruthPackage does not depend on the changed
        version -- precise, not blanket, invalidation (spec §2I/§16.12)."""
        first = await self._published_binding("precise_a01")
        second = await self._published_binding("precise_b01")
        self.assertNotEqual(
            first["data_source_version_id"], second["data_source_version_id"],
        )

        with tenant_session(CTX) as s:
            result = apply_dependency_change(
                s, CTX, change_kind="data_source_version",
                changed_version_id=first["data_source_version_id"], actor_id="user_1",
            )
        self.assertIn(first["binding_id"], result.staled_binding_ids)
        self.assertNotIn(second["binding_id"], result.staled_binding_ids)

        with tenant_session(CTX) as s:
            b1 = s.get(MarketAssetBindingRecord, first["binding_id"])
            b2 = s.get(MarketAssetBindingRecord, second["binding_id"])
        self.assertEqual(b1.truth_freshness, "stale")
        self.assertEqual(b2.truth_freshness, "current")
        self.assertTrue(b2.active)

    async def test_credential_rotation_does_not_stale(self) -> None:
        built = await self._published_binding("cred_rot01")

        with tenant_session(CTX) as s:
            before_revision = s.get(MarketAssetBindingRecord, built["binding_id"]).revision

        with tenant_session(CTX) as s:
            result = apply_dependency_change(
                s, CTX, change_kind="credential_version",
                changed_version_id="cv_rotated_1", actor_id="user_1",
            )
        self.assertFalse(result.tracked)
        self.assertEqual(result.staled_binding_ids, ())

        with tenant_session(CTX) as s:
            binding = s.get(MarketAssetBindingRecord, built["binding_id"])
            events = s.scalars(
                select(MarketAssetBindingEventRecord).where(
                    MarketAssetBindingEventRecord.workspace_id == WS_ID,
                    MarketAssetBindingEventRecord.binding_id == built["binding_id"],
                )
            ).all()
        self.assertEqual(binding.truth_freshness, "current")
        self.assertTrue(binding.active)
        self.assertEqual(binding.revision, before_revision)
        self.assertEqual([e.event_type for e in events], ["published"])


class TestBindingErrors(BindingTestBase):
    async def test_apply_dependency_change_rejects_unknown_kind(self) -> None:
        with self.assertRaises(BindingValidationError):
            with tenant_session(CTX) as s:
                apply_dependency_change(
                    s, CTX, change_kind="not_a_real_kind",
                    changed_version_id="whatever", actor_id="user_1",
                )

    async def test_publish_binding_unknown_publication_rejected(self) -> None:
        built = await self._run_to_finalized("unknown_pub01")
        with self.assertRaises(BindingValidationError):
            with tenant_session(CTX) as s:
                publish_binding(
                    s, CTX, item_id=built["item_id"], batch_id=built["batch_id"],
                    publication_id="lop_does_not_exist", actor_id="user_1",
                )


if __name__ == "__main__":
    unittest.main()
